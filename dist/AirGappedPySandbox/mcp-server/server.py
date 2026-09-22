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

import asyncio
import os
import re
import time
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

import config
import pip_installer
from artifacts import chat_link_path, diff, snapshot
from kernel_manager import POOL

mcp = FastMCP("air-gapped-python-sandbox")
# Report the project version in the MCP handshake (serverInfo.version). FastMCP
# has no version kwarg, so set it on the underlying low-level server.
mcp._mcp_server.version = config.VERSION


# --- shared guidance injected into every tool description --------------------
_RULES = """
IMPORTANT RULES for this sandbox:
0. NAMESPACE (MANDATORY): Every call takes a `namespace` argument that keeps
   YOUR conversation's variables separate from other conversations sharing this
   server. On your FIRST call pass namespace="new"; the response then shows
   `active_namespace: ns-XXXX`. On EVERY following call pass that EXACT value.
   If you change or drop it, your loaded data (dataframes, parsed files) will no
   longer be visible.
1. NO INSTALLS FROM YOUR CODE: Do NOT run pip/apt/conda inside the code you
   execute, and do NOT make network calls. If you need a library that is not
   listed below, call the install_python_packages tool -- that is the only
   supported way to add one. Pre-installed libraries include: pandas, numpy, scipy, sympy,
   matplotlib, seaborn, pdfplumber, pypdf, PyMuPDF (fitz), openpyxl, xlsxwriter,
   xlrd, lxml, xmltodict, python-docx, markdown, beautifulsoup4, python-pptx (pptx),
   reportlab, jinja2, plotly, duckdb, polars, pyarrow, Pillow (PIL), rapidfuzz.
2. PRINT RESULTS: Only what you print() is returned. Wrap every final value,
   summary or answer in print().
3. PLOTS & REPORTS:
   - For matplotlib: Never use plt.show(). Save figures via plt.savefig('chart.png') and call plt.close().
   - For plotly: Save interactive charts via fig.write_html('chart.html').
   - For presentations & reports: Save .pptx, .pdf, .docx, .html files with plain relative names.
   Every file you save is auto-detected and returned as a Markdown link.
4. PATHS: Your working directory IS the workspace. Use plain relative names
   ('chart.png', 'out/report.pdf') for both inputs and outputs -- do NOT prefix
   them with './workspace/'. Uploaded files are already here; call
   list_workspace_files to see them. The workspace is shared across namespaces,
   so use distinctive output filenames.
5. SELF-CORRECT: If stderr shows an error, read the traceback, fix the code and
   call execute_python_code again (with the same namespace).
6. "IT RAN" IS NOT "IT WORKED": execution_status reports only what this sandbox
   can observe. RAN_UNVERIFIED means the interpreter finished without raising --
   nothing more. Never tell the user a task succeeded on that basis; compare the
   printed output against what was actually asked first. Every response ends
   with a `next_action:` line: do what it says.
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


def _reminder(ns: str) -> str:
    return f'[reminder] your namespace = "{ns}" -- pass it as `namespace` on your next call.'


def _verdict(code_result, artifacts) -> tuple[str, str, str]:
    """Grade one execution: (status, what it means, what to do next).

    The sandbox can prove a failure; it can almost never prove a success. So
    the only optimistic verdict it hands out is RAN_UNVERIFIED, and the word
    SUCCESS never appears -- a weak model reads it as "the task is done" and
    stops looking, which is exactly the bug this grading exists to prevent.
    """
    if code_result.status == "TIMEOUT":
        lost = " The kernel had to be restarted, so variables are gone." if \
            code_result.state_reset else ""
        return (
            "TIMEOUT",
            "The code was interrupted at the time limit and did NOT finish." + lost,
            "Do not report a result. Cut the work down (smaller input, fewer "
            "rows, a narrower loop), add progress print()s, and run again.",
        )

    if code_result.status == "FAILED":
        return (
            "FAILED",
            "The code raised an exception or exited with a non-zero code. "
            "Nothing it was asked to do can be assumed done.",
            "Do NOT tell the user this worked. Read the stderr block, fix the "
            "code, and call this tool again with the same namespace.",
        )

    if code_result.signals:
        return (
            "RAN_WITH_ERRORS",
            "The code finished, but errors were reported while it ran: "
            + "; ".join(code_result.signals),
            "Treat this as unfinished until you have checked those errors. If "
            "they are real, fix the code and run once more; if they are "
            "genuinely harmless, say so explicitly instead of ignoring them.",
        )

    has_output = bool(code_result.stdout.strip() or code_result.result_repr.strip())
    if not has_output and not artifacts:
        return (
            "RAN_NO_OUTPUT",
            "The code finished without printing anything and without changing "
            "any file, so there is NO evidence of what it did.",
            "Do not report a result you cannot see. Wrap the values that answer "
            "the user's question in print() and run again with the same namespace.",
        )

    exited_clean = " (the script exited with code 0)" if code_result.exit_code == 0 else ""
    return (
        "RAN_UNVERIFIED",
        "The interpreter finished without raising" + exited_clean + ". That is "
        "NOT proof the task succeeded -- this sandbox cannot check your intent.",
        "Read the output below and confirm it actually answers what was asked "
        "before you report anything to the user.",
    )


def _render(ns: str, source: str, code_result, artifacts) -> str:
    """Render one execution as the single text block the model reads.

    The layout is deliberate: a weak model anchors on the FIRST and LAST lines,
    so the honest verdict opens the block and the instruction closes it, and
    stderr sits above stdout because it carries the reason for the verdict.
    """
    status, meaning, next_action = _verdict(code_result, artifacts)
    lines: list[str] = [f"execution_status: {status}", meaning, "", _banner(ns, source)]

    stderr = code_result.stderr.strip()
    if stderr:
        lines.append("\n--- stderr ---")
        lines.append(stderr)

    stdout = code_result.stdout.rstrip("\n")
    lines.append("\n--- stdout ---")
    lines.append(stdout if stdout else "(nothing was printed -- only what you print() comes back)")

    if code_result.result_repr and not stdout:
        lines.append("\n--- last expression ---")
        lines.append(code_result.result_repr)

    lines.append("\n--- artifacts ---")
    if artifacts:
        for art in artifacts:
            size_kb = art.size / 1024
            lines.append(f"{art.to_markdown()}  ({size_kb:.1f} KB)")
    else:
        lines.append("(no new files created in the workspace)")

    lines.append("\n" + _reminder(ns))
    lines.append(f"next_action: {next_action}")
    return "\n".join(lines)


def _refusal(ns: str, source: str, message: str) -> str:
    """A refusal before any code ran -- shaped like a failed execution."""
    return "\n".join([
        "execution_status: FAILED",
        "The tool refused the request, so no code ran at all.",
        "",
        _banner(ns, source),
        "",
        "--- stderr ---",
        message,
        "",
        _reminder(ns),
        "next_action: Fix the problem described above, then call the tool again.",
    ])


@mcp.tool(
    description=(
        "Execute Python code in a STATEFUL, offline IPython kernel and return "
        "its stdout, stderr, generated file links, and status. Variables and "
        "imports persist across calls WITHIN your namespace, so large files only "
        "need to be loaded once. Newly created files in the workspace (charts, "
        "CSVs, reports) are auto-detected and returned as Markdown links.\n"
        "(To simply SAVE text or code to a file without running it, use "
        "write_workspace_file instead -- it is simpler and needs no namespace.)"
        "\n\n" + _RULES
    )
)
def execute_python_code(code: str, namespace: str, ctx: Context | None = None) -> str:
    """Run ``code`` in your namespace's persistent kernel.

    Args:
        code: The Python source to execute. Use print() to return values and
              save any plots with a plain relative filename (the working
              directory is the workspace) instead of calling plt.show().
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
    return _render(ns, source, result, artifacts)


