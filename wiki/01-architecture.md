# 01 — Architecture

## Layered view

```
AnythingLLM Agent  (the MCP client; the LLM decides to call a tool)
      │  MCP protocol over stdio
      ▼
server.py ── FastMCP safety & artifact layer ──────────────────────┐
      │   • tools: execute_python_code, run_python_file,             │
      │            list_workspace_files, reset_kernel_state          │
      │   • workspace snapshot → Markdown artifact links (artifacts) │
      │   • air-gap usage rules embedded in tool descriptions        │
      ▼                                                              │
kernel_manager.py ── StatefulKernel (jupyter_client) ───────────────┘
      │  ZeroMQ over 127.0.0.1 (loopback — works with no network)
      ▼
IPython kernel  ← launched on the PORTABLE Python (WinPython 3.13)
      (pandas, numpy, scipy, sympy, pdfplumber, PyMuPDF, matplotlib,
       openpyxl, lxml, python-docx, python-pptx, polars, duckdb, ...)
```

## The key architectural decision: two decoupled interpreters

The MCP **server process** and the **kernel process** can run on *different*
Python interpreters:

- **Server interpreter** needs only `mcp`, `jupyter_client`, `ipykernel`.
- **Kernel interpreter** (`config.KERNEL_PYTHON`, env `SANDBOX_KERNEL_PYTHON`)
  holds the heavy data-science stack — the 40 GB-class portable Python.

**Why it matters:** the server stays lightweight and you can point it at any
pre-built distribution without reinstalling. It is implemented in
`kernel_manager._launch_locked()` by constructing a `jupyter_client.kernelspec.
KernelSpec` whose `argv` is `[KERNEL_PYTHON, -m, ipykernel_launcher, -f,
{connection_file}]` and assigning it to `KernelManager._kernel_spec` (overriding
the globally-installed kernelspec).

> In the shipped package both roles are the **same** WinPython (it already has
> `mcp`+`jupyter_client`+`ipykernel` installed), so `SANDBOX_KERNEL_PYTHON`
> defaults to `sys.executable` and nothing extra is needed.

## Why a persistent kernel (statefulness)

`subprocess.run(["python", ...])` resets state every call, forcing a 100 MB
Excel or multi-page PDF to be re-parsed on every follow-up question. Instead an
embedded IPython kernel keeps variables, imports, and dataframes **in memory
across tool calls**, giving low latency on follow-ups.

## Data flow of a single `execute_python_code` call

1. `server.execute_python_code(code)` → `config.ensure_workspace()`.
2. `snapshot()` the workspace (path → (mtime, size)) — the "before" set.
3. `KERNEL.execute(code, timeout)` runs the code in the persistent kernel,
   collecting iopub messages (stdout/stderr streams, `execute_result` repr,
   `error` traceback) filtered by the request's `msg_id`, until the kernel
   returns to `idle` (or the timeout fires).
4. `snapshot()` again — the "after" set.
5. `artifacts.diff(before, after)` → any new/changed workspace files become
   `Artifact` objects; images render as `![](...)`, others as `[](...)`.
6. `_format_response()` assembles one text block:
   `execution_status`, `--- stdout ---`, optional `--- last expression ---`
   (only when nothing was printed), `--- stderr ---`, `--- artifacts ---`.
7. That text is returned to the LLM via MCP.

`run_python_file` is the same flow, except step 3 runs
`runpy.run_path(<resolved>, run_name='__main__')` and then merges the file's
public names into the kernel namespace (see `03`).

## Isolation & safety model

This is a **sandbox by convention, not a hardened jail** — code runs with the
server process's OS privileges. Mitigations: workspace-relative I/O, a per-call
execution timeout with auto-recovery, an output-size cap
(`SANDBOX_MAX_STREAM_CHARS`), path-escape rejection in `run_python_file`, and the
air-gap itself. For stronger isolation, run the server in a container or a
restricted OS user. See `07` for details.
