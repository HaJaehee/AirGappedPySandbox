# 02 — Component Reference

Module-by-module map of the codebase. Paths are repo-root relative; a synced
copy of each lives under `dist/AirGappedPySandbox/mcp-server/` (see `04`).

> **Editing rule:** change the repo-root file first, then copy it into
> `dist/AirGappedPySandbox/mcp-server/`. The two must stay identical.

## `config.py` — settings (env-overridable)

All configuration in one place. Reads env vars, falls back to sane defaults.

| Symbol | Env var | Default | Meaning |
|--------|---------|---------|---------|
| `PROJECT_ROOT` | — | dir of `config.py` | Anchor for a *relative* `SANDBOX_WORKSPACE` and for chat-link paths. |
| `WORKSPACE_DIR` | `SANDBOX_WORKSPACE` | `PROJECT_ROOT/workspace` | Isolated I/O dir, **and the kernel's working directory**. A relative env value resolves against `PROJECT_ROOT`, not the host's cwd. |
| `KERNEL_PYTHON` | `SANDBOX_KERNEL_PYTHON` | `sys.executable` | Interpreter that runs the kernel (the portable Python). |
| `TOOL_BUDGET` | `SANDBOX_TOOL_BUDGET` | `45` | **How long one tool call may block before it must answer something.** Must stay under the MCP client's own timeout (60s in the official SDK). |
| `EXEC_TIMEOUT` | `SANDBOX_EXEC_TIMEOUT` | `45` | Per-call timeout (s). Kept under `TOOL_BUDGET`; was 60, which raced the client's 60s timeout. |
| `STARTUP_TIMEOUT` | `SANDBOX_STARTUP_TIMEOUT` | `60` | Kernel boot timeout (s). |
| `MAX_STREAM_CHARS` | `SANDBOX_MAX_STREAM_CHARS` | `20000` | Cap on returned stdout/stderr (0 = unlimited). |
| `MAX_WRITE_BYTES` | `SANDBOX_MAX_WRITE_BYTES` | `1048576` | Cap on one `write_workspace_file` call (0 = unlimited). |
| `VENV_DIR` | `SANDBOX_VENV` | `PROJECT_ROOT/.sandbox-venv` | Where `install_python_packages` puts packages. **Never inside the workspace** — the artifact interceptor diffs that tree and would report every installed file. |
| `PIP_INDEX_URL` | `SANDBOX_PIP_INDEX_URL` | *(unset)* | The intranet PyPI mirror. Unset means pip falls back to pip.ini / the public PyPI, i.e. installs fail on a closed network. |
| `PIP_TRUSTED_HOST` | `SANDBOX_PIP_TRUSTED_HOST` | *(unset)* | Needed for a plain-http mirror; without it pip **silently ignores** the index. |
| `PIP_TIMEOUT` | `SANDBOX_PIP_TIMEOUT` | `300` | Ceiling on one install (s). |
| `PIP_NETWORK_TIMEOUT` | `SANDBOX_PIP_NETWORK_TIMEOUT` | `15` | How long pip waits on the index (s). |
| `PIP_RETRIES` | `SANDBOX_PIP_RETRIES` | `0` | pip retries. 0 so a misconfigured host fails in a second, not minutes. |
| `VENV_CREATE_TIMEOUT` | `SANDBOX_VENV_CREATE_TIMEOUT` | `180` | Ceiling on creating the venv (s). |
| `MAX_PACKAGES_PER_CALL` | `SANDBOX_MAX_PACKAGES_PER_CALL` | `20` | Bounds one install call. |
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
    `01`). Runs kernel with `cwd=ensure_workspace()`, so a plain relative path
    in executed code lands where every tool looks.
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

## `pip_installer.py` — installs from the intranet mirror

Everything `install_python_packages` needs, and nothing that touches MCP.

- **`parse_requests(raw)`** — splits a free-form string on commas/whitespace and
  keeps only PEP 508 requirement specifiers. URLs, paths, `-e` and pip flags are
  returned as *rejected*: the model chooses packages, never pip's arguments.
- **`ensure_venv()` / `site_packages_dir()`** — the venv is created from
  `KERNEL_PYTHON` with `--system-site-packages`, so the curated 4 GB stack stays
  visible and pip does not re-download it. `site_packages_dir()` resolves by
  directory layout, not by running the interpreter: it is called on every kernel
  start-up.
- **`effective_index()`** — the URL pip will *really* use: `SANDBOX_PIP_INDEX_URL`,
  else `pip config list`, else `PIP_INDEX_URL`, else the public PyPI. Reporting
  that URL is usually enough for a user to spot an unconfigured host.
