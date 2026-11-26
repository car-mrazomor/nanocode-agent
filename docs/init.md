# /init

`/init` asks the model to explore your project and write `AGENTS.md` in its root: how to install, build, lint and test (including a single test), the code style, the layout, and the gotchas.

nanocode reads `AGENTS.md` (and `CLAUDE.md`, `NANOCODE.md`) into every chat, so this saves the model from rediscovering your project each time. Many other coding agents read `AGENTS.md` too, so it's worth committing.

If `AGENTS.md` already exists, `/init` improves it: it keeps what's right and what you wrote yourself, fixes what's outdated and adds what's missing. Add a request after the command to steer it: `/init mention that the e2e tests need Docker`.
