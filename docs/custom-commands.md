# Custom commands

Turn a prompt you type often into a slash command: a markdown file per command, like in Claude Code and OpenCode.

| Where | For |
|---|---|
| `.nanocode/commands/<name>.md` | This project (can be committed). Wins over a global one |
| `~/.nanocode/commands/<name>.md` | Every project |

`.nanocode/commands/review.md` becomes `/review`:

```markdown
---
description: Review the current diff
model: anthropic/claude-haiku-5.5
---
Review this diff for bugs. Focus on $ARGUMENTS.

!`git diff`
```

In the file:

- `$ARGUMENTS` is everything typed after the command, and `$1`, `$2`, … are its words (quotes group words: `/fix "the login page" fast`). If the file uses neither, what you typed is added at the end.
- `` !`command` `` runs a shell command and puts its output there. It asks first, like the `bash` tool. Arguments used inside it (`$ARGUMENTS`, `$1`…) are quoted, so what you type after the command is always one plain argument, never shell code.
- `@path` attaches a file, as in any message (`@$1` works too).

Frontmatter (all optional):

| Key | What it does |
|---|---|
| `description` | Shown in `/help`, completion and `/commands` (default: the first line) |
| `model` | Run this command's turn on another model (OpenRouter and Ollama) |
| `agent` | Hand the prompt to a [custom agent](custom-agents.md) instead of the main chat |

New files work right away, no restart needed. `/commands` lists them (and reloads them). A built-in command with the same name wins over a file.
