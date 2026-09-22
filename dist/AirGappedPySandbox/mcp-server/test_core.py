"""Smoke tests for the stateful kernel + artifact interceptor (no MCP needed).

Run with the dev venv:  ./.devvenv/Scripts/python.exe test_core.py
"""

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import config
from artifacts import chat_link_path, diff, snapshot
from kernel_manager import KERNEL


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        check.failed += 1
check.failed = 0


def check_write_workspace_file():
    """Path-safety and overwrite policy for write_workspace_file.

    These import ``server`` (and therefore ``mcp``), which the rest of this file
    deliberately avoids so it can run on a minimal interpreter. Skipped, not
    failed, when the MCP SDK is absent.
    """
    try:
        import server
    except ImportError as exc:
        print(f"[SKIP] write_workspace_file checks (server import failed: {exc})")
        return

    ws = config.ensure_workspace()
    made = []

    # happy path: file lands in the workspace and comes back as a Markdown link
    out = server.write_workspace_file("_smoke_write.py", "print('hi')\n")
    made.append(ws / "_smoke_write.py")
    check("write: SUCCESS status", out.startswith("write_status: SUCCESS"))
    check("write: file on disk", (ws / "_smoke_write.py").read_text(encoding="utf-8") == "print('hi')\n")
    check("write: artifact link", "[_smoke_write.py](./workspace/" in out)
    check("write: .py suggests run_python_file", "run_python_file" in out)

    # LF preserved verbatim on Windows (newline="\n")
    server.write_workspace_file("_smoke_write.py", "a\nb\n", overwrite=True)
    raw = (ws / "_smoke_write.py").read_bytes()
    check("write: LF not translated to CRLF", b"\r\n" not in raw)

    # overwrite policy
    out = server.write_workspace_file("_smoke_write.py", "x")
    check("write: refuses existing file", "already exists" in out and "ERROR" in out)
    check("write: refusal names the fix", "overwrite=true" in out)
    out = server.write_workspace_file("_smoke_write.py", "replaced\n", overwrite=True)
    check("write: overwrite=True replaces", out.startswith("write_status: SUCCESS")
          and (ws / "_smoke_write.py").read_text(encoding="utf-8") == "replaced\n")

    # ./workspace/ prefix is accepted and stripped (not nested)
    out = server.write_workspace_file("./workspace/_smoke_prefix.txt", "ok")
    made.append(ws / "_smoke_prefix.txt")
    check("write: ./workspace/ prefix stripped", (ws / "_smoke_prefix.txt").is_file())

    # subdirectories are created inside the workspace
    out = server.write_workspace_file("_smoke_dir/nested/note.md", "# hi")
    made.append(ws / "_smoke_dir" / "nested" / "note.md")
    check("write: creates subdirectories", (ws / "_smoke_dir" / "nested" / "note.md").is_file())

    # containment: every escape attempt is refused and writes nothing
    for bad in ("../escape.py", "..\\escape.py", "sub/../../escape.py",
                "C:/Windows/Temp/escape.py", "/etc/passwd"):
        out = server.write_workspace_file(bad, "nope")
        check(f"write: refuses {bad!r}", out.startswith("write_status: ERROR"))
    check("write: nothing escaped the workspace",
          not (config.PROJECT_ROOT / "escape.py").exists())

    # Windows reserved device names
    for bad in ("NUL", "con.txt", "sub/LPT1.log"):
        out = server.write_workspace_file(bad, "nope")
        check(f"write: refuses reserved name {bad!r}", "reserved device name" in out)

    # empty filename
    check("write: refuses empty filename",
          server.write_workspace_file("   ", "x").startswith("write_status: ERROR"))

    # size cap
    original = config.MAX_WRITE_BYTES
    config.MAX_WRITE_BYTES = 10
    out = server.write_workspace_file("_smoke_big.txt", "x" * 50)
    config.MAX_WRITE_BYTES = original
    check("write: enforces size cap", "over the" in out and "ERROR" in out)

    # run_python_file rejects non-.py before touching the kernel
    server.write_workspace_file("_smoke_notpy.txt", "data")
    made.append(ws / "_smoke_notpy.txt")
    out = server.run_python_file("_smoke_notpy.txt", namespace="_smoke_ns")
    check("run_python_file: rejects non-.py", "is not a .py file" in out)

    for path in made:
        try:
            path.unlink()
        except OSError:
            pass
    try:
        (ws / "_smoke_dir" / "nested").rmdir()
        (ws / "_smoke_dir").rmdir()
    except OSError:
        pass


