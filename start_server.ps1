# Launch the Air-Gapped Python Sandbox MCP server.
#
# Edit $PortablePython to point at your 4 GB portable Python interpreter, then
# run:  powershell -ExecutionPolicy Bypass -File .\start_server.ps1
#
# AnythingLLM normally launches server.py for you via its MCP config (see
# anythingllm_mcp_config.example.json). This script is for manual testing.

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- CONFIGURE ME -----------------------------------------------------------
$PortablePython = "C:\path\to\portable-python\python.exe"

# The company PyPI mirror that install_python_packages installs from. Leave it
# empty and pip falls back to the public PyPI, which a closed network cannot
# reach -- every install then fails. Set TrustedHost (host name only) when the
# mirror is plain http: without it pip silently ignores the index and reports
# the package as missing. Check both with: python check_environment.py
$PipIndexUrl = ""
$PipTrustedHost = ""
# ---------------------------------------------------------------------------

if (-not (Test-Path $PortablePython)) {
    Write-Warning "Portable Python not found at '$PortablePython'. Falling back to the interpreter on PATH."
    $PortablePython = "python"
}

$env:SANDBOX_KERNEL_PYTHON = $PortablePython
$env:SANDBOX_WORKSPACE = Join-Path $Here "workspace"
if (-not $env:SANDBOX_EXEC_TIMEOUT) { $env:SANDBOX_EXEC_TIMEOUT = "60" }
if ($PipIndexUrl) { $env:SANDBOX_PIP_INDEX_URL = $PipIndexUrl }
if ($PipTrustedHost) { $env:SANDBOX_PIP_TRUSTED_HOST = $PipTrustedHost }

Write-Host "Starting sandbox MCP server..."
Write-Host "  server python : $PortablePython"
Write-Host "  kernel python : $env:SANDBOX_KERNEL_PYTHON"
Write-Host "  workspace     : $env:SANDBOX_WORKSPACE"
if ($env:SANDBOX_PIP_INDEX_URL) {
    Write-Host "  pip index     : $env:SANDBOX_PIP_INDEX_URL"
} else {
    Write-Warning "No pip index configured - install_python_packages will fail on a closed network. Set `$PipIndexUrl above."
}

& $PortablePython (Join-Path $Here "server.py")
