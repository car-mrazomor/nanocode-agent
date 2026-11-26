# HTTP API

`nanocode serve` turns nanocode into an **OpenAI-compatible API**, like [hermes-agent](https://github.com/NousResearch/hermes-agent)'s API server. Anything that talks to OpenAI (curl, the `openai` SDK, Open WebUI, LobeChat, LibreChat, your own scripts) can then use nanocode as a "model", with all its tools: reading and editing code, commands, web search, MCP, memory and skills.

The code lives in [`api/server.py`](../api/server.py), separate from `nanocode.py`.

## Start it

```sh
cd your-project
nanocode serve                      # http://127.0.0.1:8765/v1
nanocode serve --port 9000          # another port
nanocode serve --host 0.0.0.0       # reachable from other machines (careful: see Security)
```

It uses your normal provider, model and settings, and works in the folder you start it in. The first start makes an API key and saves it in `~/.nanocode/config.json` under `"api"`.

```sh
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "nanocode", "messages": [{"role": "user", "content": "What does this project do?"}]}'
```

With the OpenAI Python SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key=KEY)
reply = client.chat.completions.create(model="nanocode", messages=[{"role": "user", "content": "Run the tests"}])
print(reply.choices[0].message.content)
```

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions. Stateless: send the whole conversation each time. `"stream": true` for SSE |
| `POST` | `/v1/responses` | OpenAI Responses API, with stored conversations (`previous_response_id`, `conversation`). `"stream": true` for SSE |
| `GET`, `DELETE` | `/v1/responses/{id}` | Read or delete a stored response |
| `POST` | `/v1/runs` | Start a turn in the background; returns a `run_id` right away |
| `GET` | `/v1/runs/{id}` | The run's status, output and usage |
| `GET` | `/v1/runs/{id}/events` | SSE: every event so far, then live ones, until the run ends |
| `POST` | `/v1/runs/{id}/stop` | Stop a run |
| `GET` | `/v1/models` | Lists `nanocode` (for apps that need a model list) |
| `GET` | `/v1/capabilities` | What this server supports, for apps that check first |
| `GET` | `/v1/tools`, `/v1/skills` | nanocode's tools and your skills |
| `GET` | `/v1/sessions` | This folder's saved chats (id, title, dates, model), newest first |
| `GET` | `/v1/sessions/{id}` | One saved chat: its turns (your messages, replies, tool calls and results) |
| `GET` | `/v1/sessions/{id}/export?format=md` | A chat as Markdown (or `json`): `{"filename", "content"}` |
| `POST` | `/v1/sessions/{id}/fork` | A copy of a chat under a new id, to try another direction |
| `GET` | `/v1/search?q=words` | This folder's chats that mention the words, best first, with the matching part |
| `GET` | `/v1/workspace` | The model and the models to pick from, thinking level, mode, themes, and the commands a client may run |
| `GET` | `/` | The [web app](web-ui.md) (no key needed for the page; it asks for the key) |
| `GET` | `/health`, `/v1/health` | Liveness check (no key needed) |

### Chat completions

Standard request and response. `system` messages are added as extra instructions on top of nanocode's own prompt, so the agent keeps its tools and memory. The reply's text is the agent's answer after it has finished all its tool calls.

- **Streaming:** normal `chat.completion.chunk` events, plus `event: nanocode.tool.progress` when a tool starts (`{"tool": "read", "arguments": {...}}`), and `[DONE]` at the end.
- **Continuing a session instead of resending history:** every reply has an `X-Nanocode-Session-Id` header. Send it back as a request header and only your new message is sent; nanocode continues that chat.

### Responses

```json
{"input": "What files are in src/?", "instructions": "Be brief.", "conversation": "my-project"}
```

- `input`: a string, or a list of messages.
- **Multi-turn:** `previous_response_id` continues from a stored response; `conversation` (a name you choose) always continues the latest response in it.
- **Output** lists the tools nanocode ran as `function_call` and `function_call_output` items (already done, for showing in a UI), then the `message`.
- `store: false` skips storing. The last 100 responses are kept in `.nanocode/api.json`, so they survive restarts.
- **Streaming:** `response.created`, `response.output_text.delta`, `response.output_item.added` / `.done` for tools, and `response.completed`.

### Runs

For dashboards and scripts that start work and check on it later:

```sh
curl -X POST .../v1/runs -d '{"input": "fix the failing test", "session_id": "ci-fix"}'   # → {"run_id": "run_…"}
curl -N .../v1/runs/run_…/events                                                      # live events
curl .../v1/runs/run_…                                                                # status and output
```

Events: `run.started`, `delta` (reply text), `tool.started`, `tool.completed`, `usage`, then `run.completed`, `run.failed` or `run.cancelled`.

- `"mode": "plan"` (or `"build"`) sets the mode before the turn: in plan mode the model only reads and plans.
- `"command": true` runs `input` as a slash command, as if typed in the terminal: `{"input": "/stats", "command": true}`. What it prints comes as `output` events (with the terminal's colors as ANSI codes); a command that starts a turn (`/compact`, a skill) also streams it as usual. `/undo`, `/redo` and `/rollback <n>` need `"confirmed": true`, meaning you asked the person first. Commands that need a terminal or change the server (`/tab`, `/editor`, `/provider`, `/update`, `/auto`…) are refused with `400`; `GET /v1/workspace` lists the ones you can run. With a read-only API, only commands that change nothing are allowed.

## Images

Send images as base64 in the last user message, the same way as with OpenAI:

```python
client.chat.completions.create(model="nanocode", messages=[{"role": "user", "content": [
    {"type": "text", "text": "What's wrong in this screenshot?"},
    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
]}])
```

From a file with curl (`-d @-` because base64 is too long for the command line):

```sh
jq -n --rawfile img <(base64 -i shot.png | tr -d '\n') '{model: "nanocode", messages: [{role: "user", content: [
  {type: "text", text: "What is wrong here?"},
  {type: "image_url", image_url: {url: ("data:image/png;base64," + $img)}}]}]}' |
