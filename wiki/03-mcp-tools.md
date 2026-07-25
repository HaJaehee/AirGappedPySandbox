# 03 — MCP Tools Reference

Four tools are registered (verified order):
`['execute_python_code', 'run_python_file', 'list_workspace_files', 'reset_kernel_state']`.

All tools return a **single text block** (not a typed object). "Return value"
means printed stdout + captured extras, never a Python object. A machine-readable
version of this catalog is in [tool-specs.xml](tool-specs.xml).

> **Every execution tool now takes a mandatory `namespace`** for per-conversation
> isolation — see [09-namespace-routing.md](09-namespace-routing.md). Pass
> `"new"` first; then reuse the echoed `active_namespace: ns-XXXX`. A hidden
> `ctx: Context` is injected by FastMCP (not in the schema) for the future
> `_meta` conversation-id hook.

## `execute_python_code(code: str, namespace: str) -> str`

Run Python in the namespace's stateful kernel. Variables/imports persist across
calls **within that namespace**. Response is wrapped with an `active_namespace:`
banner + trailing reminder.

Returned text block fields:
- `execution_status:` — `SUCCESS` | `ERROR` | `TIMEOUT`
- `--- stdout ---` — everything `print()`ed
- `--- last expression ---` — `repr` of the last expression, **only shown when
  nothing was printed** (design choice in `_format_response`)
- `--- stderr ---` — cleaned traceback / warnings (ANSI stripped)
- `--- artifacts ---` — Markdown links to new/changed `./workspace` files

## `run_python_file(file_path: str, namespace: str) -> str`

Run an existing `.py` file from `./workspace` as a script. Added in commit
`229fb8a`; gained `namespace` with the routing change.

- Executes via `runpy.run_path(resolved, run_name='__main__')` → the file's
  `if __name__ == '__main__':` block **does** run.
- Afterwards the file's top-level names are merged into the kernel namespace, so
  its variables/functions are usable in later `execute_python_code` calls.
- Same output shape as `execute_python_code`.
- **Isolation:** `file_path` is resolved by `_resolve_workspace_file` and must
  land **inside** `./workspace`. Accepts `sample.py`, `./workspace/sample.py`,
  bare names, and absolute paths within the workspace. Paths resolving outside →
  `execution_status: ERROR` ("outside the workspace"); missing file → ERROR
  ("not found").

## `list_workspace_files() -> str`

Lists every file under `./workspace` with size + extension, or a note if empty.
Use it to discover uploaded inputs (PDF/Excel/CSV) before analysing.

## `reset_kernel_state(namespace: str) -> str`

Restarts **one namespace's** kernel, clearing that conversation's in-memory
state. Workspace files are **kept**; other namespaces are untouched. Pass the
`active_namespace` to reset.

## Embedded usage rules (the `_RULES` string)

Injected into `execute_python_code` and `run_python_file` descriptions so the LLM
self-regulates:

1. **Air-gap:** no `pip`/`apt`/`conda`, no network. Full pre-installed library
   list is enumerated in the description.
2. **Print results:** only `print()`ed output is returned.
3. **Plots & reports:** matplotlib → `savefig('./workspace/..png')` + `close()`,
   never `plt.show()`; plotly → `write_html`; save `.pptx/.pdf/.docx/.html` into
   `./workspace/`.
4. **Paths:** read/write under `./workspace/` (relative).
5. **Self-correct:** on stderr error, fix and retry.

## How to add a new tool (recipe for the next AI)

1. Add a `@mcp.tool(description=...)` function in **repo-root** `server.py`. Reuse
   `_format_response` + `snapshot`/`diff` for consistency. Embed `_RULES` if the
   tool executes user code.
2. If it takes a path, validate with `_resolve_workspace_file` (never trust raw
   paths — workspace isolation).
3. Verify via `mcp.call_tool(...)` on the WinPython (see `06`), covering happy
   path + isolation + error cases.
4. Sync `server.py` into `dist/AirGappedPySandbox/mcp-server/`.
5. Update `README.md` (Korean tool list), this file, and `tool-specs.xml`.