def _resolve_workspace_file(file_path: str) -> tuple[Path | None, str | None]:
    """Resolve a user-supplied path to a real file INSIDE the workspace.

    Accepts absolute paths, workspace-prefixed paths (e.g. "./workspace/x.py")
    and plain names/relative paths. All non-absolute forms are interpreted
    against the workspace, wherever SANDBOX_WORKSPACE puts it. Enforces
    isolation: the resolved file must live under the workspace directory.

    Returns (path, None) on success or (None, error_message) on failure.
    """
    ws = config.ensure_workspace().resolve()
    text = file_path.strip().replace("\\", "/")
    # "./workspace/x.py" means "x.py inside the workspace", wherever the
    # workspace happens to live. Resolving it against PROJECT_ROOT (as this
    # did) pointed at the server's own directory once SANDBOX_WORKSPACE moved,
    # which either missed the file or matched a stale same-named file there and
    # then refused it for being outside the workspace.
    stripped = _strip_workspace_prefix(text)
    raw = Path(stripped)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(ws / raw)       # e.g. x.py or sub/x.py
        candidates.append(ws / raw.name)  # bare filename fallback

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


# Windows swallows writes to these device names silently, so reject them early.
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_WS_PREFIXES = ("./workspace/", "workspace/", "/workspace/")


