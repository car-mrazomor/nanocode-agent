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
| `/theme` | Colors: list them, `/theme opencode\|nanocode`, `/theme <color> #rrggbb`, `/theme reset` |
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
| `@path` | Attach a file; images are sent as images. `@"my file.png"` for names with spaces |
| `/<skill> [task]` | Run a skill, e.g. `/review app.py` |

## The interface

nanocode opens on a start screen with the logo and the prompt in the middle. When you send something, it switches to the chat view, using the full terminal width.

- **Transcript.** Your messages are blocks with an accent bar. Tool calls are short lines: `→ Read`, `← Edit` with a diff, `$ Bash` with output. Each reply ends with a `▣` line: time, model, tokens, cost.
- **Prompt panel.** At the bottom: mode (Ask or Auto), model, provider and thinking level. Below it the folder and git branch, context used and cost, and while a reply runs, a spinner and `esc interrupt`.
- **Pinned message.** While you scroll through a long answer, your message stays pinned at the top. Click it to jump back.
- **Command palette.** `ctrl+p` lists every command and action, searchable.

## Tab title and notifications

The terminal tab is named after the chat (`nanocode` before the first message; long titles are cut to 30 characters with `...`), with a mark for what's happening:

| Title | Meaning |
|---|---|
| `✻ Fix the login bug` | Working |
| `△ Fix the login bug` | Waiting for your OK (a permission question) |
| `✓ Fix the login bug` | Finished while you were in another tab; goes away when you come back |

When the tab isn't in front and a reply finishes (or a permission question comes up), nanocode rings the terminal bell, so most terminals mark the tab or bounce the Dock icon, and shows a desktop notification in iTerm2, kitty, Ghostty, WezTerm and foot. If your terminal doesn't report whether the tab is in front, only replies that took 15 seconds or more notify you.

- `"notify": "off"` turns notifications off; `"always"` notifies even when the tab is in front.
- `"terminal_title": false` leaves the tab title alone.
- VS Code and Cursor show the process name by default: set `"terminal.integrated.tabs.title": "${sequence}"` to see the chat title.
- In tmux, the bell marks the window; for the title set `set -g set-titles on`, and for desktop notifications `set -g allow-passthrough on`.

## Images, files and pastes

Like Claude Code, attachments show up in the input as chips:

- **Images**: drag them onto the terminal, or paste a screenshot with **Ctrl+V** (**Alt+V** on Windows). Each becomes `[Image #1]`, `[Image #2]`, ... and is sent with the message. The chip stays in your text, so you can write "what's different between [Image #1] and [Image #2]?".
- **Other files**: dropped files become `@path` mentions, and their content is attached when you send.
- **Long pastes**: more than 800 characters or 3 lines collapse to `[Pasted text #1 +120 lines]`. Paste the same text again to see it in full and edit it. The full text is sent, and the chat shows it.
- **Backspace** right after a chip removes the whole chip.

On macOS, Ctrl+V takes a screenshot (Cmd+Ctrl+Shift+4) or an image file copied in Finder. On Linux it needs `wl-paste` (Wayland) or `xclip`. PNG, JPEG, WebP and GIF, up to 20 MB.

The model has to see images: Claude, a vision model on OpenRouter, or one in Ollama (`qwen2.5vl`, `llava`, `gemma3`). Images are kept in `.nanocode/images/` so a resumed chat still has them.

## Theme

The default theme is OpenCode's dark one (its background, panels and text) with nanocode's blue and violet accents. `nanocode` is the older, grayer theme. `/theme` lists every color with a swatch, and you can change any of them:

```
/theme nanocode              pick a base theme
/theme accent #ff79c6        change one color (bar by your messages, selection, Ask)
/theme panel default         back to the theme's color
/theme reset                 opencode with no custom colors
```

Colors: `bg`, `panel` (your messages and the input), `panel2` (menus, dialogs), `border`, `text`, `muted`, `faint`, `accent`, `accent2`, `pink`, `cyan`, `success`, `error`, `warning`, `think`. They're saved in the config as `"theme"` and `"colors"`. A change shows right away; lines already in the chat keep their colors.

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
| Ctrl+V (Alt+V on Windows) | Paste an image from the clipboard as `[Image #N]` |

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

Subcommands: `setup`, `config`, `doctor`, `email`, `serve` ([HTTP API](api.md)), `update`, `version`, `uninstall [--purge]`.