- **`start_or_attach(specs)` / `collect(job)`** — installs run on a background
  thread, keyed by the sorted spec list. A repeat call with the same packages
  *joins* the running job rather than launching a second pip, which is what
  makes "call again to collect the result" safe to tell a model. `collect()`
  removes a finished job so the same install can be run again later.
- **`probe_index(url)` / `diagnose(report)`** — **read this before touching the
  failure paths.** With `PIP_RETRIES=0`, pip prints *nothing* about an
  unreachable index: a mirror that does not resolve and a package that is not on
  the mirror both come back as "No matching distribution found". So `diagnose`
  stops reading pip's text and asks the index itself. Three outcomes are
  distinguished, each naming the setting to fix: index unreachable, index
  silently dropped because it is plain http and untrusted, and index reachable
  but without that package.

## `artifacts.py` — the artifact interceptor

- **`snapshot(directory=WORKSPACE_DIR) -> dict[str, (mtime, size)]`** — dependency-
  free recursive file snapshot; missing dir → empty dict.
- **`diff(before, after, project_root) -> list[Artifact]`** — files new or
  changed between snapshots, newest first.
- **`Artifact`** dataclass — `path, rel_path (POSIX), size, is_image`;
  `to_markdown()` → `![name](./workspace/..)` for images else `[name](..)`.
- **`chat_link_path(path, project_root)`** — the link path used in responses.
  Project-relative normally; when `SANDBOX_WORKSPACE` points outside the project
  it falls back to `workspace/<name>` instead of leaking an absolute path (which
  `to_markdown` would have turned into a dead `./D:/...` link).

Rationale: a snapshot/diff is more predictable inside an air-gapped host than a
filesystem-watcher thread.

## `server.py` — the FastMCP wrapper

- `mcp = FastMCP("air-gapped-python-sandbox")`.
- `_RULES` — the usage rules string injected into tool descriptions (no pip,
  always `print()`, save plots not `show()`, plain relative paths — the cwd *is*
  the workspace — self-correct).
  Lists the full pre-installed library set.
- `_format_response(result, artifacts)` — renders the text block returned to the
  LLM (see `01`, step 6).
- `_resolve_workspace_file(file_path) -> (Path|None, err|None)` — resolves a user
  path to a real **existing** file inside the workspace; rejects escapes. Used by
  `run_python_file`.
- `_resolve_new_workspace_path(filename) -> (Path|None, err|None)` — the same job
  for a file that does **not exist yet**, so containment is checked on the parent
  after `resolve()`. Rejects absolute paths, `..`, Windows reserved device names,
  and anything landing outside the workspace; strips a leading `./workspace/`.
  Used by `write_workspace_file`.
- `_display_paths(target) -> (link_path, workspace_relative_path)` — formats a
  target for the response; via `chat_link_path` it keeps the `workspace/<name>`
  shape even when `SANDBOX_WORKSPACE` points outside `PROJECT_ROOT` (same rule as
  `artifacts.diff`).
- Tools: `execute_python_code`, `run_python_file`, `write_workspace_file`,
  `list_workspace_files`, `reset_kernel_state` (see `03`).
- `main()` — `ensure_workspace()`, warm-start `KERNEL.start()` unless
  `SANDBOX_LAZY_START` is truthy (failures are logged, not fatal), then
  `mcp.run()` (stdio transport).

## `check_environment.py` — pre-flight

Runs against `config.KERNEL_PYTHON`. Probes REQUIRED + OPTIONAL packages via a
subprocess `importlib.util.find_spec`, then actually boots a kernel and runs one
statement. Exit 0 = `RESULT: PASS`. REQUIRED misses are fatal — the base stack
belongs in the portable Python, not in the mirror, which may be unreachable when
it matters. It also reports the effective package index and whether this machine
can reach it (a warning, never fatal).

## `test_core.py` — smoke tests (no MCP needed)

36 checks. The first 14 run against `KERNEL` + artifacts directly: stdout,
statefulness, errors, library import, artifact detection (txt + png),
timeout+recovery. These intentionally do **not** import `server`/`mcp` so they
run on a minimal interpreter.

`check_write_workspace_file()` adds 22 pure-function checks (path containment,
reserved names, overwrite policy, LF preservation, size cap, the non-`.py`
`run_python_file` refusal). It needs `server` — and therefore `mcp` — so the
import is guarded: a missing SDK prints `[SKIP]` rather than failing, preserving
the minimal-interpreter property above. It cleans up every file it creates.
