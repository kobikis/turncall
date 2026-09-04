# MCP Tools Example

Connect TurnCall agents to MCP (Model Context Protocol) servers for dynamic tool discovery.

## What It Does

Instead of defining webhook tools in the agent config, you point the agent at an MCP server. TurnCall connects at call start, discovers available tools automatically, and routes tool calls through the MCP protocol.

**No webhook URLs needed — tools are self-describing.**

## Quick Start

```bash
# Terminal 1: Start the MCP server (CRM tools)
cd examples/mcp-tools
python mcp_server.py

# Terminal 2: Start TurnCall
make run

# Terminal 3: Setup agent + phone number
python setup.py \
  --twilio-number-sid PN_YOUR_SID \
  --twilio-number +15551234567

# Call your number — agent discovers and uses MCP tools
```

## The MCP Server

`mcp_server.py` exposes two tools via MCP:

| Tool | Description |
|------|-------------|
| `lookup_customer` | Look up customer by phone number |
| `create_ticket` | Create a support ticket |

Run in HTTP mode (default) or stdio mode:

```bash
python mcp_server.py              # HTTP on port 9002
python mcp_server.py --stdio      # stdio (local subprocess)
```

## Agent Config

```json
{
  "name": "support-agent",
  "config": {
    "system_prompt": "You are a support agent...",
    "mcp_servers": [
      {
        "name": "crm-tools",
        "transport": "http",
        "url": "http://localhost:9002/mcp"
      }
    ]
  }
}
```

## Transport Options

| Transport | Config | Use Case |
|-----------|--------|----------|
| `http` | `"url": "https://..."` | Remote servers (recommended) |
| `sse` | `"url": "https://..."` | Remote, server-push events |
| `stdio` | `"command": "python", "args": [...]` | Local servers, zero latency |

### HTTP (default)
```json
{
  "name": "crm",
  "transport": "http",
  "url": "https://mcp.example.com/mcp",
  "headers": {"Authorization": "Bearer ..."}
}
```

### stdio (local)
```json
{
  "name": "local-db",
  "transport": "stdio",
  "command": "python",
  "args": ["mcp_server.py", "--stdio"],
  "env": {"DB_URL": "postgres://..."}
}
```

Requires `MCP_STDIO_ENABLED=true` in `.env`.

## MCP vs Webhook Tools

| | Webhook Tools | MCP Tools |
|--|---------------|-----------|
| Config | Name, description, params, webhook_url | MCP server URL only |
| Discovery | Manual (you define the schema) | Automatic (server describes its tools) |
| Updates | Update agent config via API | Update the MCP server (agents pick up changes) |
| Execution | HTTP POST to webhook_url | MCP protocol (JSON-RPC) |
| Transports | HTTP only | HTTP, SSE, stdio |

## Security

- MCP servers are configured by project admins (same trust model as webhooks)
- stdio is **disabled by default** (opt-in via `MCP_STDIO_ENABLED=true`)
- stdio commands are whitelisted (`MCP_STDIO_ALLOWED_COMMANDS`)
- Connection timeout: configurable per server (default 10s)
- Max tools per server: 50 (configurable via `MCP_MAX_TOOLS_PER_SERVER`)

## Quick run

```bash
./run.sh
```

Reads `TURNCALL_NUMBER`, `TWILIO_PN_SID` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
