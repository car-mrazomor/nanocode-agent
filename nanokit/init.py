"""/init: the model explores the project and writes (or refreshes) AGENTS.md, like OpenCode's /init.

AGENTS.md is the shared instructions file many coding agents read; nanocode puts it in every prompt, so what's
written here (how to build and test, the code style, the layout, the traps) saves rediscovering it each chat.
"""
from __future__ import annotations

from pathlib import Path

import nanocode as nc
from nanokit import command

INIT_PROMPT = """\
Explore this repository and {action} AGENTS.md in the project root: a short guide for coding agents (and new
contributors) working here. Look at the README, the build and package files (package.json, pyproject.toml,
Makefile, Cargo.toml, go.mod, ...), CI workflows, the folder layout, a few representative source and test files,
and any existing agent rules (CLAUDE.md, .cursorrules, .cursor/rules/, .github/copilot-instructions.md).

Write, in this order and only what's true for this project:
1. Commands: install, build, lint/format, run the tests, and run a single test (exact commands).
2. Code style: formatting, naming, imports, error handling, typing, conventions the code actually follows.
3. Structure: the main folders and files, and what lives where (a few lines, not a file listing).
4. Gotchas: anything non-obvious that would trip someone up (generated files, required env vars, slow tests,
   platform quirks, things not to touch).

Keep it concise (well under 150 lines), concrete and skimmable: short bullets, real paths and commands, no
generic advice that would apply to any project, nothing you didn't verify. {existing}When done, reply with a
two-line summary of what you wrote."""


@command("/init", "explore the project and write AGENTS.md (build and test commands, style, structure)")
async def init(arg):
    path = Path.cwd() / "AGENTS.md"
    if path.exists():
        action, existing = "improve the existing", ("AGENTS.md already exists: read it first, keep what's still "
                                                    "right and anything the user wrote by hand, fix what's wrong or "
                                                    "outdated, and add what's missing. ")
    else:
        action, existing = "create", ""
    prompt = INIT_PROMPT.format(action=action, existing=existing)
    if arg:
        prompt += f"\n\nAlso, from the user: {arg}"
    await nc.send(prompt, display="/init" + (f" {arg}" if arg else ""))
    nc.out()
