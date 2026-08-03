# 04 — Build & Packaging

## The two deliverable shapes

1. **Full portable package** (what the user actually ships): a self-contained
   folder holding **both** the portable Python and the server code, transferable
   by USB/zip to the air-gapped host. This is `dist/AirGappedPySandbox/`.
2. **Wheels-only lightweight package** (fallback): when the host already has a
   portable Python, ship just `mcp-server/offline_wheels/` +
   `install_offline.ps1` to add the 3 server deps offline.

## Shipped package layout (`dist/AirGappedPySandbox/`)

```
dist/AirGappedPySandbox/
├─ PACKAGE_INFO.txt              (Korean quick-start/overview)
├─ BUILD_PORTABLE_PACKAGE.md     (Korean build manual)
├─ WPy64-313130/                 (WinPython 3.13 — portable Python; GIT-IGNORED)
│   └─ python/python.exe             * the real interpreter path
└─ mcp-server/                   (server code — matches the manual's mcp-server\)
    ├─ server.py, kernel_manager.py, artifacts.py, config.py
    ├─ check_environment.py, test_core.py
    ├─ requirements-server.txt, requirements-kernel.txt
    ├─ install_offline.ps1, start_server.ps1
    ├─ anythingllm_mcp_config.example.json
    ├─ README.md, .gitignore
    ├─ offline_wheels/           (69 wheels; py311/312/313 win_amd64; GIT-IGNORED)
    └─ workspace/.gitkeep
```

The user has also produced `dist/AirGappedPySandbox.zip` (~3.47 GB downloaded;
GIT-IGNORED via `*.zip`).

## The portable Python: WinPython 3.13 (WPy64-313130)

- Interpreter: `WPy64-313130/python/python.exe` → **Python 3.13.13**.
- It **already has everything installed**: the full kernel stack AND the server
  deps (`mcp`, `jupyter_client`, `ipykernel`). Confirmed by `check_environment`
  (`RESULT: PASS`) and the offline capability run (see `06`).
- Because server deps are present, `install_offline.ps1` is **not required** for
  this package; it's kept only for re-installing into a different interpreter.

### Why ~3.47 GB is fine

This matches the spec's "4 GB" figure — the functional footprint of this
scientific stack is ~3–4 GB. Sufficiency is about *having the right packages and
running with no network*, not size. Both are verified.

## Requirements files

- `requirements-server.txt` — server process deps: `mcp>=1.2.0`,
  `jupyter_client>=8.0`, `ipykernel>=6.0`.
- `requirements-kernel.txt` — kernel data stack (installed into WinPython on the
  online build PC). Current set: pandas, numpy, scipy, sympy, matplotlib,
  seaborn, openpyxl, xlsxwriter, xlrd, lxml, xmltodict, pdfplumber, pypdf,
  PyMuPDF, python-docx, markdown, beautifulsoup4, **python-pptx, reportlab,
  jinja2, plotly, duckdb, polars, pyarrow, Pillow, rapidfuzz, python-dateutil**,
  ipykernel. (The bold ones were added by the user after the initial build; the
  server `_RULES` string and `check_environment.py` OPTIONAL list were updated to
  match.)

## offline_wheels/

69 wheels for the 3 server deps + transitive deps, downloaded for **Python
3.11/3.12/3.13, win_amd64** (multi-version so it installs regardless of the
target's minor version). Compiled wheels present per-version: `pydantic_core`,
`rpds_py`, `pywin32`, `debugpy`, `cffi`. Regenerate on an online PC with:

```powershell
pip download -r requirements-server.txt --only-binary=:all: --platform win_amd64 --python-version 3.13 -d .\offline_wheels
```

## How to REBUILD or REFRESH the package (next-AI recipe)

Full procedure is the Korean [BUILD_PORTABLE_PACKAGE.md](../BUILD_PORTABLE_PACKAGE.md).
Short version, on an **online** Windows PC with matching arch/py-version:

1. Get a portable Python (WinPython recommended — it is genuinely relocatable and
   ships pip). Locate `...\python\python.exe` (`<PPY>`).
2. `& <PPY> -m pip install -r requirements-kernel.txt` (data stack).
3. `& <PPY> -m pip install -r requirements-server.txt` (server deps).
4. Verify offline: disconnect network → `& <PPY> check_environment.py`
   (`RESULT: PASS`) → `& <PPY> test_core.py` (all PASS).
5. Assemble `WPy64-*/` + `mcp-server/` under one folder; zip it.

### If the user asks for a new library (e.g. scikit-learn)

It **must** be added now (online): append to `requirements-kernel.txt`, install
into WinPython, add it to `check_environment.py` OPTIONAL and the `_RULES` list in
`server.py`, re-verify, re-zip. It **cannot** be added on the air-gapped host.

## Dev-only virtualenv

`.devvenv/` at repo root is a throwaway venv (has `mcp`+`jupyter_client`) used for
fast local testing during development. Git-ignored; safe to delete/recreate:
`python -m venv .devvenv && .devvenv/Scripts/python -m pip install -r requirements-server.txt pandas numpy matplotlib`.
