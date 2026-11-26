# MCP

Connect any [MCP](https://modelcontextprotocol.io) server, local or remote, and its tools become nanocode's tools, on every provider.

## Adding servers

```sh
/mcp add github npx -y @modelcontextprotocol/server-github
/mcp add linear https://mcp.linear.app/mcp      # a remote server
/mcp import        # copy the servers from Claude Code, Claude Desktop or .mcp.json
/mcp               # list servers and their status
/mcp remove github
```

Servers are stored in `~/.nanocode/config.json`:

```json
"mcp_servers": {
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
  }
}
```

`${VAR}` is filled in from your environment, so secrets can stay out of the file.

## Turning tools on and off

```sh
/mcp tools                  # every tool, and whether it's on
/mcp off github.delete_repo # one tool
/mcp off github             # a whole server
/mcp on github
```

Or `ctrl+p → MCP tools on/off` to toggle from a list. Disabled tools are stored in `"mcp_disabled"`.

## How tools are offered

- Servers start with your **first message**, not at launch.
- A server with **20 tools or fewer** has its tools offered to the model directly, named `server.tool`.
- A **bigger server** is searched on demand with `mcp_search`, `mcp_schema` and `mcp_call`, so the model's tool list stays short.
- Tools **not marked read-only** ask before running.

## Remote servers

A server with a URL instead of a command runs on the internet (or your network):

```json
"mcp_servers": {
  "linear": {"url": "https://mcp.linear.app/mcp"},
  "internal": {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ${MY_TOKEN}"}},
  "old": {"url": "https://example.com/sse", "type": "sse"}
}
```

- Both transports work: **Streamable HTTP** (the current one) and the older **SSE** transport. nanocode tries Streamable HTTP first and falls back to SSE by itself; `"type": "sse"` (or `/mcp add <name> <url> --sse`) skips the first try. On SSE, the server names the URL to send messages to; nanocode only accepts one on the same server (scheme, host and port) as the configured URL, since your headers and login go there.
- `headers` are sent with every request, with `${VAR}` filled in from your environment.
- `/mcp import` copies remote servers from Claude Code and Claude Desktop too.

### Logging in

Servers that need an account (OAuth) say so the first time they're used: `/mcp` shows **needs you to log in**. Then:

```sh
/mcp login linear     # opens your browser; log in and allow access
/mcp logout linear    # forgets the token
```

`/mcp add` with a URL starts the login right away when the server asks for one.

nanocode follows the MCP authorization flow: it finds the server's login service, registers itself there when the service allows it, and waits for the browser on a local address (`http://127.0.0.1:<port>/callback`). Tokens are saved in `~/.nanocode/mcp-tokens.json` (readable only by you) and refreshed when they run out.

If the login service doesn't let apps register themselves, add an app you made there:

```json
"github": {"url": "https://api.githubcopilot.com/mcp/",
           "oauth": {"client_id": "...", "client_secret": "...", "callback_port": 8765, "scope": "repo"}}
```

with `http://127.0.0.1:8765/callback` as the app's redirect URL.
