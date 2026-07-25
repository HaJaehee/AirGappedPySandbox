"""Air-Gapped Python Sandbox -- MCP server.

Exposes a stateful Python code interpreter to an LLM (via AnythingLLM's MCP
agent support) that runs entirely offline against a pre-installed portable
Python distribution. No network access, no ``pip install``.

Every execution is routed to a per-conversation NAMESPACE (its own kernel) so
that multiple AnythingLLM conversations sharing this one server process cannot
clobber each other's in-memory variables. The namespace id is sourced, in order:
  1. an authoritative conversation id from MCP request metadata (future-proof;
     AnythingLLM does not send one today), else
  2. the explicit ``namespace`` argument the caller passes (echoed back every
     call so a forgetful/weak model can keep re-sending it).

Run:  python server.py           (stdio transport, for AnythingLLM)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

import config
from artifacts import diff, snapshot
from kernel_manager import POOL

mcp = FastMCP("air-gapped-python-sandbox")


# --- shared guidance injected into every tool description --------------------
_RULES = """
IMPORTANT RULES for this sandbox:
0. NAMESPACE (MANDATORY): Every call takes a `namespace` argument that keeps
   YOUR conversation's variables separate from other conversations sharing this
   server. On your FIRST call pass namespace="new"; the response then shows
   `active_namespace: ns-XXXX`. On EVERY following call pass that EXACT value.
   If you change or drop it, your loaded data (dataframes, parsed files) will no
   longer be visible.
1. AIR-GAP: Do NOT run pip/apt/conda or any install command, and do NOT make
   network calls. Pre-installed libraries include: pandas, numpy, scipy, sympy,
   matplotlib, seaborn, pdfplumber, pypdf, PyMuPDF (fitz), openpyxl, xlsxwriter,
   xlrd, lxml, xmltodict, python-docx, markdown, beautifulsoup4, python-pptx (pptx),
   reportlab, jinja2, plotly, duckdb, polars, pyarrow, Pillow (PIL), rapidfuzz.
2. PRINT RESULTS: Only what you print() is returned. Wrap every final value,
   summary or answer in print().
3. PLOTS & REPORTS:
   - For matplotlib: Never use plt.show(). Save figures via plt.savefig('./workspace/chart.png') and call plt.close().
   - For plotly: Save interactive charts via fig.write_html('./workspace/chart.html').
   - For presentations & reports: Save .pptx, .pdf, .docx, .html files into ./workspace/.
   All saved files in ./workspace/ are auto-detected and returned as Markdown links.
4. PATHS: Read inputs and write outputs under ./workspace/ (relative paths).
   Uploaded files live there; call list_workspace_files to see them. The
   workspace is shared across namespaces, so use distinctive output filenames.
5. SELF-CORRECT: If stderr shows an error, read the traceback, fix the code and
   call execute_python_code again (with the same namespace).
