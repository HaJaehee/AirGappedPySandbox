"""Stateful execution core.

Wraps a single long-lived IPython kernel (via ``jupyter_client``) so that
variables, imported modules and parsed dataframes persist across separate
``execute_python_code`` tool calls. The kernel is launched with a configurable
interpreter (``config.KERNEL_PYTHON``) so the heavy data-science libraries can
live in the 4 GB portable Python while the MCP server runs anywhere.

Verdict policy: this layer reports only what it can observe -- ``OK`` (the cell
finished without an uncaught exception), ``FAILED`` (it raised, or exited with a
non-zero code) or ``TIMEOUT``. "It ran" is deliberately NOT called "SUCCESS":
whether the code did what the model intended is something this layer cannot
know, and calling it SUCCESS is what led weak models to report failures as done.
Failures that leave the cell's own control flow intact -- a crashed background
thread, a child process exiting non-zero, a traceback caught and printed -- never
arrive as an ``error`` message, so they are collected separately into
``ExecResult.signals`` (see ``_STARTUP_CODE`` and ``_PROBE_CODE``).

Timeout policy: an over-running cell is interrupted. If the kernel returns to
idle it is reused (state preserved). If it stays wedged past a grace period --
which happens on Windows for blocking C calls such as ``time.sleep`` that the
interrupt event cannot break into -- the kernel is restarted so the next call
always gets a responsive kernel. A restart clears in-memory state and this is
reported to the caller.
"""

from __future__ import annotations

import json
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field

from jupyter_client.kernelspec import KernelSpec
from jupyter_client.manager import KernelManager

import config
import pip_installer

# Matches ANSI colour escapes IPython injects into tracebacks.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Grace period (seconds) to wait for a kernel to recover after an interrupt
# before giving up and restarting it.
_INTERRUPT_GRACE = 5


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    # OK = the cell finished without an uncaught exception. Deliberately not
    # called SUCCESS -- see the verdict policy in this module's docstring.
    status: str = "OK"  # OK | FAILED | TIMEOUT
    result_repr: str = ""    # text/plain of the last expression, if any
    error_name: str = ""
    state_reset: bool = False  # True if the kernel was restarted this call
    exit_code: int | str | None = None  # sys.exit() code, when the code called it
    signals: list[str] = field(default_factory=list)  # non-raising failure evidence
    fields: dict = field(default_factory=dict)


