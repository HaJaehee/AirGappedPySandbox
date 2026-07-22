"""Air-Gapped Python Sandbox -- MCP server.

Exposes a stateful Python code interpreter to an LLM (via AnythingLLM's MCP
agent support) that runs entirely offline against a pre-installed portable
Python distribution. No network access, no ``pip install``.

Run:  python server.py           (stdio transport, for AnythingLLM)
"""

from __future__ import annotations

import os

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
