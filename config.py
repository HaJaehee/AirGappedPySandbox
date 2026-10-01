"""Central configuration for the Air-Gapped Python Sandbox MCP server.

Every value can be overridden with an environment variable so the same code
runs unchanged on a developer machine and on the locked-down air-gapped host.

The single most important setting is ``KERNEL_PYTHON``: the interpreter used to
launch the IPython kernel. Point it at the 4 GB portable Python distribution
that ships with pandas / numpy / sympy / pdfplumber / matplotlib etc. The MCP
server process itself only needs ``mcp`` and ``jupyter_client`` and may run on a
different interpreter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    """Absolute path from an env var, or ``default``.

    A *relative* value is resolved against the project root, not the process
    working directory. The server is spawned by a host (AnythingLLM, an MCP
    client, a launcher script) that chooses its own cwd, so ``./workspace``
    would otherwise mean a different directory on every host -- and a different
    one than the sibling filesystem MCP server was given.
    """
    value = os.environ.get(name)
    if not value:
        return default
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path(__file__).resolve().parent / path).resolve()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Single source of truth for the project version. Keep in sync with the version
# history table in README.md. The server reports this over MCP (serverInfo).
VERSION: str = "0.8.0"

# Project root = directory containing this file. The kernel's working directory
# is set here so that the "./workspace/..." relative paths used by the LLM
# resolve consistently regardless of where the server was launched from.
PROJECT_ROOT: Path = Path(__file__).resolve().parent

# Isolated directory that holds user uploads and generated artifacts.
WORKSPACE_DIR: Path = _env_path("SANDBOX_WORKSPACE", PROJECT_ROOT / "workspace")

# Interpreter that runs the stateful IPython kernel (the 4 GB portable Python).
# Defaults to the interpreter running this server, which is convenient for
# development but should be set explicitly in production.
KERNEL_PYTHON: str = os.environ.get("SANDBOX_KERNEL_PYTHON", sys.executable)

# How long ONE tool call may block before it has to answer something.
#
# An MCP client cancels a request that outlives its own timeout; the official
# SDK's default is 60s and AnythingLLM reports the cancellation as "Context
# server request timeout", which tells the user nothing. Everything this server
# does inside a call is therefore budgeted to finish first, so the failure the
# user sees is always OUR description of it.
TOOL_BUDGET: int = _env_int("SANDBOX_TOOL_BUDGET", 45)

# Hard ceiling on a single execute_python_code call (seconds). On timeout the
# kernel is interrupted; if it stays wedged it is restarted.
#
# Kept under TOOL_BUDGET on purpose. At the old default of 60 this raced the
# client's own 60s timeout: a long cell was a coin flip between this server's
# clean TIMEOUT verdict and an opaque client-side cancellation. Raise both (and
# the client's) together if the host needs longer cells.
EXEC_TIMEOUT: int = _env_int("SANDBOX_EXEC_TIMEOUT", 45)

# How long to wait for the kernel to come up and answer a first request.
STARTUP_TIMEOUT: int = _env_int("SANDBOX_STARTUP_TIMEOUT", 60)

# Cap on captured stdout/stderr returned to the model (characters). Prevents a
# runaway loop from flooding the LLM context. 0 disables the cap.
MAX_STREAM_CHARS: int = _env_int("SANDBOX_MAX_STREAM_CHARS", 20000)

# Cap on a single write_workspace_file call (bytes of UTF-8 content). The content
# is text the model itself emitted, so its own context window keeps it far below
# this; the cap only bounds a runaway loop. 0 disables the cap.
MAX_WRITE_BYTES: int = _env_int("SANDBOX_MAX_WRITE_BYTES", 1_048_576)  # 1 MB

# --- pip installs into a side venv ------------------------------------------
# The host network carries an intranet PyPI mirror, so missing packages can be
# installed on demand -- but never into the curated portable Python. They go
# into VENV_DIR, a venv created from KERNEL_PYTHON with --system-site-packages,
# which the kernel adds to sys.path. Deleting that one directory undoes every
# install the model ever made.
#
# Deliberately NOT inside the workspace: the artifact interceptor diffs that
# tree after every execution and would report thousands of installed files as
# new artifacts.
VENV_DIR: Path = _env_path("SANDBOX_VENV", PROJECT_ROOT / ".sandbox-venv")

# The intranet PyPI mirror. Left empty, pip uses its own configuration (pip.ini
# / PIP_INDEX_URL) -- which on an unconfigured host means it reaches for the
# public PyPI and fails. Set this and the failure mode disappears.
PIP_INDEX_URL: str = os.environ.get("SANDBOX_PIP_INDEX_URL", "").strip()
# Needed when the mirror is plain http, or https with an internal CA.
PIP_TRUSTED_HOST: str = os.environ.get("SANDBOX_PIP_TRUSTED_HOST", "").strip()

# Ceiling on one install, and on pip's own wait for the index. PIP_RETRIES is 0
# on purpose: pip's default of 5 retries with backoff turns "no mirror
# configured" into a multi-minute stall before it admits defeat, and a model
# waiting that long reads the timeout as a transient glitch and tries again.
PIP_TIMEOUT: int = _env_int("SANDBOX_PIP_TIMEOUT", 300)
PIP_NETWORK_TIMEOUT: int = _env_int("SANDBOX_PIP_NETWORK_TIMEOUT", 15)
PIP_RETRIES: int = _env_int("SANDBOX_PIP_RETRIES", 0)
VENV_CREATE_TIMEOUT: int = _env_int("SANDBOX_VENV_CREATE_TIMEOUT", 180)

# Bounds one install call, so a confused model cannot ask for a hundred names.
MAX_PACKAGES_PER_CALL: int = _env_int("SANDBOX_MAX_PACKAGES_PER_CALL", 20)

# --- Per-conversation namespace routing -------------------------------------
# Every code execution runs in a namespace-scoped kernel so that separate
# AnythingLLM conversations do not clobber each other's in-memory variables.
# Idle namespace kernels are evicted to reclaim RAM (like a cloud sandbox's
# inactivity timeout); an evicted namespace is rebuilt fresh on next use.
NS_IDLE_TIMEOUT: int = _env_int("SANDBOX_NS_IDLE_TIMEOUT", 1800)  # seconds
# Hard cap on simultaneously live namespace kernels; the least-recently-used is
# evicted when the cap would be exceeded (bounds total memory).
MAX_NAMESPACES: int = _env_int("SANDBOX_MAX_NAMESPACES", 8)

# Image extensions rendered as inline Markdown images; everything else becomes a
# plain Markdown link.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}


def ensure_workspace() -> Path:
    """Create the workspace directory if needed and return it."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_DIR
