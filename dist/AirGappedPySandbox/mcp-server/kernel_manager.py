"""Stateful execution core.

Wraps a single long-lived IPython kernel (via ``jupyter_client``) so that
variables, imported modules and parsed dataframes persist across separate
``execute_python_code`` tool calls. The kernel is launched with a configurable
interpreter (``config.KERNEL_PYTHON``) so the heavy data-science libraries can
live in the 4 GB portable Python while the MCP server runs anywhere.

Timeout policy: an over-running cell is interrupted. If the kernel returns to
idle it is reused (state preserved). If it stays wedged past a grace period --
which happens on Windows for blocking C calls such as ``time.sleep`` that the
interrupt event cannot break into -- the kernel is restarted so the next call
always gets a responsive kernel. A restart clears in-memory state and this is
reported to the caller.
"""

from __future__ import annotations

import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field

from jupyter_client.kernelspec import KernelSpec
from jupyter_client.manager import KernelManager

import config

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
    status: str = "SUCCESS"  # SUCCESS | ERROR | TIMEOUT
    result_repr: str = ""    # text/plain of the last expression, if any
    error_name: str = ""
    state_reset: bool = False  # True if the kernel was restarted this call
    fields: dict = field(default_factory=dict)


# Code run once, silently, right after the kernel boots (and after any restart).
# Forces a headless matplotlib backend and guarantees the workspace directory
# exists so the LLM's "./workspace/..." paths always resolve.
_STARTUP_CODE = """
import os as _os
_os.makedirs('workspace', exist_ok=True)
try:
    import matplotlib as _mpl
    _mpl.use('Agg')
except Exception:
    pass
"""


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
        # Run the kernel with the project root as its working directory so that
        # relative "./workspace/..." paths behave identically everywhere.
        km.start_kernel(cwd=str(config.PROJECT_ROOT))
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
        self._run_silent_locked(_STARTUP_CODE, timeout=config.STARTUP_TIMEOUT)

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
                result.status = "ERROR"
                result.error_name = content.get("ename", "")
                tb = "\n".join(content.get("traceback", []))
                stderr_parts.append(_strip_ansi(tb))
            elif mtype == "status" and content.get("execution_state") == "idle":
                self._read_shell_reply(kc, msg_id, timeout=5)
                break

        result.stdout = self._cap(_strip_ansi("".join(stdout_parts)))
        result.stderr = self._cap(_strip_ansi("".join(stderr_parts)))
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

    def _run_silent_locked(self, code: str, timeout: int) -> None:
        """Execute code and drain to idle, discarding output. Lock held."""
        kc = self._kc
        self._drain_iopub(kc)
        msg_id = kc.execute(code, allow_stdin=False, store_history=False)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = kc.get_iopub_msg(timeout=0.3)
            except queue.Empty:
                continue
            except Exception:
                return
            if (
                msg["msg_type"] == "status"
                and msg["content"].get("execution_state") == "idle"
                and msg.get("parent_header", {}).get("msg_id") == msg_id
            ):
                self._read_shell_reply(kc, msg_id, timeout=2)
                return

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
