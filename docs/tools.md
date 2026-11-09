# Tools

These are the tools the model can call. They work the same on every provider and model, because nanocode runs them itself.

## Files and code

| Tool | What it does | Asks? |
|---|---|---|
| `read` | Read a file with line numbers (`offset`, `limit` for big files) | no |
| `write` | Create or overwrite a file | yes, with a diff |
| `edit` | Replace exact text in a file | yes, with a diff |
| `glob` | Find files by pattern, newest first | no |
| `grep` | Search file contents by regex (uses ripgrep when installed) | no |
| `bash` | Run a command in the project folder | yes |
| `shell` | Background shells: `start`, `read`, `send`, `stop`, `list` ([more](shells.md)) | yes, to start |

`read`, `glob` and `grep` skip `.git`, `node_modules` and binary files.

## Planning and agents

| Tool | What it does |
|---|---|
| `todo` | The plan for a multi-step task, shown as a checklist ([more](agents.md)) |
| `agent` | Hand a subtask to a sub-agent with a fresh context |

## Memory and skills

| Tool | What it does |
|---|---|
| `remember` | Save a lasting fact or lesson to memory |
| `skill` | Load a skill's full instructions |
| `save_skill` | Save a reusable procedure as a skill |

See [Memory, skills and learning](memory-and-skills.md).

## Web

| Tool | What it does | Asks? |
|---|---|---|
| `web_search` | Search the web through your browser | yes (`a` allows for the session) |
| `web_fetch` | Read a page as plain text, JavaScript included | yes |

See [Web search](web.md).

## Email

Only available after `nanocode email`. See [Email](email.md).

| Tool | What it does | Asks? |
|---|---|---|
| `email_list` | Recent emails in a folder | no |
| `email_search` | Search by words, sender or subject | no |
| `email_read` | Read one email (doesn't mark it read) | no |
| `email_folders` | List folders | no |
| `email_draft` | Save a draft (nothing is sent) | yes |
| `email_send` | Send an email | **always**, even in Auto mode |
| `email_tag` | Mark read/unread, star, tag, move | yes |

## MCP and REA

| Tool | What it does |
|---|---|
| `<server>.<tool>` | Tools from your MCP servers, offered directly when a server has 20 tools or fewer |
| `mcp_search`, `mcp_schema`, `mcp_call` | Find, inspect and run tools of bigger MCP servers |
| `rea_search`, `rea_schema`, `rea_call` | Same for REA's ~230 reverse-engineering tools |

See [MCP](mcp.md) and [REA](rea.md).

## Read-only tools

Tools that don't change anything (`read`, `glob`, `grep`, `skill`, `todo`, `web_search`, `web_fetch`, the email read tools, the search and schema tools for MCP and REA, MCP tools their server marks read-only, and `agent` unless `agent_edits` is on) are marked read-only. On the Claude subscription this lets several of them run in parallel.
