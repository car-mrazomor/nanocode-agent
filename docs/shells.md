# Background shells

Some commands keep running: dev servers, file watchers, long builds, REPLs. nanocode runs these in background shells instead of blocking on them.

With the `shell` tool, the model can:

| Action | What it does |
|---|---|
| `start` | Start a command in a named shell |
| `read` | Read the latest output |
| `send` | Type into it, or press Ctrl+C |
| `stop` | Stop it |
| `list` | List running shells |

You can see and stop them with `/shells` and `/shells stop <name>`.

## Backends

| Backend | When | Watch it live |
|---|---|---|
| tmux | installed | `tmux attach -t nanocode-<name>` |
| screen | installed and no tmux | `screen -r nanocode-<name>` |
| builtin | otherwise; works everywhere, including Windows | `/shells` |

Pick one with `"shell_backend": "tmux"`, `"screen"`, `"builtin"` or `"auto"` (default).

Shells stop when nanocode exits. Their output is kept in `.nanocode/shells/`, which stays out of git.