def _strip_workspace_prefix(text: str) -> str:
    """Drop a leading "./workspace/" so the rest is workspace-relative.

    Models write both 'x.py' and './workspace/x.py'; both name the same file.
    """
    for prefix in _WS_PREFIXES:
        if text.lower().startswith(prefix):
            return text[len(prefix):]
    return text


def _resolve_new_workspace_path(filename: str) -> tuple[Path | None, str | None]:
    """Resolve a target path for a file to be CREATED inside the workspace.

    Unlike :func:`_resolve_workspace_file` this does not require the file to
    exist yet, so containment is checked against the *parent* directory.

    Returns (path, None) on success or (None, error_message) on failure.
    """
    ws = config.ensure_workspace().resolve()
    raw = (filename or "").strip().replace("\\", "/")
    if not raw:
        return None, "Provide a filename, e.g. 'analysis.py'."

    raw = _strip_workspace_prefix(raw)

    rel = Path(raw)
    if rel.is_absolute() or rel.drive:
        return None, (
            f"Refused: pass a path relative to ./workspace, not '{filename}'."
        )
    if not rel.name:
        return None, f"Refused: '{filename}' does not name a file."
    if any(part == ".." for part in rel.parts):
        return None, f"Refused: '..' is not allowed in '{filename}'."
    if any(part.split(".")[0].upper() in _WIN_RESERVED for part in rel.parts):
        return None, f"Refused: '{filename}' uses a Windows reserved device name."

    target = ws / rel
    try:
        resolved_parent = target.parent.resolve()
    except OSError:
        return None, f"Refused: invalid path '{filename}'."
    # Second line of defence: a symlink inside the workspace could still point out.
    try:
        resolved_parent.relative_to(ws)
    except ValueError:
        return None, f"Refused: '{filename}' resolves outside ./workspace."

    return resolved_parent / target.name, None


def _display_paths(target: Path) -> tuple[str, str]:
    """Return (chat link path, workspace-relative path) for ``target``.

    ``WORKSPACE_DIR`` can be pointed outside the project via SANDBOX_WORKSPACE;
    ``chat_link_path`` keeps the link in the familiar "workspace/<name>" shape
    either way (same rule as ``artifacts.diff``).
    """
    link = chat_link_path(target, config.PROJECT_ROOT)
    try:
        inner = target.relative_to(config.WORKSPACE_DIR.resolve()).as_posix()
    except ValueError:
        inner = target.name
    return link, inner