# Code run once, silently, right after the kernel boots (and after any restart).
# Forces a headless matplotlib backend, guarantees the workspace directory
# exists so the LLM's "./workspace/..." paths always resolve, and installs the
# incident ledger.
#
# The ledger exists because the loudest failures are invisible to the kernel
# protocol: a background thread that raises, or a child process that exits
# non-zero, leaves the cell itself finishing normally, so no `error` message is
# ever sent and the old status logic called it SUCCESS. Each hook chains to the
# original, so nothing the model would otherwise see is suppressed.
#
# This runs only on a fresh or restarted kernel, so the hooks cannot stack.
_STARTUP_CODE = """
import os as _os
_os.makedirs('workspace', exist_ok=True)
try:
    import matplotlib as _mpl
    _mpl.use('Agg')
except Exception:
    pass

_sandbox_incidents = []
_sandbox_exit_code = None


def _sandbox_record(text):
    if len(_sandbox_incidents) < 20:
        _sandbox_incidents.append(str(text)[:2000])


try:
    import threading as _threading, traceback as _traceback
    _sandbox_prev_threadhook = _threading.excepthook

    def _sandbox_threadhook(args):
        try:
            _sandbox_record("background thread %r raised:\\n%s" % (
                getattr(args.thread, 'name', '?'),
                "".join(_traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback))[-1200:]))
        finally:
            _sandbox_prev_threadhook(args)

    _threading.excepthook = _sandbox_threadhook
except Exception:
    pass

try:
    import subprocess as _subprocess

    def _sandbox_cmd(args):
        try:
            return (args if isinstance(args, str)
                    else " ".join(str(a) for a in args))[:300]
        except Exception:
            return "<command>"

    _sandbox_prev_run = _subprocess.run

    def _sandbox_run(*a, **kw):
        cp = _sandbox_prev_run(*a, **kw)
        if getattr(cp, 'returncode', 0):
            _sandbox_record("subprocess exited with code %s: %s"
                            % (cp.returncode, _sandbox_cmd(cp.args)))
        return cp

    _subprocess.run = _sandbox_run
    _sandbox_prev_call = _subprocess.call

    def _sandbox_call(*a, **kw):
        rc = _sandbox_prev_call(*a, **kw)
        if rc:
            _sandbox_record("subprocess exited with code %s" % rc)
        return rc

    _subprocess.call = _sandbox_call
    _sandbox_prev_wait = _subprocess.Popen.wait

    def _sandbox_wait(self, *a, **kw):
        rc = _sandbox_prev_wait(self, *a, **kw)
        if rc:
            _sandbox_record("subprocess exited with code %s: %s"
                            % (rc, _sandbox_cmd(self.args)))
        return rc

    _subprocess.Popen.wait = _sandbox_wait
except Exception:
    pass

try:
    _sandbox_prev_system = _os.system

    def _sandbox_system(cmd):
        rc = _sandbox_prev_system(cmd)
        if rc:
            _sandbox_record("os.system exited with code %s: %s" % (rc, str(cmd)[:300]))
        return rc

    _os.system = _sandbox_system
except Exception:
    pass
"""

# Drains the ledger after every execution. Answers on stdout as one sentinel
# line of JSON so a stray print() in the user's code cannot be mistaken for it.
_PROBE_SENTINEL = "@@SANDBOX_PROBE@@"
_PROBE_CODE = """
try:
    import json as _sb_json
    try:
        _sb_inc = list(_sandbox_incidents)
        _sandbox_incidents.clear()
    except NameError:
        _sb_inc = []
    try:
        _sb_exit = _sandbox_exit_code
    except NameError:
        _sb_exit = None
    _sandbox_exit_code = None
    if not isinstance(_sb_exit, int) and _sb_exit is not None:
        _sb_exit = str(_sb_exit)
    print('%s' + _sb_json.dumps({'exit': _sb_exit, 'incidents': _sb_inc}))
    del _sb_json, _sb_inc, _sb_exit
except Exception:
    pass
""" % _PROBE_SENTINEL

_PKG_SENTINEL = "@@SANDBOX_PKGS@@"


def _site_dir_code(site_dir: str) -> str:
    """Make a directory importable in a running kernel, with no restart.

    A restart would wipe the dataframes the conversation is working with, so a
    freshly pip-installed package has to be reachable without one.
    """
    return (
        "try:\n"
        "    import site as _sb_site, sys as _sb_sys, importlib as _sb_il\n"
        f"    _sb_dir = {site_dir!r}\n"
        "    if _sb_dir not in _sb_sys.path:\n"
        "        _sb_site.addsitedir(_sb_dir)\n"
        "    _sb_il.invalidate_caches()\n"
        "    for _sb_k in ('_sb_site', '_sb_sys', '_sb_il', '_sb_dir'):\n"
        "        globals().pop(_sb_k, None)\n"
        "    globals().pop('_sb_k', None)\n"
        "except Exception:\n"
        "    pass\n"
    )


