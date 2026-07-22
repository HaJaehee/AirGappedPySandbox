"""Air-Gapped Python Sandbox -- MCP server.

Exposes a stateful Python code interpreter to an LLM (via AnythingLLM's MCP
agent support) that runs entirely offline against a pre-installed portable
Python distribution. No network access, no ``pip install``.

Run:  python server.py           (stdio transport, for AnythingLLM)
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import config
from artifacts import diff, snapshot
from kernel_manager import KERNEL

mcp = FastMCP("air-gapped-python-sandbox")


# --- shared guidance injected into every tool description --------------------
_RULES = """
IMPORTANT RULES for this sandbox:
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
   Uploaded files live there; call list_workspace_files to see them.
5. SELF-CORRECT: If stderr shows an error, read the traceback, fix the code and
   call execute_python_code again.
State (variables, imports, dataframes) PERSISTS between calls in this session.
""".strip()


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
        "imports persist across calls in the same session, so large files only "
        "need to be loaded once. Newly created files in ./workspace (charts, "
        "CSVs, reports) are auto-detected and returned as Markdown links.\n\n"
        + _RULES
    )
)
def execute_python_code(code: str) -> str:
    """Run ``code`` in the persistent kernel.

    Args:
        code: The Python source to execute. Use print() to return values and
              save any plots to ./workspace/ instead of calling plt.show().
    """
    config.ensure_workspace()
    before = snapshot()
    result = KERNEL.execute(code, timeout=config.EXEC_TIMEOUT)
    after = snapshot()
    artifacts = diff(before, after, config.PROJECT_ROOT)
    return _format_response(result, artifacts)


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
        "Run an existing Python (.py) file from the ./workspace directory in the "
        "STATEFUL kernel, as if executed as a script (its `if __name__ == "
        "'__main__'` block runs). Returns the file's stdout, stderr, generated "
        "file links, and status -- the same output shape as execute_python_code. "
        "The file's top-level variables and functions are kept in memory "
        "afterwards, so you can inspect them with a follow-up execute_python_code "
        "call. The path must point inside ./workspace (e.g. 'sample.py' or "
        "'./workspace/sample.py'). Remember: only printed output is captured, so "
        "the file should print() the results you need.\n\n" + _RULES
    )
)
def run_python_file(file_path: str) -> str:
    """Execute a workspace .py file in the persistent kernel.

    Args:
        file_path: Path to a .py file inside ./workspace. Absolute paths and
                   paths outside the workspace are rejected for isolation.
    """
    config.ensure_workspace()
    resolved, err = _resolve_workspace_file(file_path)
    if err is not None:
        return f"execution_status: ERROR\n\n--- stderr ---\n{err}"

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
    result = KERNEL.execute(code, timeout=config.EXEC_TIMEOUT)
    after = snapshot()
    artifacts = diff(before, after, config.PROJECT_ROOT)
    return _format_response(result, artifacts)


@mcp.tool(
    description=(
        "List every file currently in the ./workspace directory (user uploads "
        "and generated outputs), with size and extension. Call this to discover "
        "input files (PDFs, Excel, CSV, etc.) before analysing them."
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
        "Restart the Python kernel, clearing ALL in-memory state (variables, "
        "imports, loaded dataframes). Files in ./workspace are NOT deleted. Use "
        "only when the session state is corrupted or you want a clean slate."
    )
)
def reset_kernel_state() -> str:
    """Restart the kernel and confirm."""
    KERNEL.restart()
    return "Kernel restarted. All in-memory variables were cleared; workspace files are intact."


def main() -> None:
    config.ensure_workspace()
    # Warm the kernel so the first tool call is fast. If the portable Python is
    # misconfigured this surfaces the error at startup rather than mid-chat.
    if os.environ.get("SANDBOX_LAZY_START", "").lower() not in ("1", "true", "yes"):
        try:
            KERNEL.start()
        except Exception as exc:  # noqa: BLE001 - report and continue lazily
            print(f"[sandbox] Warning: kernel warm-start failed: {exc}")
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