Variables/imports/dataframes PERSIST between calls WITHIN your namespace.
""".strip()


# --- namespace routing -------------------------------------------------------

_NS_RE = re.compile(r"[^A-Za-z0-9_-]")
_MINT_TOKENS = {"", "new", "none", "null", "auto", "default"}


def _sanitize_ns(raw: str) -> str:
    """Reduce a caller-supplied namespace to a safe, bounded id."""
    return _NS_RE.sub("", (raw or "").strip())[:40]


def _conversation_id_from_ctx(ctx: Context | None) -> str | None:
    """Best-effort authoritative conversation id from MCP request metadata.

    AnythingLLM does not send one today, so this returns None. If a future
    client propagates a conversation/thread id (see the MCP ``_meta`` / thread-id
    proposals), namespaces upgrade to true per-conversation isolation with no
    further code change.
    """
    if ctx is None:
        return None
    try:
        rc = getattr(ctx, "request_context", None)
        meta = getattr(rc, "meta", None) if rc is not None else None
        if meta is None and rc is not None:
            req = getattr(rc, "request", None)
            params = getattr(req, "params", None) if req is not None else None
            meta = getattr(params, "meta", None) if params is not None else None
        if meta is None:
            return None

        def _get(key: str):
            return meta.get(key) if isinstance(meta, dict) else getattr(meta, key, None)

        for key in (
            "conversationId", "conversation_id",
            "threadId", "thread_id",
            "sessionId", "session_id",
        ):
            val = _get(key)
            if val:
                return str(val)
    except Exception:
        return None
    return None


def _resolve_namespace(namespace: str, ctx: Context | None) -> tuple[str, str]:
    """Return (namespace_id, source). source in {meta, provided, minted}."""
    conv = _conversation_id_from_ctx(ctx)
    if conv:
        ns = _sanitize_ns(conv)
        if ns:
            return ns, "meta"
    raw = (namespace or "").strip()
    if raw.lower() in _MINT_TOKENS:
        return POOL.mint(), "minted"
    ns = _sanitize_ns(raw)
    if not ns:
        return POOL.mint(), "minted"
    return ns, "provided"


def _banner(ns: str, source: str) -> str:
    if source == "minted":
        note = (
            f'NEW isolated namespace created. To KEEP your variables/dataframes '
            f'across calls, pass namespace="{ns}" on EVERY following call in THIS '
            f'conversation.'
        )
    elif source == "meta":
        note = "(namespace bound to this conversation automatically)"
    else:
        note = f'Reusing your namespace. Keep passing namespace="{ns}".'
    return f"active_namespace: {ns}\n{note}"


def _wrap(ns: str, source: str, body: str) -> str:
    """Prepend the namespace banner and append a trailing reminder."""
    footer = f'\n\n[reminder] your namespace = "{ns}" -- pass it as `namespace` on your next call.'
    return f"{_banner(ns, source)}\n\n{body}{footer}"


def _format_response(code_result, artifacts) -> str:
    """Render the execution result as a single text block for the LLM."""
    lines: list[str] = [f"execution_status: {code_result.status}"]

    stdout = code_result.stdout.rstrip("\n")
    lines.append("\n--- stdout ---")
    lines.append(stdout if stdout else "(no printed output — remember to print() your results)")

    if code_result.result_repr and not stdout:
        lines.append("\n--- last expression ---")
        lines.append(code_result.result_repr)

    stderr = code_result.stderr.strip()
    if stderr:
        lines.append("\n--- stderr ---")
        lines.append(stderr)

    lines.append("\n--- artifacts ---")
    if artifacts:
        for art in artifacts:
            size_kb = art.size / 1024
            lines.append(f"{art.to_markdown()}  ({size_kb:.1f} KB)")
    else:
        lines.append("(no new files created in ./workspace)")

    return "\n".join(lines)


@mcp.tool(
    description=(
        "Execute Python code in a STATEFUL, offline IPython kernel and return "
        "its stdout, stderr, generated file links, and status. Variables and "
        "imports persist across calls WITHIN your namespace, so large files only "
        "need to be loaded once. Newly created files in ./workspace (charts, "
        "CSVs, reports) are auto-detected and returned as Markdown links.\n\n"
        + _RULES
    )
)
def execute_python_code(code: str, namespace: str, ctx: Context | None = None) -> str:
    """Run ``code`` in your namespace's persistent kernel.

    Args:
        code: The Python source to execute. Use print() to return values and
              save any plots to ./workspace/ instead of calling plt.show().
        namespace: Your conversation's namespace id. Pass "new" on your first
              call to create one; then pass the exact active_namespace returned.
    """
    ns, source = _resolve_namespace(namespace, ctx)
    config.ensure_workspace()
    kernel = POOL.get(ns)
    before = snapshot()
    result = kernel.execute(code, timeout=config.EXEC_TIMEOUT)
    after = snapshot()
    artifacts = diff(before, after, config.PROJECT_ROOT)
    return _wrap(ns, source, _format_response(result, artifacts))


def _resolve_workspace_file(file_path: str) -> tuple[Path | None, str | None]:
    """Resolve a user-supplied path to a real file INSIDE the workspace.

    Accepts absolute paths, project-relative paths (e.g. "./workspace/x.py"),
    and plain names/relative paths interpreted against ./workspace. Enforces
    isolation: the resolved file must live under the workspace directory.

    Returns (path, None) on success or (None, error_message) on failure.
    """
    ws = config.ensure_workspace().resolve()
    raw = Path(file_path.strip())
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(config.PROJECT_ROOT / raw)  # e.g. ./workspace/x.py
        candidates.append(ws / raw)                    # e.g. x.py or sub/x.py
        candidates.append(ws / raw.name)               # bare filename fallback

    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        try:
            resolved.relative_to(ws)
        except ValueError:
            return None, (
                f"Refused: '{file_path}' resolves outside the workspace "
                f"({resolved}). Only files under ./workspace can be run."
            )
        return resolved, None

    return None, (
        f"File not found in workspace: '{file_path}'. "
        "Use list_workspace_files to see available files."
    )


@mcp.tool(
    description=(
        "Run an existing Python (.py) file from the ./workspace directory in "
        "your namespace's STATEFUL kernel, as a script (its `if __name__ == "
        "'__main__'` block runs). Returns the file's stdout, stderr, generated "
        "file links, and status -- the same output shape as execute_python_code. "
        "The file's top-level variables and functions are kept in memory "
        "afterwards, so you can inspect them with a follow-up execute_python_code "
        "call using the SAME namespace. The path must point inside ./workspace "
        "(e.g. 'sample.py' or './workspace/sample.py').\n\n" + _RULES
    )
)
def run_python_file(file_path: str, namespace: str, ctx: Context | None = None) -> str:
    """Execute a workspace .py file in your namespace's persistent kernel.

    Args:
        file_path: Path to a .py file inside ./workspace. Absolute paths and
                   paths outside the workspace are rejected for isolation.
        namespace: Your conversation's namespace id (see execute_python_code).
    """
    ns, source = _resolve_namespace(namespace, ctx)
    config.ensure_workspace()
    resolved, err = _resolve_workspace_file(file_path)
    if err is not None:
        return _wrap(ns, source, f"execution_status: ERROR\n\n--- stderr ---\n{err}")

    before = snapshot()
    # Run the file as __main__ so script semantics apply, then merge its public
    # names into the interactive namespace so state persists for later calls.
    code = (
        "import runpy as _runpy\n"
        f"_result_ns = _runpy.run_path({str(resolved)!r}, run_name='__main__')\n"
        "globals().update({_k: _v for _k, _v in _result_ns.items() "
        "if not _k.startswith('__')})\n"
        "del _runpy, _result_ns\n"
    )
    kernel = POOL.get(ns)
    result = kernel.execute(code, timeout=config.EXEC_TIMEOUT)
    after = snapshot()
    artifacts = diff(before, after, config.PROJECT_ROOT)
    return _wrap(ns, source, _format_response(result, artifacts))


@mcp.tool(
    description=(
        "List every file currently in the ./workspace directory (user uploads "
        "and generated outputs), with size and extension. Call this to discover "
        "input files (PDFs, Excel, CSV, etc.) before analysing them. The "
        "workspace is shared across all namespaces."
    )
)
def list_workspace_files() -> str:
    """Return a text listing of workspace files, or a note if empty."""
    ws = config.ensure_workspace()
    entries = []
    for path in sorted(ws.rglob("*")):
        if path.is_file():
            rel = path.relative_to(ws).as_posix()
            size = path.stat().st_size
            ext = path.suffix.lower().lstrip(".") or "(none)"
            entries.append((rel, size, ext))

    if not entries:
        return "The ./workspace directory is empty. No files have been uploaded yet."

    lines = [f"{len(entries)} file(s) in ./workspace:"]
    for rel, size, ext in entries:
        if size >= 1024 * 1024:
            human = f"{size / (1024 * 1024):.1f} MB"
        elif size >= 1024:
            human = f"{size / 1024:.1f} KB"
        else:
            human = f"{size} B"
        lines.append(f"- ./workspace/{rel}  [{ext}, {human}]")
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Restart the kernel for a specific namespace, clearing that "
        "conversation's in-memory state (variables, imports, dataframes). Files "
        "in ./workspace are NOT deleted, and OTHER namespaces are unaffected. "
        "Pass the active_namespace you want to reset."
    )
)
def reset_kernel_state(namespace: str) -> str:
    """Restart one namespace's kernel and confirm."""
    ns = _sanitize_ns(namespace)
    if not ns or (namespace or "").strip().lower() in _MINT_TOKENS:
        return (
            "Provide the active_namespace value you want to reset "
            "(the ns-XXXX id from a previous response)."
        )
    existed = POOL.reset(ns)
    if existed:
        return f'Namespace "{ns}" kernel restarted; its in-memory variables were cleared. Workspace files are intact.'
    return f'Namespace "{ns}" had no live kernel; a fresh one will be created on next use.'


def main() -> None:
    config.ensure_workspace()
    # Warm one reserve kernel so the first tool call is fast, and so a
    # misconfigured portable Python surfaces at startup rather than mid-chat.
    if os.environ.get("SANDBOX_LAZY_START", "").lower() not in ("1", "true", "yes"):
        try:
            POOL.prewarm()
        except Exception as exc:  # noqa: BLE001 - report and continue lazily
            print(f"[sandbox] Warning: kernel warm-start failed: {exc}")
    try:
        mcp.run()  # stdio transport by default
    finally:
        POOL.shutdown_all()


if __name__ == "__main__":
    main()