def _pkg_probe_code(names: list[str]) -> str:
    """Ask the kernel which distributions it can actually see, and at what version.

    Deliberately asked of the KERNEL rather than of pip: pip reporting success
    proves a download, not that the package became importable where the code
    runs. importlib.metadata also avoids guessing import names from
    distribution names ('beautifulsoup4' imports as 'bs4').
    """
    return (
        "try:\n"
        "    import json as _sb_json\n"
        "    from importlib.metadata import version as _sb_ver\n"
        "    _sb_out = {}\n"
        f"    for _sb_n in {list(names)!r}:\n"
        "        try:\n"
        "            _sb_out[_sb_n] = _sb_ver(_sb_n)\n"
        "        except Exception:\n"
        "            _sb_out[_sb_n] = None\n"
        f"    print({_PKG_SENTINEL!r} + _sb_json.dumps(_sb_out))\n"
        "    for _sb_k in ('_sb_json', '_sb_ver', '_sb_out', '_sb_n'):\n"
        "        globals().pop(_sb_k, None)\n"
        "    globals().pop('_sb_k', None)\n"
        "except Exception:\n"
        "    pass\n"
    )


# Evidence that a cell which never raised still went wrong.
_TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\):", re.M)
_LOG_ERROR_RE = re.compile(r"^(?:ERROR|CRITICAL)[: ]", re.M)


def _parse_exit_code(evalue: str) -> int | str | None:
    """Exit code out of IPython's SystemExit evalue ('', '0', '2', 'message')."""
    text = _strip_ansi((evalue or "").strip())
    if not text or text == "None":
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _stderr_signals(stderr: str) -> list[str]:
    """Failure evidence visible only in the text a non-raising cell wrote."""
    found: list[str] = []
    if _TRACEBACK_RE.search(stderr):
        found.append("a traceback was printed to stderr (an exception was caught, "
                     "not fixed)")
    if _LOG_ERROR_RE.search(stderr):
        found.append("ERROR/CRITICAL log records were emitted")
    return found


