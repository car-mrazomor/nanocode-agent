# Usage tracking

Trackers that count what each coding agent spends can recognize nanocode in two ways.

## 1. It introduces itself to every provider

Every model call (OpenRouter, OpenAI, Anthropic API, Gemini, DeepSeek, Groq, Mistral, xAI, LM Studio, any OpenAI-compatible endpoint, and Ollama) carries:

```
User-Agent: nanocode/<version> (<os>; <surface>)
X-Title: nanocode
HTTP-Referer: https://github.com/car-mrazomor/nanocode-agent
```

OpenRouter lists it as "nanocode" in your activity and the app rankings; a proxy or gateway in between sees the User-Agent. A header you set yourself for a provider (`"providers": {"<name>": {"headers": {...}}}`) wins over these.

The Claude subscription is the exception: nanocode runs Claude Code for it, so Anthropic and Claude Code's own logs see Claude Code. The usage log below still records those replies as nanocode.

## 2. It logs every reply: `~/.nanocode/usage.jsonl`

One JSON object per line, appended after every reply, from every place nanocode runs:

```json
{"timestamp": "2026-11-27T10:12:03.114Z", "agent": "nanocode", "version": "0.9.2", "provider": "openrouter",
 "billing": "api", "model": "z-ai/glm-5.3", "input_tokens": 12840, "output_tokens": 612, "cost_usd": 0.0193,
 "duration_s": 8.4, "session": "20261127-101155-a3f1", "project": "/home/me/app", "surface": "terminal"}
```

| Field | What it is |
|---|---|
| `timestamp` | When the reply finished, UTC (ISO 8601) |
| `agent` | Always `nanocode` |
| `version` | nanocode's version |
| `provider` | `claude`, `openrouter`, `ollama`, `openai`, `anthropic`, `gemini`, … |
| `billing` | `api` (paid per token), `subscription` (the Claude subscription: `cost_usd` is what the same tokens would cost on the API) or `local` (Ollama: free) |
| `model` | The model id |
| `input_tokens`, `output_tokens` | Tokens of the reply (all its model calls and tool steps) |
| `cost_usd` | Cost, as the provider reported it (0 when it doesn't) |
| `duration_s` | Seconds the reply took |
| `session` | The chat's id (`.nanocode/sessions/<id>.json` in the project) |
| `project` | The folder nanocode worked in |
| `surface` | Where it ran: `terminal`, `api` (the web and iOS apps), `gateway` (chat apps), `acp` (an editor) or `headless` (`-p`, scripts, GitHub) |

Lines are only added, never rewritten, so a tracker can read from where it stopped. `NANOCODE_HOME` moves the file with the rest of `~/.nanocode`. To turn it off: `"usage_log": false` in `~/.nanocode/config.json`.
