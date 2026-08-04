"""Smoke tests for the stateful kernel + artifact interceptor (no MCP needed).

Run with the dev venv:  ./.devvenv/Scripts/python.exe test_core.py
"""

import sys
import time

import config
from artifacts import diff, snapshot
from kernel_manager import KERNEL


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
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


def main():
    config.ensure_workspace()
    print(f"Kernel python: {config.KERNEL_PYTHON}")
    KERNEL.start()

    # 1. basic stdout
    r = KERNEL.execute("print('hello air-gap')")
    check("stdout captured", "hello air-gap" in r.stdout)
    check("status SUCCESS", r.status == "SUCCESS")

    # 2. statefulness across calls
    KERNEL.execute("_persist = 21 * 2")
    r = KERNEL.execute("print(_persist)")
    check("state persists across calls", r.stdout.strip() == "42")

    # 3. error handling
    r = KERNEL.execute("1/0")
    check("error status", r.status == "ERROR")
    check("error name captured", r.error_name == "ZeroDivisionError")
    check("traceback in stderr", "ZeroDivisionError" in r.stderr)

    # 4. libraries present in the kernel interpreter
    r = KERNEL.execute("import numpy, pandas; print(pandas.__version__)")
    check("pandas importable in kernel", r.status == "SUCCESS" and r.stdout.strip())

    # 5. artifact interception: create a file, ensure diff finds it
    before = snapshot()
    KERNEL.execute(
        "open('workspace/_smoke_artifact.txt','w').write('generated')"
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
        "plt.savefig('workspace/_smoke_chart.png'); plt.close()\n"
        "print('saved')"
    )
    after = snapshot()
    arts = diff(before, after, config.PROJECT_ROOT)
    png = [a for a in arts if a.path.name == "_smoke_chart.png"]
    check("png chart created", bool(png) and r.status == "SUCCESS")
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

    KERNEL.shutdown()

    # 9. write_workspace_file: pure-function checks, no kernel needed.
    check_write_workspace_file()
    print()
    if check.failed:
        print(f"{check.failed} check(s) FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