class StatefulKernel:
    """Thread-safe wrapper around one persistent IPython kernel."""

    def __init__(self) -> None:
        self._km: KernelManager | None = None
        self._kc = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Boot the kernel and run startup code. Idempotent."""
        with self._lock:
            if self._km is not None and self._km.is_alive():
                return
            config.ensure_workspace()
            self._launch_locked()
            self._run_startup_locked()

    def _launch_locked(self) -> None:
        km = KernelManager()
        # Launch the kernel with the configured interpreter (portable Python).
        spec = KernelSpec(
            argv=[
                config.KERNEL_PYTHON,
                "-m",
                "ipykernel_launcher",
                "-f",
                "{connection_file}",
            ],
            display_name="air-gapped-sandbox",
            language="python",
        )
        # Override the resolved spec so KernelManager builds its launch command
        # from our argv instead of a globally-installed kernelspec.
        km._kernel_spec = spec
        # Run the kernel *inside* the workspace so a plain relative path in
        # executed code ("chart.png") lands where every tool looks.
        #
        # This used to be PROJECT_ROOT, which only worked while the workspace
        # was the project's own ./workspace. Once SANDBOX_WORKSPACE points
        # elsewhere -- the whole reason that setting exists -- executed code
        # writing "./workspace/chart.png" went to the *server's* directory
        # while write_workspace_file, run_python_file, list_workspace_files and
        # the artifact scan all used SANDBOX_WORKSPACE. Files vanished.
        km.start_kernel(cwd=str(config.ensure_workspace()))
        kc = km.client()
        kc.start_channels()
        try:
            kc.wait_for_ready(timeout=config.STARTUP_TIMEOUT)
        except RuntimeError as exc:  # kernel died / timed out coming up
            kc.stop_channels()
            km.shutdown_kernel(now=True)
            raise RuntimeError(f"Kernel failed to start: {exc}") from exc
        self._km = km
        self._kc = kc

    def _run_startup_locked(self) -> None:
        """Send startup init and drain to idle. Assumes lock held."""
        code = _STARTUP_CODE
        site_dir = pip_installer.site_packages_dir()
        if site_dir is not None:
            # Packages installed by install_python_packages live outside this
            # interpreter's own site-packages; make them importable from the
            # first cell, not only after an install in this session.
            code += "\n" + _site_dir_code(str(site_dir))
        self._run_silent_locked(code, timeout=config.STARTUP_TIMEOUT)

    def shutdown(self) -> None:
        with self._lock:
            if self._kc is not None:
                try:
                    self._kc.stop_channels()
                except Exception:
                    pass
            if self._km is not None:
                try:
                    self._km.shutdown_kernel(now=True)
                except Exception:
                    pass
            self._km = None
            self._kc = None

    def restart(self) -> None:
        """Restart the kernel, wiping all in-memory state."""
        with self._lock:
            self._restart_locked()

    def _restart_locked(self) -> None:
        try:
            self._km.restart_kernel(now=True)
            self._kc.wait_for_ready(timeout=config.STARTUP_TIMEOUT)
        except Exception:
            # Fall back to a clean relaunch.
            try:
                if self._kc is not None:
                    self._kc.stop_channels()
                if self._km is not None:
                    self._km.shutdown_kernel(now=True)
            except Exception:
                pass
            self._launch_locked()
        self._run_startup_locked()

    # -- execution ---------------------------------------------------------

    def execute(self, code: str, timeout: int | None = None) -> ExecResult:
        """Execute ``code`` in the kernel and collect its output.

        Serialised through a lock so concurrent tool calls cannot interleave on
        the same kernel channels.
        """
        if timeout is None:
            timeout = config.EXEC_TIMEOUT
        with self._lock:
            if self._km is None or not self._km.is_alive():
                self._launch_locked()
                self._run_startup_locked()
            return self._execute_locked(code, timeout)

    def _execute_locked(self, code: str, timeout: int) -> ExecResult:
        kc = self._kc
        # Drain any stale iopub messages left over from a prior call.
        self._drain_iopub(kc)

        msg_id = kc.execute(code, allow_stdin=False, store_history=True)
        result = ExecResult()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                recovered = self._handle_timeout_locked(msg_id)
                result.status = "TIMEOUT"
                stderr_parts.append(
                    f"\n[sandbox] Execution exceeded {timeout}s and was interrupted."
                )
                if not recovered:
                    result.state_reset = True
                    stderr_parts.append(
                        "\n[sandbox] Kernel was unresponsive and has been "
                        "restarted; in-memory variables were cleared."
                    )
                break
            try:
                msg = kc.get_iopub_msg(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue  # belongs to a different request

            mtype = msg["msg_type"]
            content = msg["content"]

            if mtype == "stream":
                (stdout_parts if content["name"] == "stdout" else stderr_parts).append(
                    content["text"]
                )
            elif mtype in ("execute_result", "display_data"):
                data = content.get("data", {})
                if "text/plain" in data:
                    result.result_repr = data["text/plain"]
            elif mtype == "error":
                ename = content.get("ename", "")
                if ename == "SystemExit":
                    # A script ending in sys.exit(0) has finished NORMALLY, but
                    # IPython reports every SystemExit as an error -- which made
                    # working scripts come back as ERROR, with a "use %tb"
                    # traceback the model then tried to "fix". Judge by the code
                    # instead, and drop that traceback either way.
                    code = _parse_exit_code(content.get("evalue", ""))
                    result.exit_code = 0 if code is None else code
                    if code:
                        result.status = "FAILED"
                        result.error_name = "SystemExit"
                        stderr_parts.append(
                            f"\n[sandbox] The code called sys.exit({code!r})."
                        )
                else:
                    result.status = "FAILED"
                    result.error_name = ename
                    tb = "\n".join(content.get("traceback", []))
                    stderr_parts.append(_strip_ansi(tb))
            elif mtype == "status" and content.get("execution_state") == "idle":
                self._read_shell_reply(kc, msg_id, timeout=5)
                break

        stdout = _strip_ansi("".join(stdout_parts))
        stderr = _strip_ansi("".join(stderr_parts))

        # Drain the in-kernel ledger, and pick up a sys.exit() code that the
        # run_python_file wrapper caught before IPython could see it. Skipped
        # after a restart: that wiped the ledger along with everything else.
        if not result.state_reset:
            exit_code, incidents = self._collect_probe_locked()
            if exit_code is not None and result.exit_code is None:
                result.exit_code = exit_code
                if exit_code:
                    result.status = "FAILED"
                    stderr += f"\n[sandbox] The script exited with code {exit_code!r}."
            result.signals.extend(incidents)

        if result.status == "OK":
            # Only for a cell that never raised: when it did, the traceback in
            # stderr IS the failure being reported, not a second signal.
            result.signals.extend(_stderr_signals(stderr))

        result.stdout = self._cap(stdout)
        result.stderr = self._cap(stderr)
        return result

    # -- helpers -----------------------------------------------------------

    def _handle_timeout_locked(self, msg_id: str) -> bool:
        """Interrupt the running cell; restart if it will not recover.

        Returns True if the kernel returned to idle (state preserved), False if
        it had to be restarted (state cleared).
        """
        try:
            self._km.interrupt_kernel()
        except Exception:
            pass
        # Wait for the interrupted cell to yield an idle status.
        deadline = time.monotonic() + _INTERRUPT_GRACE
        while time.monotonic() < deadline:
            try:
                msg = self._kc.get_iopub_msg(timeout=0.3)
            except queue.Empty:
                continue
            except Exception:
                break
            if (
                msg["msg_type"] == "status"
                and msg["content"].get("execution_state") == "idle"
                and msg.get("parent_header", {}).get("msg_id") == msg_id
            ):
                self._read_shell_reply(self._kc, msg_id, timeout=2)
                self._drain_iopub(self._kc)
                return True
        # Still wedged (e.g. a blocking time.sleep on Windows): restart.
        self._restart_locked()
        return False

    def _collect_probe_locked(self) -> tuple[int | str | None, list[str]]:
        """Read and clear the kernel-side incident ledger. Lock held.

        Costs one extra kernel round-trip per execution. Draining it on every
        call -- even when the result is already a failure -- is what stops a
        late incident from surfacing against somebody else's next call.
        """
        try:
            out = self._run_silent_locked(_PROBE_CODE, timeout=10)
        except Exception:
            return None, []
        for line in out.splitlines():
            if not line.startswith(_PROBE_SENTINEL):
                continue
            try:
                data = json.loads(line[len(_PROBE_SENTINEL):])
            except ValueError:
                return None, []
            return data.get("exit"), [str(x) for x in (data.get("incidents") or [])]
        return None, []

    def _run_silent_locked(self, code: str, timeout: int) -> str:
        """Execute code, drain to idle and return its stdout. Lock held.

        ``store_history=False`` keeps these hidden cells out of IPython's
        In/Out history, so they never shift the ``_`` the model may rely on.
        """
        kc = self._kc
        self._drain_iopub(kc)
        msg_id = kc.execute(code, allow_stdin=False, store_history=False)
        parts: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = kc.get_iopub_msg(timeout=0.3)
            except queue.Empty:
                continue
            except Exception:
                break
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            if msg["msg_type"] == "stream" and msg["content"].get("name") == "stdout":
                parts.append(msg["content"]["text"])
            elif (
                msg["msg_type"] == "status"
                and msg["content"].get("execution_state") == "idle"
            ):
                self._read_shell_reply(kc, msg_id, timeout=2)
                break
        return "".join(parts)

    def _drain_iopub(self, kc) -> None:
        while True:
            try:
                kc.get_iopub_msg(timeout=0.05)
            except queue.Empty:
                return
            except Exception:
                return

    def _read_shell_reply(self, kc, msg_id: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = kc.get_shell_msg(timeout=0.2)
            except queue.Empty:
                continue
            except Exception:
                return
            if reply.get("parent_header", {}).get("msg_id") == msg_id:
                return

    @staticmethod
    def _cap(text: str) -> str:
        cap = config.MAX_STREAM_CHARS
        if cap and len(text) > cap:
            head = text[:cap]
            return head + f"\n[sandbox] ...output truncated at {cap} characters."
        return text

    def add_site_dir(self, site_dir: str, wait: float = 5.0) -> bool:
        """Make ``site_dir`` importable in this live kernel. False if it was busy.

        Bounded rather than blocking: this is called on EVERY live kernel after
        an install, and one of them may be halfway through a long cell. Waiting
        out that cell would spend the whole tool budget refreshing a namespace
        nobody is installing for.
        """
        if not self._lock.acquire(timeout=wait):
            return False
        try:
            if self._km is None or not self._km.is_alive():
                return False  # a kernel started later picks it up from startup
            self._run_silent_locked(_site_dir_code(site_dir), timeout=30)
            return True
        finally:
            self._lock.release()

    def package_versions(self, names: list[str]) -> dict[str, str | None]:
        """Version of each distribution as THIS kernel sees it, None if absent."""
        if not names:
            return {}
        with self._lock:
            if self._km is None or not self._km.is_alive():
                return {n: None for n in names}
            out = self._run_silent_locked(_pkg_probe_code(names), timeout=60)
        for line in out.splitlines():
            if line.startswith(_PKG_SENTINEL):
                try:
                    return json.loads(line[len(_PKG_SENTINEL):])
                except ValueError:
                    break
        return {n: None for n in names}

    def is_alive(self) -> bool:
        return self._km is not None and self._km.is_alive()


# Kept for the standalone smoke tests (test_core.py), which exercise a single
# kernel directly. The MCP server itself routes through POOL, below.
KERNEL = StatefulKernel()


# --- Per-namespace kernel pool ----------------------------------------------

_MAINT_INTERVAL = 30  # seconds between eviction/replenish sweeps
_NS_ALPHABET = "0123456789abcdef"


def mint_namespace_id() -> str:
    """Short, high-enough-entropy id a (possibly weak) LLM can copy back."""
    return "ns-" + "".join(random.choices(_NS_ALPHABET, k=4))


class KernelPool:
    """Manages one StatefulKernel per namespace id.

    Each namespace = an isolated set of variables/imports (its own kernel), so
    concurrent AnythingLLM conversations cannot collide. Idle kernels are
    evicted after ``config.NS_IDLE_TIMEOUT`` and the pool is capped at
    ``config.MAX_NAMESPACES`` (LRU eviction). A single pre-warmed "reserve"
    kernel is kept so opening a new namespace is usually instant.
    """

    def __init__(self) -> None:
        self._kernels: dict[str, StatefulKernel] = {}
        self._last_used: dict[str, float] = {}
        self._reserve: StatefulKernel | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._maintainer: threading.Thread | None = None

    # -- startup -----------------------------------------------------------

    def prewarm(self) -> None:
        """Boot one reserve kernel (surfaces config errors early) and start the
        background maintainer. Safe to call once at server startup."""
        spare = StatefulKernel()
        spare.start()  # may raise -> caller logs and continues lazily
        with self._lock:
            if self._reserve is None:
                self._reserve = spare
            else:
                spare.shutdown()
        self._ensure_maintainer()

    def _ensure_maintainer(self) -> None:
        if self._maintainer is None or not self._maintainer.is_alive():
            self._stop.clear()
            self._maintainer = threading.Thread(
                target=self._maintain_loop, name="kernel-pool-maintainer", daemon=True
            )
            self._maintainer.start()

    # -- routing -----------------------------------------------------------

    def mint(self) -> str:
        with self._lock:
            for _ in range(10000):
                nsid = mint_namespace_id()
                if nsid not in self._kernels:
                    return nsid
        return "ns-" + "".join(random.choices(_NS_ALPHABET + "ghijklmnop", k=8))

    def get(self, ns: str) -> StatefulKernel:
        """Return the kernel for ``ns``, creating it (adopting the reserve when
        available) if needed."""
        with self._lock:
            kernel = self._kernels.get(ns)
            if kernel is None or not kernel.is_alive():
                self._evict_over_cap_locked()
                kernel = self._acquire_kernel_locked()
                self._kernels[ns] = kernel
            self._last_used[ns] = time.monotonic()
            self._ensure_maintainer()
            return kernel

    def reset(self, ns: str) -> bool:
        """Restart a namespace's kernel (clears its variables). Returns True if
        the namespace existed."""
        with self._lock:
            kernel = self._kernels.get(ns)
            if kernel is None:
                return False
            kernel.restart()
            self._last_used[ns] = time.monotonic()
            return True

    def add_site_dir_everywhere(self, site_dir: str) -> list[str]:
        """Make a just-installed package importable in every live kernel.

        Otherwise only the namespace that ran the install would see it until its
        kernel restarted -- and restarting is exactly what the other
        conversations cannot afford, since it drops their loaded data.

        Returns the namespaces that were too busy to refresh; they pick the
        directory up when their kernel next starts.
        """
        with self._lock:
            pairs = list(self._kernels.items())
            reserve = self._reserve
        busy: list[str] = []
        for name, kernel in pairs:
            try:
                if not kernel.add_site_dir(site_dir):
                    busy.append(name)
            except Exception:  # noqa: BLE001 - one bad kernel must not block the rest
                busy.append(name)
        if reserve is not None:
            try:
                reserve.add_site_dir(site_dir)
            except Exception:  # noqa: BLE001
                pass
        return busy

    def active_namespaces(self) -> list[str]:
        with self._lock:
            return sorted(self._kernels)

    # -- internals ---------------------------------------------------------

    def _acquire_kernel_locked(self) -> StatefulKernel:
        # Adopt the pre-warmed reserve if it is ready (instant); otherwise boot
        # a fresh kernel now (blocks this call ~2.5s -- acceptable for a new
        # conversation on a single-user host).
        if self._reserve is not None and self._reserve.is_alive():
            kernel = self._reserve
            self._reserve = None
            return kernel
        kernel = StatefulKernel()
        kernel.start()
        return kernel

    def _evict_over_cap_locked(self) -> None:
        while len(self._kernels) >= config.MAX_NAMESPACES and self._last_used:
            lru = min(self._last_used, key=self._last_used.get)
            self._shutdown_ns_locked(lru)

    def _shutdown_ns_locked(self, ns: str) -> None:
        kernel = self._kernels.pop(ns, None)
        self._last_used.pop(ns, None)
        if kernel is not None:
            try:
                kernel.shutdown()
            except Exception:
                pass

    def _maintain_loop(self) -> None:
        while not self._stop.wait(_MAINT_INTERVAL):
            # 1. Evict idle namespaces (quick, under lock).
            with self._lock:
                now = time.monotonic()
                idle = [
                    ns for ns, t in self._last_used.items()
                    if now - t > config.NS_IDLE_TIMEOUT
                ]
                for ns in idle:
                    self._shutdown_ns_locked(ns)
                need_reserve = self._reserve is None or not self._reserve.is_alive()
            # 2. Replenish the reserve OUTSIDE the lock (booting is slow).
            if need_reserve and not self._stop.is_set():
                try:
                    spare = StatefulKernel()
                    spare.start()
                except Exception:
                    spare = None
                if spare is not None:
                    with self._lock:
                        if self._reserve is None or not self._reserve.is_alive():
                            self._reserve = spare
                        else:
                            spare.shutdown()

    def shutdown_all(self) -> None:
        self._stop.set()
        with self._lock:
            for ns in list(self._kernels):
                self._shutdown_ns_locked(ns)
            if self._reserve is not None:
                try:
                    self._reserve.shutdown()
                except Exception:
                    pass
                self._reserve = None


# Process-wide pool the MCP server routes every execution through.
POOL = KernelPool()
