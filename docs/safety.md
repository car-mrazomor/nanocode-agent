# Safety

## Permissions

nanocode asks before every file edit and command, and shows a diff for edits. Answer:

- `y`: allow this once
- `a`: always allow this tool for the rest of the session
- `n`: deny it; the model is told and tries something else

Dangerous commands only offer `y` and `n`.

To stop asking, pick "just do it" in setup (`"permissions": "auto"`), toggle `/auto`, or pass `--yes`.

## What always asks

Even in Auto mode:

- **Destructive commands:** `rm -rf /` (or `~`, `$HOME`, `*`), `git push --force`, `git reset --hard`, `mkfs`, `dd if=`, writing to `/dev/sd*`, `chmod -R 777 /`, `shutdown`, `reboot` and fork bombs.
- **Sending an email.** "Always" can't be chosen for it.

## What asks by default

- Going online: `web_search` and `web_fetch`.
- MCP tools their server doesn't mark read-only.
- REA actions that change something.
- Email drafts, tags and moves.

## Undo

`/undo` reverts file changes one at a time, newest first. Backups are kept in `.nanocode/undo/`.

## Privacy

- Chats, project memory, undo backups and shell logs live in `.nanocode/`. nanocode gives that folder its own `.gitignore`, so they never get committed in any repo (project skills in `.nanocode/skills/` can be).
- `~/.nanocode/config.json` holds your API key and email password with user-only permissions (`0600`).
- `/compare` sends only your prompt, never the chat ([more](compare.md)).
- Email content is treated as information, never as instructions ([more](email.md)).
