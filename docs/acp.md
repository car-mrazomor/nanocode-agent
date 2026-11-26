# Editors (Agent Client Protocol)

`nanocode acp` runs nanocode as an [Agent Client Protocol](https://agentclientprotocol.com) agent: editors that speak ACP, like [Zed](https://zed.dev), can use it in their agent panel.

The editor starts `nanocode acp` and talks to it over stdin and stdout. nanocode uses your usual provider, model, memory and skills, and works in the project folder the editor opens. It works in one folder at a time: while a session is answering, a prompt (or a new session) for a different folder gets an error asking to try again when that turn ends.

## Zed

Add nanocode to `agent_servers` in Zed's `settings.json` (Zed → Settings → Open Settings):

```json
{
  "agent_servers": {
    "nanocode": {
      "type": "custom",
      "command": "nanocode",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

If `nanocode` isn't on the PATH Zed sees, use the full path (`~/.nanocode/bin/nanocode`; on Windows `%USERPROFILE%\.nanocode\bin\nanocode.cmd`). (Older Zed versions don't know `"type"`: leave it out there.) Then open the agent panel, start a new thread and pick nanocode.

Options go in `args`: `["acp", "--model", "anthropic/claude-sonnet-5"]`, `["acp", "--provider", "ollama"]`, or `["acp", "--yes"]` to allow edits and commands without asking.

## What you get

- **Streaming replies**, tool calls with their results (files read and edited are shown as locations), and the plan from the `todo` tool.
- **Permissions in the editor.** When nanocode would ask before an edit or a command, the editor asks you: allow once, always allow that tool (for this run), or deny. Commands that look destructive (`rm -rf /`, `git push --force`, …) and sending email are always denied here, as in `nanocode serve`. With `"permissions": "auto"` in the config (or `--yes`) nothing asks.
- **Files from the editor.** Files you mention or attach become `@` mentions, so nanocode reads them; embedded file contents are passed as they are. Images work with models that see images.
- **Saved chats.** Each thread is a normal nanocode chat in the project's `.nanocode/sessions/`. Editors that load threads get the history back (`session/load`), and `/resume` in the terminal app opens them too.
- **Cancel** stops the turn.

Each thread runs on its own; several can run at once. Everything nanocode prints for you goes to stderr (Zed shows it in its logs), since stdout carries the protocol.

## Protocol details

JSON-RPC 2.0, one message per line. Methods: `initialize`, `authenticate` (nothing to do: nanocode uses your own setup), `session/new`, `session/load`, `session/prompt`, `session/cancel`. Notifications sent: `session/update` with `agent_message_chunk`, `user_message_chunk` (when loading), `tool_call`, `tool_call_update` and `plan`. Requests sent: `session/request_permission`.
