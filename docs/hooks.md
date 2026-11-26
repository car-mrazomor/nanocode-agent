# Hooks

Hooks run your own shell commands at points in a turn, like Claude Code's hooks: format a file after every edit, block risky commands, play a sound when a reply is done. They're set in `~/.nanocode/config.json`:

```json
"hooks": {
  "before_tool": [{"match": "bash", "run": "python3 ~/.nanocode/check_command.py"}],
  "after_tool":  [{"match": "edit|write", "run": "ruff format \"$NANOCODE_FILE\""}],
  "turn_end":    [{"run": "afplay /System/Library/Sounds/Glass.aiff"}],
  "startup":     [{"run": "git fetch --quiet", "timeout": 60}]
}
```

Each hook has:

| Key | What it does |
|---|---|
| `run` | The shell command (sh on macOS and Linux, cmd on Windows), run in the project folder |
| `match` | Tool hooks only: which tools. A regex (`edit\|write`), a glob (`mcp__github__*`) or `*` (all, the default) |
| `timeout` | Seconds before it's stopped (default 30) |

## Events

| Event | When | What the hook can do |
|---|---|---|
| `before_tool` | Before a tool runs | Exit non-zero to **stop the tool**. What it printed is the reason the model sees. A hook that times out or can't start stops the tool too (fails closed) |
| `after_tool` | After a tool ran | What it prints is added to the tool's result, so the model sees it (lint errors, test output) |
| `turn_start` | A message is about to go to the model | Nothing; a failure is shown |
| `turn_end` | The reply finished | Nothing; a failure is shown |
| `startup` | nanocode opened | Nothing; a failure is shown |
| `shutdown` | nanocode is closing | Nothing |

## What a hook gets

Environment variables:

| Variable | What |
|---|---|
| `NANOCODE_EVENT` | The event name |
| `NANOCODE_PROJECT` | The project folder |
| `NANOCODE_TOOL` | The tool's name |
| `NANOCODE_ARGS` | The tool's arguments, as JSON |
| `NANOCODE_FILE` | The file a tool works on (edit, write, read…), as a full path |
| `NANOCODE_RESULT` | after_tool: the tool's result (cut at 8,000 characters) |
| `NANOCODE_ERROR` | after_tool: `1` if the tool failed, else `0`; turn_end: the error, if the turn failed |
| `NANOCODE_TEXT` | turn_start and turn_end: your message |

The same details come as one JSON object on standard input (with the full result), for scripts that would rather parse that.

`/hooks` lists your hooks. Hooks run on your machine with your permissions, without asking: only add commands you trust.
