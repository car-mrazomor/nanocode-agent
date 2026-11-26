# Permission rules

Instead of answering the same question again and again, set rules for what's allowed, what asks and what's refused:

```json
{
  "permission": {
    "bash": {"git status*": "allow", "git diff*": "allow", "git push*": "ask", "rm -rf*": "deny", "*": "ask"},
    "edit": "allow",
    "write": {"*.md": "allow", "secrets/*": "deny"},
    "web_fetch": "allow"
  }
}
```

- A tool gets one action (`allow`, `ask` or `deny`), or patterns with an action each.
- Patterns are globs (`*` matches anything, including `/`), matched against the command for `bash` and `shell`, the path for `edit` and `write`, and the URL for `web_fetch`.
- When several patterns match, the longest one wins: `git push*` beats `git *`.
- `write` follows the `edit` rules unless it has its own. `"*"` as a tool name applies to every tool without a rule.
- `ask` asks even with `/auto` on. Without a matching rule, the usual setting decides.
- A command line with several commands (`a && b`, `a; b`, `a | b`) is allowed only if every command is. `git status && rm -rf ~` isn't let through by `git status*`.
- A command with redirects (`>`, `>>`, `<`, `&>`), command or process substitution (`$(…)`, backticks, `<(…)`, `>(…)`), variables (`$X`) or subshells is never allowed by a rule: it asks. Copying output (`2>&1`) and discarding it (`2>/dev/null`) are fine. `deny` and `ask` rules still apply to every part.
- Paths are normalized before matching (`src/../x` is `x`, symlinks are followed): relative to the project inside it, absolute outside it. A relative pattern (`src/*`, `*.md`) never allows a file outside the project; use an absolute one (`/Users/me/notes/*`) or `*`.

Destructive commands and sending an email always ask, whatever the rules say ([more](safety.md)).

## /permissions

`/permissions` lists your rules. To add or change one (saved to your config):

```
/permissions allow bash "npm test*"
/permissions deny edit "*.lock"
/permissions allow web_fetch
/permissions remove bash "npm test*"
```
