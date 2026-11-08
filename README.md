<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-wordmark-dark.svg">
    <img src="assets/logo-wordmark-light.svg" alt="nanocode" width="300">
  </picture>
</p>

# nanocode-agent

[![CI](https://github.com/car-mrazomor/nanocode-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/car-mrazomor/nanocode-agent/actions/workflows/ci.yml)

A small, hackable coding agent for your terminal, in one Python file you can read and edit end to end. **Website: [car-mrazomor.github.io/nanocode-agent](https://car-mrazomor.github.io/nanocode-agent/)**

It runs on:

- **your Claude subscription**, through the Claude Agent SDK
- **any OpenRouter model**: Claude, GLM, GPT, Gemini, DeepSeek, Kimi, or OpenRouter's auto router
- **local models through Ollama**: free, private and offline

With tools to read, edit and run code, plan and hand work to sub-agents, search the web through your own browser, use any MCP server, compare models side by side, and handle your email.

> Inspired by [1rgs/nanocode](https://github.com/1rgs/nanocode), a minimal Claude Code alternative in ~250 lines. This project started from it and grew into a full agent.

```
 █▀▀▄ ▄▀▀▄ █▀▀▄ ▄▀▀▄ ▄▀▀▀ ▄▀▀▄ █▀▀▄ █▀▀▀   nanocode v0.3.0
 █  █ █▀▀█ █  █ █  █ █    █  █ █  █ █▀▀    Opus 5.5 · Claude subscription
 ▀  ▀ ▀  ▀ ▀  ▀  ▀▀   ▀▀▀  ▀▀  ▀▀▀  ▀▀▀▀   ~/my-project

 Welcome back, Ivan · 2 memory files · 3 skills
 / commands · @ attach file · # remember · /resume · /help
```

## Install

macOS, Linux, WSL, Android (Termux):

```sh
curl -fsSL https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master/install.ps1 | iex
```

Everything goes into `~/.nanocode`: the app, a private Python environment, your config, memory and skills. Nothing else on your system is touched except one PATH line.

**Python:** if you already have Python 3.10 or newer, the installer uses it. If you don't, it downloads a private Python 3.12 into `~/.nanocode/python` (using [uv](https://github.com/astral-sh/uv)). That needs no admin rights and doesn't touch your system. On Android (Termux) it runs `pkg install python` instead.

```sh
nanocode update              # latest nanocode (and REA)
nanocode version             # your version, and whether there's a newer one
nanocode uninstall           # remove the app, keep config, memory and skills
nanocode uninstall --purge   # remove everything
```

**Updates:** nanocode checks GitHub for a newer version once a day, in the background. When there is one, the start screen says `/update to install v0.5.1` (like OpenCode). Click it or type `/update` to choose **Update** (installs in the background while you keep working) or **Skip** (no more reminders for that version). Once it's installed, `/exit` restarts nanocode on the new version. On Windows, Update closes nanocode first, installs, then you start it again. Outside the app, `nanocode update` does the same, and `/version` checks right away. To turn the check off, set `"update_check": false` in `~/.nanocode/config.json` (or `NANOCODE_NO_UPDATE_CHECK=1`).

## Start

```sh
cd your-project
nanocode
```

The first start runs a short setup:

1. **Pick a provider.**
   - **Claude subscription** uses your Max or Pro plan. Log in once with [Claude Code](https://claude.com/claude-code) (`claude`, then `/login`).
   - **OpenRouter** works with any cloud model and charges per token. Paste a key from [openrouter.ai/keys](https://openrouter.ai/keys). The default model is `openrouter/auto-code`.
   - **Ollama** runs local models. Install [Ollama](https://ollama.com/download) and pull a model with tool support, such as `ollama pull qwen3`.
2. **Pick a model.**
3. **Choose whether nanocode asks before it edits files or runs commands.**

Your choices are saved in `~/.nanocode/config.json`. To switch provider while nanocode is running, use `/provider ollama` (or `openrouter`, `claude`). To change keys and defaults, run `nanocode setup`. To check that everything works, run `nanocode doctor`.

Thinking is on by default (`medium`). It's sent the way each provider expects: `reasoning` for OpenRouter, `think` for Ollama, and adaptive thinking with an effort level for Claude. Models that can't think (or can't stop) are sent the request without it.

## Use

Type what you want done. nanocode reads, searches, edits, runs commands and tests, and shows diffs before it changes anything. After every response, a dim line shows how long it took, the tokens used and what it cost:

```
  ✻ Cooked for 11s · 12.3k in · 340 out · $0.0123 · done 19:06
```

The cost is OpenRouter's real price, or "free (local)" with Ollama. With your Claude subscription it's what the same usage would cost on the API, which your plan covers.

| Command | What it does |
|---|---|
| `/provider [name]` | List providers, or switch (Claude subscription, OpenRouter, Ollama) |
| `/think [level]` | How hard the model thinks: `off`, `low`, `medium` (default) or `high`. Shown under the prompt after the model, like OpenCode (`· medium`); click it to change |
| `/model [name]` | List models (with prices, or what's installed in Ollama), or switch. Tab completes |
| `/resume [n]` | Continue a saved chat. Every chat is saved per project |
| `/recap` | What happened this session (files changed, commands run, cost) and what's next |
| `/compact` | Summarize a long chat to free up context |
| `/undo` | Revert the last file change |
| `/learn` | Review this chat and save lessons and skills for next time |
| `/memory`, `/skills` | Show what nanocode knows |
| `/shells` | Background shells: list them, or `/shells stop <name>` |
| `/auto` | Toggle auto-approve for edits and commands |
| `/rea` | Reverse-engineering tools: status, `/rea on`, `/rea off` |
| `/mcp` | MCP servers: list, `add`, `remove`, `import` (from Claude Code / Claude Desktop), `tools`, `on`/`off` per server or tool |
| `/compare <prompt>` | Send one prompt to several models and see the answers side by side. `/compare models a, b, c` picks them |
| `/email [task]` | Summarize and triage your inbox, or do an email task (set up with `nanocode email`) |
| `/terminal-setup` | Make Shift+Enter, Cmd+Backspace and Cmd+arrows work in your terminal |
| `/cost`, `/config`, `/help` | Usage, settings, help |
| `/c`, `/q` | New chat, quit |
| `# note` | Save a note to project memory |
| `@path` | Attach a file to your message |
| `/<skill> [task]` | Run a skill, e.g. `/review app.py` |

**The interface:** nanocode opens on a start screen, with the logo and the prompt in the middle. Once you send something, it switches to the chat view, which uses the full width of the terminal:

- **Transcript:** your messages appear as blocks with an accent bar, tool calls as short lines (`→ Read`, `← Edit` with a diff, `$ Bash` with its output), and each reply ends with a `▣` line showing time, model, tokens and cost.
- **Prompt panel:** at the bottom, with the mode (Ask or Auto), the model, the provider and the thinking level. Below it are the folder and git branch, context used and cost, and while a reply runs, a spinner and `esc interrupt`. `/recap` lists the files changed this session and `/shells` the background shells.
- **Command palette:** `ctrl+p` opens a searchable list of every action and command.

**Keys:**

| Key | Action |
|---|---|
| Enter | Send |
| Shift+Enter, Alt+Enter, Ctrl+J | New line |
| Ctrl+P | Command palette |
| Ctrl+X then N / L / M / C / U / H / Q | New session / sessions / models / compact / undo / help / quit |
| Esc Esc | Stop the reply (press twice) |
| Ctrl+C | Stop the reply, clear the input, or quit |
| Ctrl+U (Cmd+Backspace on macOS) | Delete everything before the cursor |
| Ctrl+← →, Alt/Option+← → | Jump by word |
| Home / End, Ctrl+A / Ctrl+E | Start / end of line |
| ↑ / ↓ | History |
| PageUp / PageDown, mouse wheel | Scroll the chat |
| Tab | Complete |

While you scroll through a long answer, your message stays pinned at the top; click it to jump back to it.

Drag with the mouse to select text in the chat; it's copied when you let go (double-click copies a word). This works like Claude Code's fullscreen mode and OpenCode: the system clipboard is used locally, and OSC 52 over SSH. The wheel scrolls the chat wherever the pointer is. To use your terminal's own selection instead, hold Shift while dragging (Option in iTerm2 and VS Code on macOS, Fn in Terminal.app).

Most terminals send the same thing for Shift+Enter as for Enter, and many don't pass Cmd shortcuts to apps at all. Run **`/terminal-setup`** once to fix that:

- **kitty, Ghostty, VS Code, Cursor, Windsurf:** it adds the mappings to the terminal's config. It asks first and makes a backup.
- **iTerm2, WezTerm, Alacritty, Windows Terminal, Terminal.app:** it shows the steps.

Alt+Enter and Ctrl+J insert a new line in every terminal.

**In scripts:**

```sh
nanocode -p "explain this error" < build.log                  # one-shot: the answer goes to stdout
nanocode -p "fix the failing test" --yes                      # allow edits and commands
nanocode -c                                                   # continue the last chat in this folder
nanocode --provider ollama -m qwen3 -p "summarize README.md"  # one-off provider and model
```

## Agents and planning

nanocode chats over many turns and works through tasks on its own: it plans, calls tools, checks the results and keeps going until it's done.

- **Plan.** For tasks with several steps it keeps a checklist (`☐ ◐ ☑`) in the chat and ticks items off as it works.
- **Sub-agents.** It can hand a side task (exploring the codebase, reading docs) to a sub-agent with a fresh context, which works on its own and reports back only the result. Several run in parallel. They can read, search and use the web, but not edit files or run commands unless you set `"agent_edits": true` in the config.

## Web search

`web_search` and `web_fetch` work with every provider and model, because nanocode runs them itself:

- **Your real browser.** Pages are loaded by your installed Chrome, Chromium, Edge or Brave, headless and with a throwaway profile, so JavaScript-heavy pages work. Without a browser, a plain request is used.
- **Engines.** DuckDuckGo by default (fast), Bing, or Google (often blocks headless browsers, then DuckDuckGo is used). Pick one with `"search_engine"` in the config.
- **OpenRouter's own search.** Set `"search_engine": "openrouter"` to let OpenRouter's web plugin search (with your model, or `"search_model"`). It costs a little extra per search.
- **Asks first.** Going online asks for permission (answer `a` to allow it for the session).

## MCP

Connect any local MCP server and its tools become nanocode's tools, on every provider.

```sh
/mcp add github npx -y @modelcontextprotocol/server-github
/mcp import                 # copy the servers you use in Claude Code, Claude Desktop or .mcp.json
/mcp tools                  # every tool, on or off
/mcp off github.delete_repo # switch one tool off (or a whole server: /mcp off github)
```

Servers start with your first message. Small servers' tools are offered directly; servers with more than 20 tools are searched on demand, so the model's tool list stays short. Tools not marked read-only ask before running. `ctrl+p → MCP tools on/off` toggles tools from a list. Servers are stored under `"mcp_servers"` in the config (`command`, `args`, `env`); `${VAR}` in them is filled from your environment. Remote (HTTP) servers aren't supported yet.

## Compare

```
/compare models anthropic/claude-sonnet-5, z-ai/glm-5.3, ollama:qwen3:8b
/compare explain this regex: ^(?=.*\d).{8,}$
```

The prompt goes to every model at once and the answers appear side by side (one under the other in narrow terminals), each with its time, tokens and cost. Models can be OpenRouter models, `ollama:<model>` or `claude:<model>` (your subscription).

**Only what you send, only when you ask.** Compare is a command, not a tool: the model can never start it. It sends just the prompt you typed (and files you attach with `@`), with no chat history, memory or project details, and the answers don't become part of your chat.

## Email

```sh
nanocode email      # once, in a terminal: address, servers (filled in for Gmail, iCloud, Outlook, Yahoo, Fastmail), password
```

Then in nanocode:

- **`/email`** summarizes what's new: what needs a reply, what's FYI, what looks like spam, with suggested tags and moves.
- **`/email <task>`**, or just ask: "draft a reply to Ana saying Friday works", "move the newsletters to archive", "tag everything from my bank as finance".
- **Drafts match your style:** before writing, it reads a few of your sent emails for tone, greeting and sign-off.
- **It can't send by itself.** Drafts go to your Drafts folder. Sending only happens when you ask, and you confirm every send, even in auto mode.
- **Email is untrusted.** nanocode treats email content as information, never as instructions.

It uses IMAP and SMTP (no extra packages). Gmail and iCloud need an app password. The password is stored in `~/.nanocode/config.json`, readable only by you.

## Background shells

Some commands keep running: dev servers, file watchers, long builds, REPLs. nanocode runs these in background shells instead of blocking on them. It can start one, read its latest output, type into it (or press Ctrl+C), and stop it.

- **tmux** is used if it's installed. You can watch any shell live with `tmux attach -t nanocode-<name>`.
- **screen** is used if there's no tmux. Watch with `screen -r nanocode-<name>`.
- **Otherwise** nanocode runs them itself, which works everywhere, including Windows.

To choose one, set `"shell_backend": "tmux"`, `"screen"` or `"builtin"` in `~/.nanocode/config.json`. Shells stop when nanocode exits, and their output is kept in `.nanocode/shells/` (not committed).

## Memory

Memory is plain markdown, loaded into every chat:

| File | What | In git? |
|---|---|---|
| `~/.nanocode/NANOCODE.md` | You, everywhere: name, preferences, lessons | no |
| `.nanocode/memory.md` | This project: `# note` and the `remember` tool write here | ignored automatically |
| `NANOCODE.md`, `CLAUDE.md`, `AGENTS.md` | Team instructions in the project root | your choice (nanocode only reads them) |

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

The model sees only each skill's name and description, and loads the full text when a task matches. You can also run one directly with `/release`.

## Learning from experience

nanocode improves over time by writing down what it learns. It doesn't retrain the model:

- **Corrections and preferences.** When you correct it or state a preference, it saves the lesson to memory with `remember`.
- **Procedures.** After a multi-step task that's likely to come up again, it saves the procedure as a skill with `save_skill`. Later runs load that skill and refine it.
- **`/learn`.** This asks it to review the current chat: what went wrong, what took several attempts, what you corrected. It then saves the lessons and skills.
- **`auto_learn`.** Set `"auto_learn": true` in `~/.nanocode/config.json` to run that review automatically after any turn where tools failed more than once.

Everything it learns is plain markdown in your memory files and skill folders, so you can read, edit or delete it.

## Reverse engineering (REA)

nanocode comes with [REA](https://github.com/morluto/rea), an MCP server for reverse engineering. It covers native binaries, apps, Android APKs, Electron and JavaScript bundles, websites, firmware and devices. Ask things like *"what does this binary do?"* or *"which API endpoints does this Electron app call?"*.

- **Installed and updated automatically.** The installer installs the latest REA, and `nanocode update` updates it. REA needs Node.js 22+. If you don't have it, the installer downloads the official Node.js LTS into `~/.nanocode/node`, verified against its checksum. That needs no admin rights, and only REA uses it. Skip REA with `sh install.sh --no-rea` (Windows: `-NoRea`).
- **Costs nothing until it's needed.** REA has about 230 tools, too many to hand to a model at once. nanocode gives the model three small tools instead: `rea_search` finds a REA tool, `rea_schema` shows its arguments, and `rea_call` runs it. REA starts on first use. Its own `reverse-engineer-anything` skill teaches the model the workflows.
- **Fails safe.** If REA is missing, crashes or times out, the model gets an error message and nanocode keeps working. A crashed REA restarts on the next call.
- **Asks permission** before REA actions that change something, like installing an APK, running Frida or executing commands.
- **Status:** `/rea` shows it, and `/rea off` / `/rea on` turns it off or on.

Some REA features need programs you install yourself, because they're large, platform-specific or paid:

| For | Install |
|---|---|
| Native binaries (decompiling, functions) | [Ghidra](https://ghidra-sre.org) (free), or Hopper / IDA |
| Android APKs | [JADX](https://github.com/skylot/jadx) and a Java JDK |
| Android devices | adb ([platform-tools](https://developer.android.com/tools/releases/platform-tools)) |
| APK resources | [Apktool](https://apktool.org) |
| Websites | Chrome or Chromium |
| Firmware (Linux) | Binwalk or Unblob |
| Packet captures | TShark ([Wireshark](https://www.wireshark.org)) |

When a task needs one you don't have, nanocode tells you what to install and carries on with what works without it. Static analysis of .NET, PyInstaller, Electron and JavaScript apps, and Mach-O, ELF and PE files needs nothing extra.

## Ollama tips

- Use a model with tool support, otherwise it can't edit files or run commands. `/model` and `nanocode doctor` flag the ones that don't.
- The context size is picked from your RAM: 8k, 16k or 32k. A context too large for your machine spills onto the CPU and gets very slow. On an 8 GB Mac, one reply took 12 minutes at 32k and 10 seconds at 8k.

## Safety

- **Permissions.** nanocode asks before every file edit or command, unless you choose "just do it" in setup, use `/auto`, or pass `--yes`.
- **Destructive commands always ask,** even in auto mode: `rm -rf /`, `git push --force`, `git reset --hard`, `mkfs`, and similar. So does sending an email.
- **Going online asks:** web searches and page reads, and MCP tools that aren't read-only.
- **Undo.** `/undo` reverts file changes one at a time.
- **Private by default.** Chats and project memory stay out of git. Your API key is stored with user-only permissions.

## Development

```sh
pip install -r requirements.txt pytest
python -m pytest -q tests
python nanocode.py           # run from the repo without installing
```

## Files

```
nanocode.py      the whole agent
install.sh       installer for macOS / Linux / WSL / Termux
install.ps1      installer for Windows
tests/           pytest suite (tools, providers, sessions, config, CLI)
```

## Credits

Based on [nanocode](https://github.com/1rgs/nanocode) by [@1rgs](https://github.com/1rgs).