def check_external_workspace():
    """Path handling when SANDBOX_WORKSPACE points outside the project.

    That is the configuration the setting exists for -- one workspace shared
    with a sibling filesystem MCP server, say -- and it was the one that broke.
    These are pure-function checks: no kernel, no MCP.
    """
    import tempfile

    external = Path(tempfile.mkdtemp(prefix="sandbox-ext-ws-"))
    original = config.WORKSPACE_DIR
    try:
        config.WORKSPACE_DIR = external

        # A relative SANDBOX_WORKSPACE must anchor at the project root, not at
        # whatever cwd the host happened to spawn the server with.
        anchored = config._env_path("SANDBOX_WORKSPACE", external)
        os.environ["SANDBOX_WORKSPACE"] = "./relative-ws"
        try:
            anchored = config._env_path("SANDBOX_WORKSPACE", external)
        finally:
            os.environ.pop("SANDBOX_WORKSPACE", None)
        check("relative SANDBOX_WORKSPACE anchors at project root",
              anchored == (config.PROJECT_ROOT / "relative-ws").resolve(), anchored)

        # Chat links stay workspace-relative instead of leaking an absolute path.
        link = chat_link_path(external / "chart.png", config.PROJECT_ROOT)
        check("external artifact link is not absolute",
              link == "workspace/chart.png", link)

        try:
            import server
        except ImportError as exc:
            print(f"[SKIP] external workspace: server import failed ({exc})")
            return

        (external / "sample.py").write_text("print('ran')", encoding="utf-8")
        for given in ("sample.py", "./workspace/sample.py", "workspace/sample.py"):
            resolved, err = server._resolve_workspace_file(given)
            check(f"external: resolves {given!r}",
                  resolved == (external / "sample.py").resolve(), resolved or err)

        # A same-named file in the project's own ./workspace must not win, and
        # must not produce a bogus "resolves outside the workspace" refusal.
        stale_dir = config.PROJECT_ROOT / "workspace"
        stale_dir.mkdir(exist_ok=True)
        stale = stale_dir / "sample.py"
        stale.write_text("print('STALE')", encoding="utf-8")
        try:
            resolved, err = server._resolve_workspace_file("./workspace/sample.py")
            check("external: stale project-local file does not win",
                  resolved == (external / "sample.py").resolve(), resolved or err)
        finally:
            stale.unlink(missing_ok=True)
    finally:
        config.WORKSPACE_DIR = original
        shutil.rmtree(external, ignore_errors=True)


