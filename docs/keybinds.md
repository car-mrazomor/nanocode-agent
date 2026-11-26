# Key bindings and the editor

## Your own key bindings

Set `"keybinds"` in `~/.nanocode/config.json` to bind actions to keys. Your keys are added on top of the built-in ones, which keep working; if you pick a key that's already used, yours wins.

```json
"keybinds": {
  "new_tab": "ctrl+n",
  "palette": "ctrl+k",
  "editor": "ctrl+x ctrl+e",
  "models": "alt+m",
  "tabs": ["alt+t", "ctrl+x z"]
}
```

Keys are written like `ctrl+t`, `alt+m`, `shift+tab`, or in prompt_toolkit's notation (`c-t`, `escape m`). Several keys separated by spaces are a sequence (`ctrl+x w`: Ctrl+X, then W). A list binds several keys to one action. Changes apply when nanocode starts.

| Action | What it does |
|---|---|
| `palette` | The command palette (Ctrl+P) |
| `new_tab` | New tab (Ctrl+T) |
| `close_tab` | Close this tab (Ctrl+X W) |
| `next_tab`, `prev_tab` | Next / previous tab (Ctrl+X → / ←) |
| `tabs` | List the tabs |
| `sessions` | Saved chats (Ctrl+X L) |
| `models` | Switch model (Ctrl+X M) |
| `new_chat` | New chat in this tab (Ctrl+X N) |
| `compact` | Summarize the chat (Ctrl+X C) |
| `undo` | Undo the last file change (Ctrl+X U) |
| `help` | Commands and keys (Ctrl+X H) |
| `recap` | What happened this session |
| `cost` | Tokens and cost |
| `auto` | Auto-approve on or off |
| `editor` | Write the prompt in your editor (Ctrl+X E) |
| `quit` | Quit (Ctrl+X Q) |

`/keybinds` lists the actions, your keys, and any key or action it couldn't use.

## Writing the prompt in your editor

**Ctrl+X E** (or `/editor`) opens what you've typed in your editor. Save and quit, and the text comes back into the input box, ready to send. Handy for long prompts.

nanocode uses `$VISUAL`, then `$EDITOR` (e.g. `export EDITOR="code --wait"`), then `nano`, `vim` or `vi`; on Windows, Notepad.
