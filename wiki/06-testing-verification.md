# 06 — Testing & Verification

## Ground rule

Verify on the **real WinPython** (`dist/AirGappedPySandbox/WPy64-313130/python/
python.exe`), not only the dev venv. The dev venv proves logic; WinPython proves
the actual shipped runtime.

## 1. Core smoke tests — `test_core.py` (105 checks)

Kernel + artifact layer, no MCP needed.

```bash
# from repo root, dev venv:
./.devvenv/Scripts/python.exe test_core.py
# or on WinPython, from the package mcp-server dir:
../WPy64-313130/python/python.exe test_core.py
```

Covers: stdout capture, status SUCCESS, **state persistence across calls**, error
status + name + traceback, library import in kernel, artifact detection (txt +
png with `![]` image markdown), timeout status, prompt return, **kernel survives
timeout**. Last run: **14/14 PASS** on WinPython 3.13.

> Note: the "timeout returns promptly" check bound was relaxed from `<10s` to
> `<20s` because WinPython's restart-recovery path (interrupt + 5 s grace +
> relaunch) legitimately exceeds 10 s. This is expected, not a regression.

## 2. Environment pre-flight — `check_environment.py`

```bash
<winpython> check_environment.py
```
Probes REQUIRED + OPTIONAL packages, then boots a kernel and runs one statement.
Last run: **RESULT: PASS** (all required + all optional present, kernel boots).

## 3. `run_python_file` verification (8 checks)

Ad-hoc script exercised via `mcp.call_tool`. Covered: happy path + stdout,
SUCCESS status, `__main__` artifact capture, **state persistence to next call**,
`./workspace/` prefix accepted, **out-of-workspace path rejected**, missing-file
error, runtime-error traceback. Last run: **8/8 PASS** on both dev venv and
WinPython. (The throwaway driver lived in the scratchpad, not committed. Recreate
from `03`'s recipe if needed.)

## 4. Offline capability run (representative workloads)

One `execute_python_code` payload on WinPython exercised the real promised
features, all SUCCESS:
- sympy: ∫x·sin(x)dx = `-x·cos(x)+sin(x)`
- pandas+openpyxl xlsx round-trip
- matplotlib → png artifact
- reportlab → PDF, then read back with **pdfplumber** and **PyMuPDF** (`Hello
  Air-Gap 42`)
- python-docx → .docx
- polars sum, duckdb `select 40+2`
- numpy 2.4.4 / scipy 1.17.1

This is the evidence that the package is genuinely usable offline.

## 5. Warm-start timing (WinPython, measured)

| Step | Time |
|------|------|
| kernel boot (warm-start cost) | ~2.5 s |
| trivial call after boot | ~0.07 s |
| first `import pandas` | ~1.1 s |
| cached re-import | ~0.06 s |

## What is NOT covered by automated tests (gaps)

- The offline capability run (§4) is still ad-hoc, not committed.
  **Candidate improvement:** add `test_server.py` using `mcp.call_tool` — but
  keep it separate from `test_core.py` so `test_core` stays MCP-free.
  (`run_python_file` and the execution verdicts ARE committed now: `test_core.py`
  calls `server.*` directly and skips those checks when the MCP SDK is absent.)
- **No test installs a package for real.** `check_pip_installer()` covers
  request parsing, the venv, and every failure path (it points pip at a
  hostname RFC 2606 reserves to never resolve), but a genuine download from the
  company mirror can only be verified on a host that has one. Do that once, by
  hand, when deploying.
- **Nothing tests a failure the sandbox cannot observe.** A model that wraps its
  own code in `try/except` and swallows the exception still gets
  `RAN_UNVERIFIED`; only the Phase 3 `expect` argument in
  `11-execution-verdict-plan.md` would close that, and it is not built.
- No test simulating an actual network cut (relied on reasoning: no runtime
  network calls; loopback only).
- No large-file / long-running performance test.