def check_execution_verdicts():
    """"It ran" must not come back as "it worked".

    Each case below used to report SUCCESS (or, for sys.exit(0), ERROR) no
    matter what actually happened. Uses the live KERNEL, so call before it is
    shut down.
    """
    try:
        import server
    except ImportError as exc:
        print(f"[SKIP] execution verdict checks (server import failed: {exc})")
        return

    cases = [
        ("printed answer", "print('answer', 6 * 7)", "RAN_UNVERIFIED"),
        ("silent cell", "_quiet = 1", "RAN_NO_OUTPUT"),
        ("ERROR log record",
         "import logging; logging.error('database unreachable')",
         "RAN_WITH_ERRORS"),
        ("swallowed traceback",
         "import traceback\n"
         "try:\n"
         "    raise ValueError('boom')\n"
         "except Exception:\n"
         "    traceback.print_exc()\n"
         "print('finished')\n",
         "RAN_WITH_ERRORS"),
        ("background thread crash",
         "import threading\n"
         "def _boom():\n"
         "    raise RuntimeError('bg')\n"
         "_t = threading.Thread(target=_boom)\n"
         "_t.start(); _t.join()\n"
         "print('finished')\n",
         "RAN_WITH_ERRORS"),
        ("subprocess exits non-zero",
         "import subprocess, sys\n"
         "subprocess.run([sys.executable, '-c', 'import sys; sys.exit(3)'])\n"
         "print('return code ignored')\n",
         "RAN_WITH_ERRORS"),
        ("uncaught exception", "1 / 0", "FAILED"),
        ("sys.exit(1)", "import sys; sys.exit(1)", "FAILED"),
        ("sys.exit(0) is not a failure",
         "print('all good')\nimport sys; sys.exit(0)", "RAN_UNVERIFIED"),
    ]
    for name, code, expected in cases:
        r = KERNEL.execute(code, timeout=30)
        got = server._verdict(r, [])[0]
        check(f"verdict: {name} -> {expected}", got == expected,
              f"got {got} (status={r.status}, signals={r.signals})")

    # The SystemExit traceback used to be dumped on the model as an "error".
    r = KERNEL.execute("import sys; sys.exit(0)")
    check("sys.exit(0) leaves stderr clean", "SystemExit" not in r.stderr,
          r.stderr[:160])

    # A ledger entry must not leak into the next call as a phantom failure.
    r = KERNEL.execute("print('clean run')")
    check("incident ledger is drained between calls",
          server._verdict(r, [])[0] == "RAN_UNVERIFIED", f"signals={r.signals}")


def check_run_python_file():
    """Script semantics: sys.exit(0) is a normal ending, and state survives it."""
    try:
        import server
    except ImportError as exc:
        print(f"[SKIP] run_python_file checks (server import failed: {exc})")
        return
    from kernel_manager import POOL

    ws = config.ensure_workspace()
    good = ws / "_smoke_exit0.py"
    good.write_text(
        "def main():\n"
        "    print('all good')\n"
        "    return 0\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    import sys\n"
        "    sys.exit(main())\n",
        encoding="utf-8")
    bad = ws / "_smoke_exit2.py"
    bad.write_text(
        "def main():\n"
        "    return 2\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    import sys\n"
        "    sys.exit(main())\n",
        encoding="utf-8")
    try:
        out = server.run_python_file("_smoke_exit0.py", namespace="_smoke_run")
        check("run_python_file: sys.exit(0) is not a failure",
              out.startswith("execution_status: RAN_UNVERIFIED"), out[:160])
        check("run_python_file: __main__ block ran", "all good" in out)
        probe = server.execute_python_code("print('main' in dir())",
                                           namespace="_smoke_run")
        check("run_python_file: state survives sys.exit()", "True" in probe,
              probe[:200])

        out = server.run_python_file("_smoke_exit2.py", namespace="_smoke_run")
        check("run_python_file: non-zero exit is FAILED",
              out.startswith("execution_status: FAILED"), out[:160])

        out = server.run_python_file("_nope_missing.py", namespace="_smoke_run")
        check("run_python_file: missing file is FAILED",
              out.startswith("execution_status: FAILED"), out[:160])
    finally:
        good.unlink(missing_ok=True)
        bad.unlink(missing_ok=True)
        POOL.shutdown_all()


def check_response_shape():
    """A weak model reads the first and the last line. Both must carry weight."""
    try:
        import server
    except ImportError as exc:
        print(f"[SKIP] response shape checks (server import failed: {exc})")
        return

    class _Fake:
        stdout = "42\n"
        stderr = ""
        status = "OK"
        result_repr = ""
        error_name = ""
        state_reset = False
        exit_code = None
        signals: list = []

    out = server._render("ns-test", "provided", _Fake(), [])
    lines = out.splitlines()
    check("render: verdict is the first line",
          lines[0] == "execution_status: RAN_UNVERIFIED", lines[0])
    check("render: next_action is the last line",
          lines[-1].startswith("next_action:"), lines[-1])
    check("render: the word SUCCESS never appears", "SUCCESS" not in out)
    check("render: namespace still advertised", "active_namespace: ns-test" in out)

    class _Failed(_Fake):
        status = "FAILED"
        stderr = "Traceback (most recent call last): ..."
        stdout = ""

    out = server._render("ns-test", "provided", _Failed(), [])
    check("render: failure verdict leads", out.startswith("execution_status: FAILED"))
    check("render: stderr precedes stdout",
          out.index("--- stderr ---") < out.index("--- stdout ---"))
    check("render: failure closes with an instruction",
          out.splitlines()[-1].startswith("next_action:"))


