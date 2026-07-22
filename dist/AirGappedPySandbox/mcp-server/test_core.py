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
    print()
    if check.failed:
        print(f"{check.failed} check(s) FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
