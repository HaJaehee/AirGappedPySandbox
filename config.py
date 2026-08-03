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
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


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
VERSION: str = "0.3.0"

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

# Hard ceiling on a single execute_python_code call (seconds). On timeout the
# kernel is interrupted; if it stays wedged it is restarted.
EXEC_TIMEOUT: int = _env_int("SANDBOX_EXEC_TIMEOUT", 60)

# How long to wait for the kernel to come up and answer a first request.
STARTUP_TIMEOUT: int = _env_int("SANDBOX_STARTUP_TIMEOUT", 60)

# Cap on captured stdout/stderr returned to the model (characters). Prevents a
# runaway loop from flooding the LLM context. 0 disables the cap.
MAX_STREAM_CHARS: int = _env_int("SANDBOX_MAX_STREAM_CHARS", 20000)

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