# Executes the file's source directly in the interactive namespace. IPython's
# user namespace is already named "__main__", so a script's
# `if __name__ == "__main__":` block runs -- and because names land in globals()
# as they are bound, they survive sys.exit() and even a crash.
#
# runpy.run_path (used before) handed its namespace back only on a clean return,
# so the ordinary `sys.exit(main())` ending left NOTHING in the kernel, while
# IPython reported the SystemExit as ERROR on top of that. _sandbox_exit_code is
# read back by the kernel's probe (see kernel_manager._PROBE_CODE).
_RUN_FILE_TEMPLATE = """\
import sys as _sb_sys
_sandbox_exit_code = None
_sb_path = {path}
_sb_prev_file = globals().get('__file__')
_sb_prev_argv = _sb_sys.argv
__file__ = _sb_path
_sb_sys.argv = [_sb_path]
try:
    with open(_sb_path, 'r', encoding='utf-8') as _sb_fh:
        _sb_src = _sb_fh.read()
    exec(compile(_sb_src, _sb_path, 'exec'), globals())
except SystemExit as _sb_exc:
    _sandbox_exit_code = 0 if _sb_exc.code is None else _sb_exc.code
finally:
    _sb_sys.argv = _sb_prev_argv
    if _sb_prev_file is None:
        globals().pop('__file__', None)
    else:
        __file__ = _sb_prev_file
    for _sb_name in ('_sb_sys', '_sb_path', '_sb_prev_file', '_sb_prev_argv',
                     '_sb_fh', '_sb_src', '_sb_exc', '_sb_name'):
        globals().pop(_sb_name, None)
"""


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
        return _refusal(ns, source, err)
    if resolved.suffix.lower() != ".py":
        err = (
            f"Refused: '{resolved.name}' is not a .py file. run_python_file only "
            "runs Python scripts; use execute_python_code to open or parse other "
            "file types."
        )
        return _refusal(ns, source, err)

    before = snapshot()
    code = _RUN_FILE_TEMPLATE.format(path=repr(str(resolved)))
    kernel = POOL.get(ns)
    result = kernel.execute(code, timeout=config.EXEC_TIMEOUT)
    after = snapshot()
    artifacts = diff(before, after, config.PROJECT_ROOT)
    return _render(ns, source, result, artifacts)


@mcp.tool(
    description=(
        "Save text content to a file in ./workspace -- source code, notes, CSV, "
        "Markdown, JSON, config. Use this whenever the user asks you to WRITE, "
        "SAVE, or OUTPUT something AS A FILE. Do NOT generate Python that writes "
        "the file itself; pass the text directly as `content`.\n\n"
        "filename : path relative to ./workspace, e.g. \"fib.py\" or "
        "\"reports/out.md\". A leading \"./workspace/\" is accepted and stripped.\n"
        "content  : the exact text to write (UTF-8). Written verbatim.\n"
        "overwrite: pass true only to replace an existing file (default false).\n\n"
        "This tool does NOT execute anything and does NOT need a namespace. "
        "To run a .py file you saved, call run_python_file next."
    )
)
def write_workspace_file(filename: str, content: str, overwrite: bool = False) -> str:
    """Write ``content`` verbatim to ``filename`` inside ./workspace.

    Args:
        filename: Path relative to ./workspace. Escapes are rejected.
        content: Exact text to write, UTF-8 encoded with LF line endings.
        overwrite: Replace an existing file instead of refusing.
    """
    target, err = _resolve_new_workspace_path(filename)
    if err is not None:
        return f"write_status: ERROR\n\n--- stderr ---\n{err}"

    text = content if content is not None else ""
    size = len(text.encode("utf-8"))
    if config.MAX_WRITE_BYTES and size > config.MAX_WRITE_BYTES:
        return (
            "write_status: ERROR\n\n--- stderr ---\n"
            f"Refused: content is {size} bytes, over the "
            f"{config.MAX_WRITE_BYTES}-byte limit for one write."
        )

    link_path, inner_path = _display_paths(target)
    if target.exists() and not target.is_file():
        return (
            "write_status: ERROR\n\n--- stderr ---\n"
            f"Refused: '{filename}' is a directory, not a file."
        )
    if target.exists() and not overwrite:
        try:
            existing = target.stat().st_size
        except OSError:
            existing = 0
        return (
            "write_status: ERROR\n\n--- stderr ---\n"
            f"Refused: './{link_path}' already exists ({existing / 1024:.1f} KB).\n"
            "Either call again with overwrite=true, or choose a different filename."
        )

    before = snapshot()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" keeps the file byte-identical to what the model sent;
        # Windows text mode would otherwise rewrite every \n as \r\n.
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        return f"write_status: ERROR\n\n--- stderr ---\nCould not write '{filename}': {exc}"
    after = snapshot()
    artifacts = diff(before, after, config.PROJECT_ROOT)

    line_count = text.count("\n") + 1 if text else 0
    lines = [
        "write_status: SUCCESS",
        "\n--- file ---",
        f"./{link_path}  ({size} B, {line_count} lines)",
        "\n--- artifacts ---",
    ]
    if artifacts:
        for art in artifacts:
            lines.append(f"{art.to_markdown()}  ({art.size / 1024:.1f} KB)")
    else:
        lines.append("(warning: the write reported success but no file change was detected)")
    if target.suffix.lower() == ".py":
        lines.append(
            f'\nNext: run it with run_python_file(file_path="{inner_path}", '
            "namespace=<your namespace>)."
        )
    return "\n".join(lines)


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


