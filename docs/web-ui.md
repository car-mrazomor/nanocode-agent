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

- **Chats:** this project's saved chats; click one to continue it. The `⋯` menu (or a right-click) forks a chat, downloads it as Markdown or JSON, or deletes it. **New chat** is `Ctrl+Shift+O`.
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
| Settings | Delete chats (pick several, or all), and what this server runs |
| Help | Every command |

In the message box:

- **`/` opens the command menu**: the same commands as in the terminal (`/compact`, `/init`, `/recap`, `/undo`, `/rollback`, `/think`, your skills and custom commands…), with ↑ ↓ and Tab. Their output shows in the chat, colored as in the terminal. `# note` saves to memory.
- **Model picker** and **thinking level**, at the bottom right.
- **Build / Plan**: in plan mode the model only reads and plans (`Shift+Tab` switches, as in the terminal).
- **Files and images:** attach, paste or drop them. Images go to the model as images. Any other file is saved in the project's `.nanocode/uploads/`: a text file (code, CSV, logs…) is attached like `@file` in the terminal, and anything else (PDF, zip, spreadsheet) is there for the model to open with its tools. Up to 25 MB each.
- The reply streams with Markdown (code blocks have a copy button); each tool call is a line you can click open; **Stop** (or Esc) stops the turn. Tokens and cost after each reply, and a browser notification when a reply finishes in a background tab.

Reloading the page (or opening it on another device) keeps everything: every chat shows its whole history, on every provider, and a reply that's still being written is picked up where it is.

`/undo`, `/redo` and `/rollback` ask you first in the page. Commands that need a terminal (tabs, the editor, `/terminal-setup`) or change the server itself (`/provider`, `/update`, `/auto`) aren't offered.

The page is one file (`nanokit/web/index.html`), with no outside scripts or fonts. It uses the HTTP API like any other client (see [HTTP API](api.md)).

## Install it as an app

The page is a Progressive Web App. In Chrome or Edge, click the install icon in the address bar (or menu → Install nanocode); on iOS Safari, Share → Add to Home Screen. It then opens in its own window like a desktop app. The server still has to be running (`nanocode web --no-open` or `nanocode serve`).

## Safety

On `127.0.0.1` (the default) edits and commands are allowed, as with `nanocode serve`; dangerous commands are always refused. Anyone who has the key can use nanocode as you, so don't share it. With `"api": {"permissions": "read-only"}` the page can still chat, search, and show every page, but commands that change things (undo, memory notes, installing skills, permission rules…) are refused. To reach it from another device, see the [HTTP API's security notes](api.md).