curl http://127.0.0.1:8765/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d @-
```

- `/v1/responses` and `/v1/runs` take `{"type": "input_image", "image_url": "data:..."}` too.
- PNG, JPEG, WebP and GIF, up to 20 MB each. The type is read from the bytes, so `image/png` on a JPEG is fine; plain base64 without the `data:` prefix works too.
- Image URLs (`https://...`) aren't downloaded: send base64.
- The model has to see images: a vision model on OpenRouter, a vision model in Ollama (`qwen2.5vl`, `llava`, `gemma3`), or Claude.
- Images are saved in `.nanocode/images/` and the session keeps their names, so a continued chat still has them.

## Sessions

Every API conversation is a normal saved nanocode chat in `.nanocode/sessions/`, so you can open it later in the app with `/resume`. A `session_id` you choose (runs, or the `X-Nanocode-Session-Id` header) names it, and later requests with the same id continue it.

nanocode works on one chat at a time, so requests run **one after another**: a second request waits until the first one is done. Up to 20 can wait; more get `429`. If a client disconnects (or times out) before its turn is done, the turn is stopped, so a retry doesn't run the same edits twice. (A client that closes its sending side right after the request counts as disconnected, so keep the connection open until the answer arrives; all common HTTP clients do.)

## Permissions

There's nobody to ask, so the API decides up front:

| `"api": {"permissions": ...}` | Edits and commands |
|---|---|
| `"auto"` (default on `127.0.0.1`) | allowed |
| `"read-only"` (default with any other `--host`) | denied; the agent can read, search and answer |

Listening beyond this machine, edits and commands stay off until you set `"permissions": "auto"` yourself.

Either way, **dangerous commands** (`rm -rf /`, `git push --force`, …) and **sending email** are always denied over the API.

## Settings

| Where | Host | Port | Key |
|---|---|---|---|
| Flags | `--host` | `--port` | |
| Environment | `NANOCODE_API_HOST` | `NANOCODE_API_PORT` | `NANOCODE_API_KEY` |
| `~/.nanocode/config.json` | `"api": {"host": ...}` | `"api": {"port": ...}` | `"api": {"key": ...}` |
| Default | `127.0.0.1` | `8765` | made on first start |

Flags win over environment variables, which win over the config.

## Security

- **The key is always required** (except `/health`), even on `127.0.0.1`. Anyone with it can make nanocode edit files and run commands in that folder.
- It listens on `127.0.0.1` by default: only this machine. With `--host 0.0.0.0` it warns you; put it behind HTTPS (nginx, Caddy, a tunnel) if it's reachable from the internet.
- Use `"permissions": "read-only"` when you only need answers.
- There's no browser CORS: apps like Open WebUI connect server to server, which is what you want.

## Not supported (yet)

- File uploads, and image URLs (send images as base64)
- Choosing the model per request: the server uses the model you set in nanocode
- Running several turns at the same time
