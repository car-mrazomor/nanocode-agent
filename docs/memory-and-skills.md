# Memory, skills and learning

## Memory

Memory is plain markdown, loaded into every chat:

| File | What | In git? |
|---|---|---|
| `~/.nanocode/NANOCODE.md` | You, everywhere: name, preferences, lessons | no |
| `.nanocode/memory.md` | This project. `# note` and the `remember` tool write here | ignored automatically |
| `NANOCODE.md`, `CLAUDE.md`, `AGENTS.md` | Team instructions in the project root | your choice; nanocode only reads them |

- `# use pnpm, not npm` in the prompt saves a note to project memory.
- The model calls `remember` when you correct it or state a preference. Lessons that apply everywhere go to the global file.
- `/memory` shows everything nanocode currently knows.

## Skills

A skill is a folder with a `SKILL.md`:

```markdown
---
name: release
description: Cut a release: bump version, changelog, tag
---
1. Update the version in pyproject.toml
2. ...
```

nanocode looks for skills in:

- `.nanocode/skills/` and `.claude/skills/` (this project)
- `~/.nanocode/skills/` and `~/.claude/skills/` (everywhere)

Skills made for Claude Code work as they are.

The model sees only each skill's name and description, and loads the full text with the `skill` tool when a task matches. You can run one directly: `/release` or `/release 1.2.0`. `/skills` lists them.

## Learning from experience

nanocode improves by writing down what it learns. It doesn't retrain the model.

- **Corrections and preferences** are saved to memory with `remember`.
- **Procedures.** After a multi-step task likely to come up again, it saves the steps as a skill with `save_skill`. Later runs load and refine it.
- **`/learn`** reviews the current chat (what went wrong, what took several attempts, what you corrected) and saves the lessons and skills.
- **`"auto_learn": true`** runs that review automatically after any turn where tools failed more than once.

Everything it learns is plain markdown you can read, edit or delete.