def _install_block(ns: str, source: str, status: str, headline: str,
                   body: list[str], next_action: str) -> str:
    """Same shape as an execution response: verdict first, instruction last."""
    lines = [f"install_status: {status}", headline, "", _banner(ns, source)]
    lines.extend(body)
    lines.append("\n" + _reminder(ns))
    lines.append(f"next_action: {next_action}")
    return "\n".join(lines)


async def _report_progress(ctx: Context | None, job) -> None:
    """Tell the client we are still alive, if it asked to be told.

    Some MCP clients extend their timeout while progress arrives. Those that do
    not simply ignore this -- the real protection against the client giving up
    is the budget in _await_install, not these notifications.
    """
    if ctx is None:
        return
    try:
        await ctx.report_progress(
            min(job.elapsed, config.PIP_TIMEOUT), config.PIP_TIMEOUT,
            f"pip install running ({job.elapsed:.0f}s)",
        )
    except Exception:  # noqa: BLE001 - no client support, or no progress token
        pass


async def _await_install(job, deadline: float, ctx: Context | None):
    """Wait for a background install, but never past ``deadline``.

    Returns the finished InstallReport, or None when the budget ran out and the
    install is still going.
    """
    last_ping = 0.0
    while True:
        if job.done.is_set():
            return pip_installer.collect(job)
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.5)
        if job.elapsed - last_ping >= 5:
            last_ping = job.elapsed
            await _report_progress(ctx, job)


