# MCP

Connect any local [MCP](https://modelcontextprotocol.io) server and its tools become nanocode's tools, on every provider.

## Adding servers

```sh
/mcp add github npx -y @modelcontextprotocol/server-github
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

Remote (HTTP) servers aren't supported yet.
