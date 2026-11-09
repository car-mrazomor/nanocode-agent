# Using nanocode

Type what you want done. nanocode reads, searches, edits, runs commands and tests, and shows a diff before every change. After each reply a dim line shows the time, tokens and cost:

```
  ✻ Cooked for 11s · 12.3k in · 340 out · $0.0123 · done 19:06
```

The cost is OpenRouter's real price, "free (local)" on Ollama, and on the Claude subscription what the same usage would cost on the API (covered by your plan).

## Commands

| Command | What it does |
|---|---|
| `/provider [name]` | List providers, or switch |
| `/model [name]` | List models, or switch |
| `/think [level]` | `off`, `low`, `medium` (default), `high` |
| `/resume [n]` | Continue a saved chat (every chat is saved per project) |
| `/recap` | This session: files changed, commands run, cost, and what's next |
| `/compact` | Summarize a long chat to free context |
| `/undo` | Revert the last file change |
| `/learn` | Review this chat and save lessons and skills |
| `/memory`, `/skills` | Show memory and skills |
| `/shells` | Background shells; `/shells stop <name>` |
| `/auto` | Toggle auto-approve |
| `/rea` | REA status; `/rea on`, `/rea off` |
| `/mcp` | MCP servers: `add`, `remove`, `import`, `tools`, `on`/`off` |
| `/compare <prompt>` | Several models side by side; `/compare models a, b, c` |
| `/email [task]` | Inbox summary, or an email task |
| `/update`, `/version` | Install an update, or check for one |
| `/terminal-setup` | Make Shift+Enter and Cmd shortcuts work |
| `/cost`, `/config`, `/help` | Usage, settings, help |
| `/c` (`/new`, `/clear`) | New chat |
| `/q` (`/exit`, `/quit`) | Quit |
| `# note` | Save a note to project memory |
| `@path` | Attach a file |
| `/<skill> [task]` | Run a skill, e.g. `/review app.py` |

## The interface

nanocode opens on a start screen with the logo and the prompt in the middle. When you send something, it switches to the chat view, using the full terminal width.

- **Transcript.** Your messages are blocks with an accent bar. Tool calls are short lines: `→ Read`, `← Edit` with a diff, `$ Bash` with output. Each reply ends with a `▣` line: time, model, tokens, cost.
- **Prompt panel.** At the bottom: mode (Ask or Auto), model, provider and thinking level. Below it the folder and git branch, context used and cost, and while a reply runs, a spinner and `esc interrupt`.
- **Pinned message.** While you scroll through a long answer, your message stays pinned at the top. Click it to jump back.
- **Command palette.** `ctrl+p` lists every command and action, searchable.

## Keys

| Key | Action |
|---|---|
| Enter | Send |
| Shift+Enter, Alt+Enter, Ctrl+J | New line |
| Ctrl+P | Command palette |
| Ctrl+X then N / L / M / C / U / H / Q | New chat / chats / models / compact / undo / help / quit |
| Esc Esc | Stop the reply |
| Ctrl+C | Stop the reply, clear the input, or quit |
| Ctrl+U (Cmd+Backspace on macOS) | Delete to the start of the line |
| Ctrl+← →, Alt/Option+← → | Jump by word |
| Home / End, Ctrl+A / Ctrl+E | Start / end of line |
| ↑ / ↓ | History |
| PageUp / PageDown, mouse wheel | Scroll the chat |
| Tab | Complete |

## Mouse

- **Select** by dragging in the chat; it's copied when you let go. Double-click copies a word. Dragging past the top or bottom edge scrolls, after a short pause.
- **Copying** uses the system clipboard (`pbcopy`, `wl-copy`, `xclip`, tmux) locally and OSC 52 over SSH.
- **In the prompt**, selection works like a normal text box: typing replaces the selection, Backspace deletes it.
- **The wheel** scrolls the chat wherever the pointer is, faster when you spin it faster.
- **Your terminal's own selection:** hold Shift while dragging (Option in iTerm2 and VS Code on macOS, Fn in Terminal.app).

## Terminal setup

Most terminals send the same thing for Shift+Enter as for Enter, and many keep Cmd shortcuts to themselves. Run `/terminal-setup` once:

- **kitty, Ghostty, VS Code, Cursor, Windsurf:** it adds the key mappings to the terminal's config (asks first, makes a backup).
- **iTerm2, WezTerm, Alacritty, Windows Terminal, Terminal.app:** it shows the steps.

Alt+Enter and Ctrl+J insert a new line everywhere. In kitty, Ghostty, WezTerm and foot, nanocode uses the enhanced keyboard protocol automatically (`"keyboard_protocol": "on" | "off" | "auto"`).

## Scripting

```sh
nanocode -p "explain this error" < build.log                  # one-shot, answer to stdout
nanocode -p - < prompt.txt                                    # prompt from stdin
nanocode -p "fix the failing test" --yes                      # allow edits and commands
nanocode -c                                                   # continue the last chat in this folder
nanocode --provider ollama -m qwen3 -p "summarize README.md"  # one-off provider and model
```

| Flag | Meaning |
|---|---|
| `-p, --print PROMPT` | Run one prompt without the UI |
| `-c, --continue` | Continue the last chat here |
| `-y, --yes` | Allow edits and commands without asking |
| `-m, --model` | Model for this run |
| `--provider` | `claude`, `openrouter` or `ollama` for this run |
| `-v, --version` | Print the version |

Subcommands: `setup`, `config`, `doctor`, `email`, `update`, `version`, `uninstall [--purge]`.