@mcp.tool(
    description=(
        "Install Python packages from the company's internal PyPI mirror into "
        "this sandbox, so you can import them in later execute_python_code "
        "calls. Use it when an import fails because a library is missing.\n\n"
        "packages : distribution names separated by spaces or commas, e.g. "
        "\"polars\" or \"polars, rapidfuzz>=3.0\". Names ONLY (a version "
        "specifier is allowed) -- URLs, file paths, editable installs and pip "
        "flags are refused, and you cannot choose the index.\n"
        "namespace: your conversation's namespace id, exactly as for "
        "execute_python_code.\n\n"
        "The distribution name is often NOT the import name: install "
        "\"beautifulsoup4\" to import bs4, \"python-docx\" to import docx, "
        "\"Pillow\" to import PIL.\n\n"
        "Packages go into a separate virtual environment, never into the "
        "bundled Python, and become importable in every live namespace "
        "immediately -- no kernel restart, so your loaded data survives.\n\n"
        "The reply starts with `install_status:` INSTALLED, RUNNING or FAILED.\n"
        "INSTALLED: the kernel itself confirmed each package and reports its "
        "version -- not merely that pip exited 0.\n"
        "RUNNING: NOT a failure. Downloading can take longer than one tool "
        "call, so the install continues in the background. Call this tool "
        "again immediately with the SAME packages and namespace to collect the "
        "result; that joins the running install, it does not start a second "
        "one. Repeat until you get INSTALLED or FAILED.\n"
        "FAILED: explains what went wrong. The usual cause is that this machine "
        "was never pointed at the mirror, which is for the USER to fix -- "
        "report it instead of retrying."
    )
)
async def install_python_packages(packages: str, namespace: str,
                                  ctx: Context | None = None) -> str:
    """Install ``packages`` into the sandbox venv and verify them in the kernel.

    Async, and the pip run happens on a background thread, because an MCP client
    cancels a request that outruns its own timeout (60s in the official SDK;
    AnythingLLM shows it as "Context server request timeout"). A mirror download
    -- plus creating the venv on the very first call -- goes past that easily,
    which made a perfectly healthy mirror look broken. So this answers within
    SANDBOX_TOOL_BUDGET no matter what, and says how to collect a slow result.

    Args:
        packages: Distribution names separated by spaces or commas, each
              optionally carrying a version specifier ("polars>=1.0").
        namespace: Your conversation's namespace id (see execute_python_code).
    """
    deadline = time.monotonic() + config.TOOL_BUDGET
    ns, source = _resolve_namespace(namespace, ctx)
    specs, rejected = pip_installer.parse_requests(packages)

    if rejected:
        return _install_block(
            ns, source, "FAILED",
            "Refused before pip ran: " + ", ".join(rejected),
            ["\n--- why ---",
             "Pass distribution names only, optionally with a version "
             "specifier -- \"polars\" or \"polars>=1.0\". URLs, file paths, "
             "editable installs and pip flags are not accepted: the index and "
             "every other pip option are fixed by the server."],
            "Call this tool again with plain package names only.")

    if not specs:
        return _install_block(
            ns, source, "FAILED", "No package was named.", [],
            'Call this tool again with, for example, packages="polars".')

    if len(specs) > config.MAX_PACKAGES_PER_CALL:
        return _install_block(
            ns, source, "FAILED",
            f"{len(specs)} packages requested; the limit is "
            f"{config.MAX_PACKAGES_PER_CALL} per call.",
            [], "Ask for the ones you actually need, in smaller batches.")

    names = [pip_installer.dist_name(spec) for spec in specs]
    kernel = await asyncio.to_thread(POOL.get, ns)

    # A job already in flight for exactly these packages means this is the model
    # coming back to collect a result, as a RUNNING answer told it to. Go
    # straight to it: re-running the "already importable?" shortcut here could
    # answer from a half-finished install and strand the job in the registry.
    job = pip_installer.peek(specs)
    before: dict[str, str | None] = {}
    if job is None:
        before = await asyncio.to_thread(kernel.package_versions, names)
        # Nothing to do: every requested name is already importable and none of
        # them pinned a version. Saves a pointless round trip to the mirror when
        # a model reflexively "installs" pandas.
        if all(spec == pip_installer.dist_name(spec) for spec in specs) and \
                all(before.get(name) for name in names):
            return _install_block(
                ns, source, "INSTALLED",
                f"{len(names)} package(s) were already importable; pip was not run.",
                ["\n--- packages ---"]
                + [f"{name} {before[name]}  (already available)" for name in names],
                "Import them in your next execute_python_code call, with the "
                "same namespace.")
        job = pip_installer.start_or_attach(specs)

    report = await _await_install(job, deadline, ctx)

    if report is None:
        # Still going. This is the case that used to surface as the client's own
        # "Context server request timeout", with the install invisibly
        # continuing and no way for the model to learn how it ended.
        again = " ".join(job.specs)
        index_url, index_origin = await asyncio.to_thread(pip_installer.effective_index)
        return _install_block(
            ns, source, "RUNNING",
            f"pip is still working after {job.elapsed:.0f}s. This is NOT a "
            "failure: the install carries on in the background.",
            ["\n--- what is happening ---",
             "Downloading and installing can outlast a single tool call, so "
             "this call is answering early rather than being cut off. Nothing "
             "has been lost.",
             "\n--- pip ---",
             f"index:   {index_url}  (from {index_origin})",
             f"venv:    {config.VENV_DIR}",
             f"elapsed: {job.elapsed:.0f}s of at most {config.PIP_TIMEOUT}s"],
            f'Call install_python_packages again, right now, with exactly '
            f'packages="{again}" and namespace="{ns}" to collect the result. '
            "Do not change the package list and do not start another install "
            "in between.")

    pip_lines = [
        "\n--- pip ---",
        f"index: {report.index_label}",
        f"venv:  {config.VENV_DIR}" + (" (created now)" if report.venv_created else ""),
    ]
    if report.seconds:
        exit_note = "" if report.returncode is None else f", exit {report.returncode}"
        pip_lines.append(f"took:  {report.seconds:.1f}s{exit_note}")

    if not report.ok:
        body = ["\n--- why it failed ---", pip_installer.diagnose(report)]
        if report.output:
            body += ["\n--- pip output ---", report.output]
        return _install_block(
            ns, source, "FAILED",
            "pip could not install: " + ", ".join(specs),
            body + pip_lines,
            "Do NOT repeat the same install. Tell the user in one sentence what "
            "has to be fixed on their machine, and carry on without the package "
            "if the task allows it.")

    site_dir = pip_installer.site_packages_dir()
    busy: list[str] = []
    if site_dir is not None:
        busy = await asyncio.to_thread(POOL.add_site_dir_everywhere, str(site_dir))
    after = await asyncio.to_thread(kernel.package_versions, names)
    missing = [name for name in names if not after.get(name)]

    if missing:
        # pip exiting 0 proves a download, not an import. Saying INSTALLED here
        # would be the same lie the execution verdicts exist to prevent.
        body = [
            "\n--- why it failed ---",
            "pip exited 0, but " + ", ".join(missing) + " is still not visible "
            "to the kernel. The download worked and the import path did not: "
            "usually the distribution name is an alias for a differently-named "
            f"package, or the files landed outside {site_dir}.",
        ]
        if report.output:
            body += ["\n--- pip output ---", report.output]
        return _install_block(
            ns, source, "FAILED",
            "Downloaded by pip, but not importable: " + ", ".join(missing),
            body + pip_lines,
            "Confirm the exact distribution name with the user before retrying.")

    packages_block = ["\n--- packages ---"]
    for name in names:
        unchanged = before.get(name) and before.get(name) == after.get(name)
        packages_block.append(
            f"{name} {after[name]}" + ("  (already available)" if unchanged else ""))
    if busy:
        packages_block.append(
            "\nnote: namespace(s) " + ", ".join(busy) + " were running code and "
            "could not be refreshed; they will see the package when their "
            "kernel next starts. Your own namespace is ready.")

    return _install_block(
        ns, source, "INSTALLED",
        f"{len(names)} package(s) are importable in your namespace now.",
        packages_block + pip_lines,
        "Import them in your next execute_python_code call, with the same "
        "namespace.")


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
        # Build the package venv now too. Creating it can take tens of seconds
        # on the portable Python, and paying that inside the first
        # install_python_packages call is what pushed it past the client's
        # timeout on a host whose mirror was working perfectly well.
        try:
            created, err = pip_installer.ensure_venv()
            if err:
                print(f"[sandbox] Warning: package venv unavailable: {err}")
            elif created:
                print(f"[sandbox] Created package venv at {config.VENV_DIR}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sandbox] Warning: package venv setup failed: {exc}")
    try:
        mcp.run()  # stdio transport by default
    finally:
        POOL.shutdown_all()


if __name__ == "__main__":
    main()
