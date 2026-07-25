# 10 — Environment, Paths & State (read first)

## Canonical location

- **Repo root: `D:\AirGappedPySandbox`** (Bash tool `pwd`: `/d/AirGappedPySandbox`).
- ⚠️ `C:\Users\loves\AirGappedPySandbox` was a **junction to D:** used earlier in
  the project; it is now disconnected. Not data loss — everything is on D:. Never
  assume the C: path exists.

## Interpreters available

| Purpose | Path | Version |
|---------|------|---------|
| **Kernel / shipped runtime** | `dist/AirGappedPySandbox/WPy64-313130/python/python.exe` | **3.13.13** (WinPython, all libs installed) |
| Dev/test venv | `.devvenv/Scripts/python.exe` | 3.12 (has mcp+jupyter_client+pandas+numpy+matplotlib) |
| System Python | on PATH | 3.12.10 (and a 3.14 present) |

Running a scratch script that imports project modules: set
`PYTHONPATH=D:/AirGappedPySandbox` (Python puts the *script's* dir on
`sys.path[0]`, not the cwd).

## Git state

- Branch: `main`. Remote: none configured (local repo).
- Commits (newest first):
  - `229fb8a` Add run_python_file tool to execute a workspace .py file directly
  - `b96d962` Add stateful air-gapped Python sandbox MCP server + portable package
  - `198af66` Initial commit
- Git user: `J.Ha`. Working tree essentially clean (see the known
  `dist/.../mcp-server/README.md` CRLF note in `07`).
- `wiki/` was created after `229fb8a` and is **untracked** unless a later commit
  added it.

## Repo-root inventory

```
config.py  kernel_manager.py  artifacts.py  server.py     ← core modules
check_environment.py  test_core.py                        ← verification
requirements-server.txt  requirements-kernel.txt          ← deps
README.md (KO)  BUILD_PORTABLE_PACKAGE.md (KO)  CLAUDE.md (spec)
anythingllm_mcp_config.example.json  start_server.ps1
.gitignore  workspace/.gitkeep
dist/AirGappedPySandbox/…                                 ← the package (see 04)
.devvenv/                                                 ← dev venv (ignored)
wiki/                                                     ← this handoff (EN)
```

## Environment variables (all optional; see `02` for defaults)

`SANDBOX_KERNEL_PYTHON`, `SANDBOX_WORKSPACE`, `SANDBOX_EXEC_TIMEOUT`,
`SANDBOX_STARTUP_TIMEOUT`, `SANDBOX_MAX_STREAM_CHARS`, `SANDBOX_LAZY_START`.

## Platform facts

- OS: Windows 11 Pro. Shells: PowerShell (primary) + Bash (Git Bash / POSIX).
- Console codepage cp949 (Korean) — see the encoding gotcha in `07`.
- Target deployment: fully air-gapped Windows host, no internet, no pip.

## Fast orientation commands

```bash
cd /d/AirGappedPySandbox
git log --oneline
./dist/AirGappedPySandbox/WPy64-313130/python/python.exe --version   # -> 3.13.13
# full offline readiness check:
cd dist/AirGappedPySandbox/mcp-server && ../WPy64-313130/python/python.exe check_environment.py
```
