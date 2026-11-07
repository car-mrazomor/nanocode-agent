# nanocode-agent

A small, hackable coding agent for your terminal. One Python file. It runs on your **Claude Max subscription** (through the Claude Agent SDK) or on **any model from OpenRouter**: Claude, GLM, GPT, Gemini, DeepSeek, Kimi, or OpenRouter's auto router.

> Inspired by [1rgs/nanocode](https://github.com/1rgs/nanocode), a minimal Claude Code alternative in ~250 lines. This project started as a fork of it and grew from there.

```
 ╭────────────────────────────────────────────╮
 │  █▀▀▄ ▄▀▀▄ █▀▀▄ ▄▀▀▄ ▄▀▀▀ ▄▀▀▄ █▀▀▄ █▀▀▀   │
 │  █  █ █▀▀█ █  █ █  █ █    █  █ █  █ █▀▀    │
 │  ▀  ▀ ▀  ▀ ▀  ▀  ▀▀   ▀▀▀  ▀▀  ▀▀▀  ▀▀▀▀   │
 │                                            │
 │  Welcome back, Ivan!                       │
 │                                            │
 │  model  openrouter/auto-code · OpenRouter  │
 │  cwd    ~/my-project                       │
 │  memory 2 files · 3 skills                 │
 ╰────────────────────────────────────────────╯
```

## Features

- **Two backends:** your Claude Max subscription through the Claude Agent SDK, or the OpenRouter API with any of its models
- **Full-screen terminal UI:** output on top, input pinned to the bottom, streamed markdown replies
- **Tools:** `read`, `write`, `edit`, `glob`, `grep`, `bash` (with live output), `remember`, `skill`
- **Permission prompts** before `bash`, `write` and `edit`: yes / no / always
- **Memory:** global and per-project notes loaded into every chat. Also reads `CLAUDE.md` and `AGENTS.md`
- **Skills:** `SKILL.md` folders the model loads when a task needs them. Your existing Claude Code skills work too
- **Saved chats:** every conversation is saved, and `/resume` brings one back
- **Model switching mid-chat:** `/model`, with Tab completion and prices
- **Quality of life:** `/` command menu, `@file` mentions, input history, multi-line input, Esc to stop, a token and cost line after every reply

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master/install.sh | sh
```

or from a clone:

```sh
git clone https://github.com/car-mrazomor/nanocode-agent.git
cd nanocode-agent && sh install.sh
```

The installer:
- checks for Python 3.10+
- creates a private venv in `~/.local/share/nanocode`
- puts a `nanocode` command on your PATH
- creates your global memory file, asking your name for the greeting

It works on macOS, Linux and WSL. Android (Termux), iSH and Git Bash may work but are untested. Run `sh install.sh` again to update, or `sh install.sh --uninstall` to remove it.

## Usage

```sh
cd your-project
nanocode
```

At startup you pick a backend:

1. **Claude Code SDK** uses your Claude Max or Pro subscription. Log in once with [Claude Code](https://claude.com/claude-code) (`claude`, then `/login`).
2. **OpenRouter API** needs `export OPENROUTER_API_KEY=sk-or-...`. The default model is `openrouter/auto-code`. Override it with `MODEL=z-ai/glm-5.3 nanocode`.

### Commands

| Command | What it does |
|---|---|
| `/help` | Commands and keys |
| `/model [name]` | List models with prices, or switch (Tab completes) |
| `/resume [n]` | List saved chats, or continue one |
| `/memory` | Show what's in memory |
| `/skills` | List skills |
| `/<skill> [task]` | Run a skill, e.g. `/review app.py` |
| `/cost` | Tokens and cost for this session |
| `/c` | New chat (the old one stays saved) |
| `/q` | Quit |
| `# note` | Save a note to project memory |
| `@path` | Attach a file to your message |

### Keys

| Key | Action |
|---|---|
| Enter / Alt+Enter | Send / new line |
| Esc | Stop the current reply |
| ↑ / ↓ | Input history |
| PageUp / PageDown | Scroll output |
| Tab | Complete commands, models, files |
| Ctrl+C | Quit |

## Memory

Memory is plain markdown, loaded into every chat:

| File | Scope | In git? |
|---|---|---|
| `~/.config/nanocode/NANOCODE.md` | You, in every project: name, preferences | not in any repo |
| `.nanocode/memory.md` | This project only: `# note` and the `remember` tool write here | ignored automatically |
| `NANOCODE.md`, `CLAUDE.md`, `AGENTS.md` | Team instructions in the project root | your choice (read-only for nanocode) |

nanocode creates `.nanocode/.gitignore` in each project, so memory and saved chats never get pushed.

## Skills

A skill is a folder with a `SKILL.md`:

```markdown
---
name: review
description: Review code for bugs and style before committing
---
1. Read the diff
2. List bugs first, then style issues
```

nanocode looks for skills in `.nanocode/skills/`, `.claude/skills/`, `~/.config/nanocode/skills/` and `~/.claude/skills/`. The model only sees each skill's name and description, and loads the full text with the `skill` tool when a task matches. You can also run one yourself with `/review`.

## Models (OpenRouter)

`/model` lists a curated set with prices: `openrouter/auto-code`, `openrouter/auto`, GLM 5.3 (`z-ai/glm-5.3`, `-flash`, `-prime`), Claude Opus/Sonnet/Haiku 5.5, GPT-5.6, Gemini, DeepSeek V4, Kimi, Grok and MiniMax. Any slug from [openrouter.ai/models](https://openrouter.ai/models) works.

## Files

```
nanocode.py     the whole agent
install.sh      installer: venv + `nanocode` command + global memory
.nanocode/      per project: memory.md, sessions/ (both git-ignored), skills/
```

## Credits

Based on [nanocode](https://github.com/1rgs/nanocode) by [@1rgs](https://github.com/1rgs).
