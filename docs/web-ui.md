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

A sidebar with every nanocode feature, and the chat next to it:

- **Chats:** this project's saved chats; click one to continue it. The `⋯` menu (or a right-click) forks a chat or downloads it as Markdown or JSON. **New chat** is `Ctrl+Shift+O`.
- **Search** (`Ctrl+K`): every chat in this folder that mentions your words, with the matching part.
- **Tools**, each a page with its terminal output and buttons for the usual actions:

| Page | What you can do there |
|---|---|
| Memory | See what nanocode remembers; add a note |
| Skills | List skills, search the skills hub, install one |
| Agents | List custom agents, create one |
| Commands | Your custom slash commands and every built-in one |
| Tasks | Scheduled prompts: add, pause, resume, remove, run now |
| Checkpoints | Every turn's file changes: undo, redo, restore to before a turn |
| Stats | Usage for this project, all projects, or this chat |
| MCP | Servers and their tools; add one, turn tools on or off |
| Permissions | Allow, ask or deny rules |
| Environment | Where commands run: here, Docker, SSH, Apptainer |
| Hooks, Plugins | What's set up; trust this project's plugins |
| Theme | Every nanocode theme, for this page (kept in this browser) |
| Help | Every command |

In the message box:

- **`/` opens the command menu**: the same commands as in the terminal (`/compact`, `/init`, `/recap`, `/undo`, `/rollback`, `/think`, your skills and custom commands…), with ↑ ↓ and Tab. Their output shows in the chat, colored as in the terminal. `# note` saves to memory.
- **Model picker** and **thinking level**, at the bottom right.
- **Build / Plan**: in plan mode the model only reads and plans (`Shift+Tab` switches, as in the terminal).
- **Images:** attach, paste or drop them.
- The reply streams with Markdown (code blocks have a copy button); each tool call is a line you can click open; **Stop** (or Esc) stops the turn. Tokens and cost after each reply, and a browser notification when a reply finishes in a background tab.

`/undo`, `/redo` and `/rollback` ask you first in the page. Commands that need a terminal (tabs, the editor, `/terminal-setup`) or change the server itself (`/provider`, `/update`, `/auto`) aren't offered.

The page is one file (`nanokit/web/index.html`), with no outside scripts or fonts. It uses the HTTP API like any other client (see [HTTP API](api.md)).

## Install it as an app

The page is a Progressive Web App. In Chrome or Edge, click the install icon in the address bar (or menu → Install nanocode); on iOS Safari, Share → Add to Home Screen. It then opens in its own window like a desktop app. The server still has to be running (`nanocode web --no-open` or `nanocode serve`).

## Safety

On `127.0.0.1` (the default) edits and commands are allowed, as with `nanocode serve`; dangerous commands are always refused. Anyone who has the key can use nanocode as you, so don't share it. With `"api": {"permissions": "read-only"}` the page can still chat, search, and show every page, but commands that change things (undo, memory notes, installing skills, permission rules…) are refused. To reach it from another device, see the [HTTP API's security notes](api.md).