def check_pip_installer():
    """Package installs, and -- the point of the feature -- honest failures.

    Nothing here downloads a package. The install that runs is aimed at a
    hostname reserved by RFC 2606 to never resolve, which is precisely the
    shape of the failure this has to report well: a machine whose mirror is
    not configured or not reachable.
    """
    import asyncio
    import tempfile

    try:
        import pip_installer
        import server
    except ImportError as exc:
        print(f"[SKIP] pip installer checks (import failed: {exc})")
        return

    def call(packages):
        """install_python_packages is async (see check_install_budget)."""
        return asyncio.run(server.install_python_packages(packages, "_smoke_pip"))

    # 1. Request parsing: names in, everything else out.
    ok, bad = pip_installer.parse_requests("polars, rapidfuzz>=3.0 numpy==2.1")
    check("pip: accepts names, versions, commas and spaces",
          ok == ["polars", "rapidfuzz>=3.0", "numpy==2.1"] and not bad, ok)
    ok, bad = pip_installer.parse_requests(
        "polars --index-url=http://evil -e . ../x.whl https://h/x.tar.gz")
    check("pip: refuses flags, editables, paths and URLs",
          ok == ["polars"] and len(bad) == 5, (ok, bad))
    check("pip: distribution name is extracted from a specifier",
          pip_installer.dist_name("polars[all]>=1.0,<2") == "polars")

    # 2. An index that cannot resolve is NOT reachable, and one that answers is.
    reachable, _ = pip_installer.probe_index(
        "https://sandbox-mirror.invalid/simple", 5)
    check("pip: probe_index reports an unresolvable host as unreachable",
          reachable is False)

    # 3. A configured-but-unreachable mirror must be blamed on the mirror.
    original = (config.VENV_DIR, config.PIP_INDEX_URL, config.PIP_TRUSTED_HOST)
    tmp = Path(tempfile.mkdtemp(prefix="sandbox-test-venv-"))
    try:
        config.VENV_DIR = tmp / "venv"
        config.PIP_INDEX_URL = "https://sandbox-mirror.invalid/simple"
        config.PIP_TRUSTED_HOST = ""
        report = pip_installer.install(["some-internal-lib"])
        check("pip: install against an unreachable mirror fails", not report.ok,
              f"returncode={report.returncode} error={report.error!r}")
        check("pip: the venv is created next to the server, not in the workspace",
              pip_installer.venv_python().is_file())
        why = pip_installer.diagnose(report)
        check("pip: an unreachable mirror is diagnosed as unreachable",
              "cannot reach" in why and "SANDBOX_PIP_INDEX_URL" in why, why[:200])

        # 4. The plain-http trap: pip drops the index and reports "not found".
        fake = pip_installer.InstallReport(
            returncode=1,
            index_url="http://mirror.corp.local/simple",
            index_origin="SANDBOX_PIP_INDEX_URL",
            output="WARNING: The repository located at mirror.corp.local is not "
                   "a trusted or secure host and is being ignored.\n"
                   "ERROR: No matching distribution found for polars",
        )
        why = pip_installer.diagnose(fake)
        check("pip: an ignored http index blames the setting, not the package",
              "SANDBOX_PIP_TRUSTED_HOST" in why and "mirror.corp.local" in why,
              why[:200])

        # 5. With no mirror configured, the reported index is the public PyPI --
        #    which is the clue the user needs when it then fails.
        config.PIP_INDEX_URL = ""
        url, origin = pip_installer.effective_index()
        check("pip: an unconfigured host resolves to a default index",
              url.startswith("http"), (url, origin))
    finally:
        config.VENV_DIR, config.PIP_INDEX_URL, config.PIP_TRUSTED_HOST = original
        shutil.rmtree(tmp, ignore_errors=True)

    # 6. The tool refuses bad input before pip, and before touching a kernel.
    out = call("polars --index-url=http://evil")
    check("install tool: refuses pip flags",
          out.startswith("install_status: FAILED") and "--index-url=http://evil" in out,
          out[:160])
    out = call("   ")
    check("install tool: refuses an empty request",
          out.startswith("install_status: FAILED"), out[:160])
    out = call(" ".join(f"pkg{i}" for i in range(config.MAX_PACKAGES_PER_CALL + 1)))
    check("install tool: enforces the per-call package limit",
          out.startswith("install_status: FAILED") and "limit is" in out, out[:160])
    check("install tool: a refusal still ends with an instruction",
          out.splitlines()[-1].startswith("next_action:"))


