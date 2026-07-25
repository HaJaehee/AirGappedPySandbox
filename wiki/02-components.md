# 02 — Component Reference

Module-by-module map of the codebase. Paths are repo-root relative; a synced
copy of each lives under `dist/AirGappedPySandbox/mcp-server/` (see `04`).

> **Editing rule:** change the repo-root file first, then copy it into
> `dist/AirGappedPySandbox/mcp-server/`. The two must stay identical.

## `config.py` — settings (env-overridable)

All configuration in one place. Reads env vars, falls back to sane defaults.

| Symbol | Env var | Default | Meaning |
|--------|---------|---------|---------|
| `PROJECT_ROOT` | — | dir of `config.py` | Kernel's working dir; makes `./workspace/...` resolve consistently. |
| `WORKSPACE_DIR` | `SANDBOX_WORKSPACE` | `PROJECT_ROOT/workspace` | Isolated I/O dir. |
| `KERNEL_PYTHON` | `SANDBOX_KERNEL_PYTHON` | `sys.executable` | Interpreter that runs the kernel (the portable Python). |
| `EXEC_TIMEOUT` | `SANDBOX_EXEC_TIMEOUT` | `60` | Per-call timeout (s). |
| `STARTUP_TIMEOUT` | `SANDBOX_STARTUP_TIMEOUT` | `60` | Kernel boot timeout (s). |
| `MAX_STREAM_CHARS` | `SANDBOX_MAX_STREAM_CHARS` | `20000` | Cap on returned stdout/stderr (0 = unlimited). |
| `IMAGE_EXTENSIONS` | — | png/jpg/jpeg/gif/svg/webp/bmp | Which artifacts render as inline images. |
| `ensure_workspace()` | — | — | Creates and returns `WORKSPACE_DIR`. |

`SANDBOX_LAZY_START` (read in `server.main`, not config) skips kernel warm-start.

## `kernel_manager.py` — the stateful execution core

- **`StatefulKernel`** — thread-safe wrapper around one long-lived kernel.
  Guarded by an `RLock`; all public methods serialize on it.
  - `start()` — boot + run startup code; idempotent.
  - `execute(code, timeout=None) -> ExecResult` — the main entry. Relaunches a
    dead kernel first, then runs `_execute_locked`.
  - `restart()` / `_restart_locked()` — restart, wiping in-memory state; used by
    `reset_kernel_state` and by the timeout-recovery path.
  - `shutdown()` — stop channels + kill kernel.
  - `_launch_locked()` — the cross-interpreter launch (KernelSpec override, see
    `01`). Runs kernel with `cwd=PROJECT_ROOT`.
  - `_execute_locked()` — sends code, drains iopub filtered by `msg_id`, collects
    stdout/stderr/`result_repr`/`error_name`; on timeout calls
    `_handle_timeout_locked`.
  - `_handle_timeout_locked()` — interrupts; waits up to `_INTERRUPT_GRACE` (5 s)
    for idle; if still wedged, **restarts** the kernel and sets
    `ExecResult.state_reset = True`. (Windows blocking `time.sleep` is not
    interruptible — this is why the restart fallback exists. See `07`.)
  - `_STARTUP_CODE` — run silently after every boot/restart: `makedirs
    workspace`, `matplotlib.use('Agg')`.
- **`ExecResult`** dataclass — `stdout, stderr, status ('SUCCESS'|'ERROR'|
  'TIMEOUT'), result_repr, error_name, state_reset`.
- **`KERNEL`** — process-wide singleton `StatefulKernel()` imported by `server`.

## `artifacts.py` — the artifact interceptor

- **`snapshot(directory=WORKSPACE_DIR) -> dict[str, (mtime, size)]`** — dependency-
  free recursive file snapshot; missing dir → empty dict.
- **`diff(before, after, project_root) -> list[Artifact]`** — files new or
  changed between snapshots, newest first.
- **`Artifact`** dataclass — `path, rel_path (POSIX), size, is_image`;
  `to_markdown()` → `![name](./workspace/..)` for images else `[name](..)`.

Rationale: a snapshot/diff is more predictable inside an air-gapped host than a
filesystem-watcher thread.

## `server.py` — the FastMCP wrapper

- `mcp = FastMCP("air-gapped-python-sandbox")`.
- `_RULES` — the usage rules string injected into tool descriptions (no pip,
  always `print()`, save plots not `show()`, `./workspace/` paths, self-correct).
  Lists the full pre-installed library set.
- `_format_response(result, artifacts)` — renders the text block returned to the
  LLM (see `01`, step 6).
- `_resolve_workspace_file(file_path) -> (Path|None, err|None)` — resolves a user
  path to a real file **inside** the workspace; rejects escapes. Used by
  `run_python_file`.
- Tools: `execute_python_code`, `run_python_file`, `list_workspace_files`,
  `reset_kernel_state` (see `03`).
- `main()` — `ensure_workspace()`, warm-start `KERNEL.start()` unless
  `SANDBOX_LAZY_START` is truthy (failures are logged, not fatal), then
  `mcp.run()` (stdio transport).

## `check_environment.py` — pre-flight

Runs against `config.KERNEL_PYTHON`. Probes REQUIRED + OPTIONAL packages via a
subprocess `importlib.util.find_spec`, then actually boots a kernel and runs one
statement. Exit 0 = `RESULT: PASS`. REQUIRED misses are fatal (no pip on host).

## `test_core.py` — smoke tests (no MCP needed)

14 checks against `KERNEL` + artifacts directly: stdout, statefulness, errors,
library import, artifact detection (txt + png), timeout+recovery. Intentionally
does **not** import `server`/`mcp` so it can run on a minimal interpreter.
