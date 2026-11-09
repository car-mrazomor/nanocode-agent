# Configuration

Settings live in `~/.nanocode/config.json` (readable only by you). Most are set for you by `nanocode setup`, `/model`, `/think` and the other commands; you only need to edit the file for the rarer ones. `nanocode config` (or `/config`) prints the current settings with secrets hidden.

## Settings

| Key | Values | Default | What it does |
|---|---|---|---|
| `provider` | `claude`, `openrouter`, `ollama` | `claude` | Where the model runs |
| `models` | `{"claude": "...", "openrouter": "...", "ollama": "..."}` | `claude-opus-5-5`, `openrouter/auto-code` | Chosen model per provider |
| `permissions` | `ask`, `auto` | `ask` | Ask before edits and commands, or not |
| `think` | `off`, `low`, `medium`, `high` | `medium` | Thinking level ([more](getting-started.md#thinking)) |
| `openrouter_api_key` | string | | OpenRouter key (or `OPENROUTER_API_KEY`) |
| `ollama_url` | URL | `http://localhost:11434` | Ollama server (or `OLLAMA_HOST`) |
| `ollama_num_ctx` | number | 8k, 16k or 32k from your RAM | Ollama context size |
| `context_tokens` | number | `150000` | Context size used for the "context used" meter on cloud models |
| `agent_edits` | `true`, `false` | `false` | Let sub-agents edit files and run commands |
| `auto_learn` | `true`, `false` | `false` | Review a turn and save lessons when tools failed more than once |
| `search_engine` | `duckduckgo`, `bing`, `google`, `openrouter` | `duckduckgo` | Engine for `web_search` ([more](web.md)) |
| `search_model` | model id | your model, or `openrouter/auto` | Model for `search_engine: openrouter` |
| `browser` | path | auto-detected | Browser used for web pages |
| `shell_backend` | `auto`, `tmux`, `screen`, `builtin` | `auto` | Background shells ([more](shells.md)) |
| `mcp_servers` | object | | Your MCP servers ([more](mcp.md)) |
| `mcp_disabled` | list | | MCP servers and tools switched off |
| `compare_models` | list (up to 6) | | Models for `/compare` |
| `rea` | `true`, `false` | `true` | REA on or off |
| `email` | object | | Email account, set by `nanocode email` |
| `keyboard_protocol` | `auto`, `on`, `off` | `auto` | Enhanced keyboard protocol (kitty, Ghostty, WezTerm, foot) |
| `update_check` | `true`, `false` | `true` | Daily check for a new version |

Example:

```json
{
  "provider": "openrouter",
  "models": {"openrouter": "anthropic/claude-sonnet-5", "ollama": "qwen3"},
  "permissions": "ask",
  "think": "high",
  "search_engine": "bing",
  "agent_edits": false
}
```

## Environment variables

| Variable | What it does |
|---|---|
| `NANOCODE_HOME` | Use another folder instead of `~/.nanocode` |
| `NANOCODE_NO_UPDATE_CHECK=1` | Turn off the update check |
| `OPENROUTER_API_KEY` | OpenRouter key, if not in the config |
| `OLLAMA_HOST` | Ollama server, if not in the config |

## Project files

| Path | What |
|---|---|
| `.nanocode/sessions/` | Saved chats for this project |
| `.nanocode/memory.md` | Project memory |
| `.nanocode/undo/` | Backups for `/undo` |
| `.nanocode/shells/` | Background shell output |
| `.nanocode/skills/` | Project skills (can be committed) |
