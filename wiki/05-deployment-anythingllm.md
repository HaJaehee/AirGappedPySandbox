# 05 — Deployment into AnythingLLM

## Prerequisite

The package unzipped on the host at some `<BASE>` (e.g. `D:\AirGappedPySandbox`),
so that these exist:
- `<BASE>\WPy64-313130\python\python.exe` (portable Python)
- `<BASE>\mcp-server\server.py` (server code)

## Step 1 — Final offline self-check

```powershell
<BASE>\WPy64-313130\python\python.exe <BASE>\mcp-server\check_environment.py
```
Expect `RESULT: PASS`.

## Step 2 — Register the MCP server

Copy `mcp-server\anythingllm_mcp_config.example.json` into AnythingLLM's MCP
config (**Settings → Agent Skills → MCP Servers**, or edit
`~/AnythingLLM/plugins/anythingllm_mcp_servers.json`), fixing `<BASE>`:

```jsonc
{
  "mcpServers": {
    "air-gapped-python-sandbox": {
      "command": "D:\\AirGappedPySandbox\\WPy64-313130\\python\\python.exe",
      "args": ["D:\\AirGappedPySandbox\\mcp-server\\server.py"],
      "env": {
        "SANDBOX_KERNEL_PYTHON": "D:\\AirGappedPySandbox\\WPy64-313130\\python\\python.exe",
        "SANDBOX_WORKSPACE": "D:\\AirGappedPySandbox\\mcp-server\\workspace",
        "SANDBOX_EXEC_TIMEOUT": "60"
      }
    }
  }
}
```

Restart the agent, then enable the tools in the workspace's agent skills.

## Step 3 — Getting files into the sandbox

Drop the target PDF/Excel/CSV into the folder that `SANDBOX_WORKSPACE` points at
(`<BASE>\mcp-server\workspace`). The LLM finds them with `list_workspace_files`
and reads them with plain relative names (`report.pdf`) — the kernel runs *inside*
that folder. Generated outputs land there too and come back as Markdown links.

## Runtime notes

- **Transport:** stdio. AnythingLLM launches `server.py` as a child process; MCP
  messages flow over stdin/stdout.
- **Warm-start:** on launch the server pre-boots the kernel (~2.5 s on WinPython)
  so the first tool call is fast, and to surface config errors early. Disable
  with `SANDBOX_LAZY_START=1` if you want faster server startup at the cost of a
  slower first call. See `07`.
- **Loopback only:** the kernel↔server ZeroMQ channel is on `127.0.0.1`. This
  works on a fully air-gapped machine (loopback needs no network adapter). The
  "Kernel is running over TCP" warning in logs refers to this local channel, not
  external traffic.

## Smoke test after wiring (optional)

Ask the agent something that forces a tool call, e.g. "compute the definite
integral of x·sin(x) from 0 to π and plot it, saving the chart" — you should get
a numeric answer plus an inline chart image link from `./workspace`.