def check_install_budget():
    """A slow mirror must not look like a broken one.

    The client cancels a request that outlives its own timeout -- AnythingLLM
    calls that "Context server request timeout" -- so no tool call may block
    for the length of a pip download. Nothing here runs pip: the install is
    replaced by a sleep, because what is under test is the answering, not the
    installing.
    """
    import asyncio
    import importlib.metadata

    try:
        import pip_installer
        import server
        from kernel_manager import POOL
    except ImportError as exc:
        print(f"[SKIP] install budget checks (import failed: {exc})")
        return
    try:
        pinned = f"numpy=={importlib.metadata.version('numpy')}"
    except Exception:  # noqa: BLE001 - numpy missing on this interpreter
        print("[SKIP] install budget checks (numpy not installed)")
        return

    def slow_install(specs):
        time.sleep(4)
        return pip_installer.InstallReport(
            ok=True, returncode=0, seconds=4.0,
            index_url="http://test-mirror/simple", index_origin="test")

    real_install, real_budget = pip_installer.install, config.TOOL_BUDGET
    pip_installer.install, config.TOOL_BUDGET = slow_install, 1
    try:
        started = time.monotonic()
        out = asyncio.run(server.install_python_packages(pinned, "_smoke_budget"))
        elapsed = time.monotonic() - started
        check("install: a slow install answers RUNNING instead of blocking",
              out.startswith("install_status: RUNNING"), f"{out[:120]} ({elapsed:.1f}s)")
        check("install: RUNNING is not phrased as a failure", "NOT a failure" in out)
        check("install: RUNNING says exactly how to collect the result",
              f'packages="{pinned}"' in out, out.splitlines()[-1][:160])

        job = pip_installer.peek([pinned])
        check("install: the install really did carry on in the background",
              job is not None)
        if job is not None:
            job.done.wait(30)

        out = asyncio.run(server.install_python_packages(pinned, "_smoke_budget"))
        check("install: calling back collects the finished result",
              out.startswith("install_status: INSTALLED"), out[:160])
        check("install: the finished job is cleared, so a later install re-runs",
              pip_installer.peek([pinned]) is None)
    finally:
        pip_installer.install, config.TOOL_BUDGET = real_install, real_budget
        POOL.shutdown_all()


def check_busy_kernel_is_not_waited_for():
    """Refreshing sys.path in OTHER namespaces must not spend the tool budget.

    One of them may be halfway through a long cell; waiting that out would
    stall the install for a conversation that is not even installing.
    """
    try:
        from kernel_manager import StatefulKernel
    except ImportError as exc:
        print(f"[SKIP] busy-kernel check (import failed: {exc})")
        return

    kernel = StatefulKernel()  # never started; only its lock matters here
    release = threading.Event()

    def hold_it():
        with kernel._lock:
            release.wait(5)

    holder = threading.Thread(target=hold_it, daemon=True)
    holder.start()
    time.sleep(0.2)
    try:
        started = time.monotonic()
        refreshed = kernel.add_site_dir("C:/does-not-matter", wait=0.5)
        elapsed = time.monotonic() - started
        check("kernel: add_site_dir gives up on a busy kernel", refreshed is False)
        check("kernel: and gives up promptly", elapsed < 2, f"{elapsed:.1f}s")
    finally:
        release.set()
        holder.join(timeout=5)


