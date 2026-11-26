# Your chats: search, fork, export, share, stats

Every chat is saved in the project it was started in (`.nanocode/sessions/`). These commands work across them.

## Search: `/search`

```
/search flaky test retry       chats that mention these words, in every project
/search open 2                 resume result 2 (when it's in this project)
```

Results show the chat's title, project, date and a snippet. A chat from another project can't be opened here: nanocode tells you the folder, and you `/resume <id>` there.

The model can search too, with the read-only `session_search` tool: ask "what did we decide about the cache last week?" and it looks it up. It can also read a whole earlier chat by its id. It only looks in this project's chats; with `all_projects` it can look in every project's, but asks you first (the permission name is `session_search`). To let it without asking, set `"session_search_all_projects": true` in the config.

How it works: an SQLite index at `~/.nanocode/search.db` holds the titles, your messages, the replies and the tool calls (not tool output). It uses SQLite's full-text search (FTS5) when your Python has it, and a simple word match otherwise. It's updated after every turn and before each search, reading only the chats that changed. Chats from the Claude subscription have no saved messages (Claude Code keeps them), so their saved screen is indexed instead. Projects are added as you use nanocode in them; a deleted folder drops out of the index.

## Fork: `/fork`

`/fork` copies the current chat into a new tab, so you can try another direction and keep the original. The copy gets its own id and is titled "fork of …" (or `/fork <title>`).

With the Claude subscription the conversation itself lives in Claude Code and can't be copied: the new tab shows the same screen and starts a fresh conversation, and your first message there is sent with the earlier chat as context.

## Export and import

```
/export                        this chat as Markdown, in the current folder
/export json                   the chat's session file (everything, for /import)
/export md ~/notes/chat.md     choose the file
/import chat.json              add an exported chat to this project's chats
nanocode import chat.json      the same from the shell; a URL or a gist link works too
```

An imported chat shows up in `/resume`. It keeps its provider: a chat made with OpenRouter is listed when OpenRouter is in use.

## Share: `/share`

`/share` uploads the chat's Markdown transcript as a **secret GitHub gist** and copies the link. Anyone with the link can read it; it isn't listed or searchable.

- It needs the [GitHub CLI](https://cli.github.com), logged in (`gh auth login`).
- Before uploading, nanocode shows how much will be shared, the first lines and where the full text is, and asks.
- Things that look like secrets are removed first: API keys (OpenAI, Anthropic, OpenRouter, GitHub, Slack, AWS, Google, GitLab, npm, PyPI), bearer tokens, JWTs, private keys, `password=` and `token=` values, and every secret in your config.
- `/unshare <gist url>` deletes it.

The filter catches common patterns, not everything: read the transcript before sharing chats that touched private code.

## Usage: `/stats`

```
/stats            this project
/stats all        every project in the search index
```

Shows chats, messages, tool calls by tool, tokens in and out, cost, a breakdown by model, messages per day for the last 14 days as a small bar chart, and the files changed most (lines added and removed). The numbers come from the saved chats.
