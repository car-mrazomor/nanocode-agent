# Configuration

Settings live in `~/.nanocode/config.json` (readable only by you). Most are set for you by `nanocode setup`, `/model`, `/think` and the other commands; you only need to edit the file for the rarer ones. `nanocode config` (or `/config`) prints the current settings with secrets hidden.

## Settings

| Key | Values | Default | What it does |
|---|---|---|---|
| `provider` | `claude`, `openrouter`, `ollama`, or `openai`, `anthropic`, `gemini`, `deepseek`, `groq`, `mistral`, `xai`, `together`, `fireworks`, `lmstudio`, `custom` | `claude` | Where the model runs ([more providers](providers.md)) |
| `models` | `{"claude": "...", "openrouter": "...", "ollama": "..."}` | `claude-opus-5-5`, `openrouter/auto-code` | Chosen model per provider |
| `api_keys` | `{"openai": "sk-...", ...}` | | Keys for the [other providers](providers.md) (`/provider key <name> <key>`) |
| `providers` | `{"custom": {"base_url", "api_key", "models", "headers", "max_tokens", "reasoning"}}` | | Your own OpenAI-compatible API, or changes to a built-in one ([more](providers.md#your-own-api-custom)) |
| `permissions` | `ask`, `auto` | `ask` | Ask before edits and commands, or not |
| `think` | `off`, `low`, `medium`, `high` | `medium` | Thinking level ([more](getting-started.md#thinking)) |
| `theme` | `opencode`, `nanocode`, `tokyonight`, `catppuccin`, `gruvbox`, `nord`, `dracula`, `everforest`, `one-dark`, `solarized-dark`, `kanagawa`, `rosepine`, `github-light` | `opencode` | Base colors ([more](themes.md)) |
| `colors` | `{"accent": "#ff79c6", ...}` | | Your own colors on top of the theme (`/theme`) |
| `terminal_title` | `true`, `false` | `true` | Name the terminal tab after the chat ([more](using.md#tab-title-and-notifications)) |
| `notify` | `auto`, `always`, `off` | `auto` | Bell and desktop notification when a reply finishes while you're in another tab |
| `openrouter_api_key` | string | | OpenRouter key (or `OPENROUTER_API_KEY`) |
| `ollama_url` | URL | `http://localhost:11434` | Ollama server (or `OLLAMA_HOST`) |
| `ollama_num_ctx` | number | 8k, 16k or 32k from your RAM | Ollama context size |
| `context_tokens` | number | `150000` | Context size used for the "context used" meter on cloud models |
| `auto_compact` | `true`, `false` | `true` | Summarize the chat before a message when it's over 80% of the context, instead of trimming old messages |
| `prompt_cache` | `true`, `false` | `true` | Ask Claude models on OpenRouter to cache the prompt (much cheaper long chats) |
| `agent_edits` | `true`, `false` | `false` | Let sub-agents edit files and run commands |
| `auto_learn` | `true`, `false` | `false` | Review a turn and save lessons when tools failed more than once |
| `usage_log` | `true`, `false` | `true` | Log every reply's provider, model, tokens and cost to `~/.nanocode/usage.jsonl`, for usage trackers ([more](usage-tracking.md)) |
| `self_improve` | `true`, `false` | `false` | Quietly learn from hard turns in the background ([more](self-improve.md)) |
| `skills_hub` | `owner/repo` | `anthropics/skills` | Where `/skills hub` finds skills ([more](skills-hub.md)) |
| `hooks` | object | | Your shell commands before and after tools, at turn end, at startup ([more](hooks.md)) |
| `keybinds` | `{"action": "key"}` | | Your own key bindings ([more](keybinds.md)) |
| `trusted_plugins` | list of hashes | | Project plugins you trusted, set when you answer the question ([more](plugins.md)) |
| `search_engine` | `duckduckgo`, `bing`, `google`, `openrouter` | `duckduckgo` | Engine for `web_search` ([more](web.md)) |
| `search_model` | model id | your model, or `openrouter/auto` | Model for `search_engine: openrouter` |
| `browser` | path | auto-detected | Browser used for web pages |
| `browser_headless` | `true`, `false` | `true` | `false` shows the window the browser tools drive ([more](browser.md)) |
| `browser_cdp_url` | URL | | Drive a browser that's already running (`http://127.0.0.1:9222`) instead of starting one |
| `shell_backend` | `auto`, `tmux`, `screen`, `builtin` | `auto` | Background shells ([more](shells.md)) |
| `mcp_servers` | object | | Your MCP servers, local or remote ([more](mcp.md)) |
| `mcp_disabled` | list | | MCP servers and tools switched off |
| `compare_models` | list (up to 6) | | Models for `/compare` |
| `rea` | `true`, `false` | `true` | REA on or off |
| `email` | object | | Email account, set by `nanocode email` |
| `keyboard_protocol` | `auto`, `on`, `off` | `auto` | Enhanced keyboard protocol (kitty, Ghostty, WezTerm, foot) |
| `permission` | object | | Allow, ask or deny tools by command, path or URL pattern ([more](permissions.md)) |
| `snapshots` | `true`, `false` | `true` | Checkpoints for `/undo`, `/redo` and `/rollback` ([more](snapshots.md)) |
| `snapshots_max_files` | number | `20000` | Don't snapshot projects with more files than this |
| `formatters` | `false`, or `{".py": "black {file}"}` | auto-detected | Format files after the model changes them ([more](formatters.md)) |
| `lsp` | `false`, or `{"python": "pylsp"}` | auto-detected | Language-server errors after edits ([more](lsp.md)) |
| `update_check` | `true`, `false` | `true` | Daily check for a new version |
| `api` | `{"key", "host", "port", "permissions"}` | made on first `nanocode serve` | The HTTP API ([more](api.md)) |
| `environment` | `{"type": "local"}`, `docker`, `ssh`, `apptainer`, `singularity`, `custom` | `local` | Where shell commands run ([more](environments.md)) |
| `gateway` | `{"workdir", "permissions", "telegram", "discord", "slack", "whatsapp", "matrix"}` | made by `nanocode gateway setup` | The messaging gateway: tokens, allow lists, `read-only` or `auto` ([more](gateway.md#settings)) |
| `cron` | `{"permissions", "timeout", "in_app"}` | `read-only`, `1800`, `true` | Scheduled tasks: may they edit, how long a run may take, run them while the app is open ([more](cron.md)) |
| `voice` | `{"speak", "tts", "stt_url", ...}` | | Replies read aloud and speech input ([more](voice.md#settings)) |

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
| `GITHUB_TOKEN` (or `GH_TOKEN`) | Used by `/skills install` and `/skills hub` to avoid GitHub's rate limit |
| `VISUAL`, `EDITOR` | The editor Ctrl+X E opens ([more](keybinds.md)) |
| `NANOCODE_NO_UPDATE_CHECK=1` | Turn off the update check |
| `OPENROUTER_API_KEY` | OpenRouter key, if not in the config |
| `OLLAMA_HOST` | Ollama server, if not in the config |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `XAI_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY` | Keys for the [other providers](providers.md), if not in the config |
| `NANOCODE_API_KEY`, `NANOCODE_API_HOST`, `NANOCODE_API_PORT` | The HTTP API's key, address and port ([more](api.md)) |
| `NANOCODE_NO_SEARCH_INDEX=1` | Don't update the chat search index after each turn (`/search` still updates it) ([more](chats.md)) |
| `NANOCODE_PROVIDER`, `NANOCODE_MODEL` | Provider and model for `nanocode github run` ([more](github.md)) |
| `OPENAI_API_KEY` | Speech to text through OpenAI, when there's no local whisper ([more](voice.md#speech-to-text)) |
| `ELEVENLABS_API_KEY`, `WHISPER_CPP_MODEL` | ElevenLabs voice, and a whisper.cpp model file ([more](voice.md)) |

## Project files

| Path | What |
|---|---|
| `.nanocode/sessions/` | Saved chats for this project |
| `.nanocode/memory.md` | Project memory |
| `.nanocode/undo/` | Backups for `/undo` |
| `.nanocode/snapshots/` | Checkpoints for `/undo`, `/redo` and `/rollback` (a separate git repository) |
| `.nanocode/shells/` | Background shell output |
| `.nanocode/skills/` | Project skills (can be committed) |
| `.nanocode/agents/` | Project [custom agents](custom-agents.md) (can be committed) |
| `.nanocode/commands/` | Project [custom commands](custom-commands.md) (can be committed) |
| `.nanocode/plugins/` | Project [plugins](plugins.md), loaded once you trust them |
| `.nanocode/api.json` | Stored API responses (`previous_response_id`) |
| `.nanocode/images/` | Images you attached (or sent through the API), used by the chats that have them |
| `~/.nanocode/search.db` | The search index of all your chats, for `/search`, `/stats all` and `session_search` ([more](chats.md)) |

## Files in ~/.nanocode

| Path | What |
|---|---|
| `config.json` | Your settings and keys |
| `cron.json` | [Scheduled tasks](cron.md) |
| `cron/<id>/` | Their answers, one file per run |
| `gateway/` | The [gateway](gateway.md)'s pid file, log, and which chat is which session |