def main():
    config.ensure_workspace()
    print(f"Kernel python: {config.KERNEL_PYTHON}")
    KERNEL.start()

    # 1. basic stdout
    r = KERNEL.execute("print('hello air-gap')")
    check("stdout captured", "hello air-gap" in r.stdout)
    check("status OK", r.status == "OK")

    # 2. statefulness across calls
    KERNEL.execute("_persist = 21 * 2")
    r = KERNEL.execute("print(_persist)")
    check("state persists across calls", r.stdout.strip() == "42")

    # 3. error handling
    r = KERNEL.execute("1/0")
    check("error status", r.status == "FAILED")
    check("error name captured", r.error_name == "ZeroDivisionError")
    check("traceback in stderr", "ZeroDivisionError" in r.stderr)

    # 4. libraries present in the kernel interpreter
    r = KERNEL.execute("import numpy, pandas; print(pandas.__version__)")
    check("pandas importable in kernel", r.status == "OK" and r.stdout.strip())

    # 5. artifact interception: create a file, ensure diff finds it
    before = snapshot()
    KERNEL.execute(
        "open('_smoke_artifact.txt','w').write('generated')"
    )
    after = snapshot()
    arts = diff(before, after, config.PROJECT_ROOT)
    names = [a.path.name for a in arts]
    check("new artifact detected", "_smoke_artifact.txt" in names)
    if arts:
        md = next(a.to_markdown() for a in arts if a.path.name == "_smoke_artifact.txt")
        check("artifact markdown link", md.startswith("[_smoke_artifact.txt](./workspace/"))

    # 6. matplotlib png artifact (image markdown)
    before = snapshot()
    r = KERNEL.execute(
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1,2,3],[3,1,2]); plt.title('t')\n"
        "plt.savefig('_smoke_chart.png'); plt.close()\n"
        "print('saved')"
    )
    after = snapshot()
    arts = diff(before, after, config.PROJECT_ROOT)
    png = [a for a in arts if a.path.name == "_smoke_chart.png"]
    check("png chart created", bool(png) and r.status == "OK")
    if png:
        check("image markdown uses ![]", png[0].to_markdown().startswith("!["))

    # 7. timeout handling (short timeout so the test stays fast)
    t0 = time.monotonic()
    r = KERNEL.execute("import time; time.sleep(30)", timeout=3)
    elapsed = time.monotonic() - t0
    check("timeout status", r.status == "TIMEOUT")
    # Budget: timeout(3) + interrupt grace(5) + kernel restart. Well under the
    # 30s the cell asked for; generous enough for a slower restart on WinPython.
    check("timeout returns promptly", elapsed < 20)

    # 8. kernel still usable after a timeout/interrupt
    r = KERNEL.execute("print('alive after timeout')")
    check("kernel survives timeout", "alive after timeout" in r.stdout)

    # 9. the kernel works INSIDE the workspace, wherever that is.
    #    The kernel used to be started with cwd=PROJECT_ROOT, so a
    #    relative path in executed code went to the server's own directory as
    #    soon as SANDBOX_WORKSPACE pointed anywhere else -- while every tool
    #    kept using SANDBOX_WORKSPACE. Files written by the model vanished.
    r = KERNEL.execute("import os; print(os.getcwd())")
    check("kernel cwd is the workspace",
          Path(r.stdout.strip()).resolve() == config.WORKSPACE_DIR.resolve(),
          )

    # 10. execution verdicts: failures that never raise must not read as success.
    check_execution_verdicts()

    KERNEL.shutdown()

    # 11. write_workspace_file: pure-function checks, no kernel needed.
    check_write_workspace_file()

    # 12. path handling when the workspace lives outside the project.
    check_external_workspace()

    # 13. response shape (pure) and script semantics (boots a pooled kernel).
    check_response_shape()
    check_run_python_file()

    # 14. package installs and, above all, how their failures are reported.
    check_pip_installer()

    # 15. answering inside the MCP client's timeout.
    check_busy_kernel_is_not_waited_for()
    check_install_budget()
    print()
    if check.failed:
        print(f"{check.failed} check(s) FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
