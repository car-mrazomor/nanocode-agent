# Web app

nanocode has a chat page for the browser, in nanocode's dark look. It runs on your machine and works in the project folder you start it in.

```sh
cd your-project
nanocode web                  # starts the server on 127.0.0.1:8765 and opens the page
nanocode web --port 9000      # another port
nanocode web --no-open        # don't open the browser
```

`nanocode web` opens the page already connected (the key is passed in the address's `#` part, which never leaves the browser). The page is also served by `nanocode serve` at its root, e.g. `http://127.0.0.1:8765/`: open it, paste the API key once (it's printed when the server starts, and kept in `~/.nanocode/config.json` under `"api"`), and the browser remembers it.

## What it does

- Lists this project's saved chats; click one to continue it, or **+ New** for a new chat.
- Streams the reply as it's written, with simple Markdown (code blocks, lists, tables, links).
- Shows each tool call as a line you can click open for its arguments and output.
- **Stop** (or Esc) stops the turn.
- Tokens and cost after each reply.

The page is one file (`nanokit/web/index.html`), with no outside scripts or fonts. It uses the HTTP API like any other client: `GET /v1/sessions`, `POST /v1/runs` and the run's event stream (see [HTTP API](api.md)).

## Install it as an app

The page is a Progressive Web App. In Chrome or Edge, click the install icon in the address bar (or menu → Install nanocode); on iOS Safari, Share → Add to Home Screen. It then opens in its own window like a desktop app. The server still has to be running (`nanocode web --no-open` or `nanocode serve`).

## Safety

On `127.0.0.1` (the default) edits and commands are allowed, as with `nanocode serve`; dangerous commands are always refused. Anyone who has the key can use nanocode as you, so don't share it. To reach it from another device, see the [HTTP API's security notes](api.md).
