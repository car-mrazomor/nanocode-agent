# Custom agents

A custom agent is a sub-agent with its own instructions, and optionally its own model and tools. Like OpenCode's agents, each one is a markdown file:

| Where | For |
|---|---|
| `.nanocode/agents/<name>.md` | This project (can be committed). Wins over a global one with the same name |
| `~/.nanocode/agents/<name>.md` | Every project |

```markdown
---
description: Reviews a change for bugs and missing tests
model: anthropic/claude-sonnet-5.5
tools: read, grep, glob, bash
---
You are a strict code reviewer. Read the changed files, look for bugs, risky edge
cases and missing tests. Report each finding with its file and line.
```

- `description` tells the model (and you) when to use it.
- `model` is optional: the agent runs on that model instead of the chat's.
- `tools` is optional: only these tools (names, or patterns like `mcp__github__*`). Without it the agent gets the usual sub-agent tools: read, search and the web. Tools that change things still ask for permission. A custom agent can't start other agents.
- The rest of the file is the agent's prompt.

## Using them

- **The model** sees the list of your agents and hands them matching tasks with the `run_agent` tool. It gets back only the agent's report, so the main chat stays small.
- **You** can run one directly: type `@reviewer check the last commit`. The agent's report is the reply. (`@file` still attaches a file when there's no agent with that name.)
- `/agents` lists them; `/agents new <name>` creates `.nanocode/agents/<name>.md` from a template (`--global` puts it in `~/.nanocode/agents`).

A [custom command](custom-commands.md) can run with an agent too.
