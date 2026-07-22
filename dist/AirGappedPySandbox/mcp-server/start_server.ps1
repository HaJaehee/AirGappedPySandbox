# Launch the Air-Gapped Python Sandbox MCP server.
#
# Edit $PortablePython to point at your 40 GB portable Python interpreter, then
# run:  powershell -ExecutionPolicy Bypass -File .\start_server.ps1
#
# AnythingLLM normally launches server.py for you via its MCP config (see
# anythingllm_mcp_config.example.json). This script is for manual testing.

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- CONFIGURE ME -----------------------------------------------------------
$PortablePython = "C:\path\to\portable-python\python.exe"
# ---------------------------------------------------------------------------

if (-not (Test-Path $PortablePython)) {
    Write-Warning "Portable Python not found at '$PortablePython'. Falling back to the interpreter on PATH."
    $PortablePython = "python"
}

$env:SANDBOX_KERNEL_PYTHON = $PortablePython
$env:SANDBOX_WORKSPACE = Join-Path $Here "workspace"
if (-not $env:SANDBOX_EXEC_TIMEOUT) { $env:SANDBOX_EXEC_TIMEOUT = "60" }

Write-Host "Starting sandbox MCP server..."
Write-Host "  server python : $PortablePython"
Write-Host "  kernel python : $env:SANDBOX_KERNEL_PYTHON"
Write-Host "  workspace     : $env:SANDBOX_WORKSPACE"

& $PortablePython (Join-Path $Here "server.py")
