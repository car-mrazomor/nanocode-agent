"""Custom slash commands: a markdown file per command, like Claude Code's and OpenCode's.

    .nanocode/commands/review.md   → /review   (this project; wins over a global one)
    ~/.nanocode/commands/review.md → /review   (every project)

    ---
    description: Review the current diff
    model: anthropic/claude-haiku-5.5   (optional)
    agent: reviewer                     (optional: run it with that custom agent)
    ---
    Review this diff for bugs, focusing on $ARGUMENTS:
    !`git diff`

$ARGUMENTS is everything after the command, $1 $2 … its words; !`cmd` runs a shell command (asking first) and
puts its output there (arguments used inside it are shell-quoted, and can't add commands of their own); @file
attaches a file as in any message.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import re
import shlex
import subprocess

import nanocode as nc
import nanokit
from nanokit import command, on

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
SHELL_RE = re.compile(r"!`([^`\n]+)`")
ARG_RE = re.compile(r"\$(ARGUMENTS|\d+)")
SHELL_TIMEOUT = 60
OURS = set()  # the "/names" registered from files, so a refresh can drop deleted ones


def command_dirs():
    """Earlier wins when two commands share a name."""
    return [nc.project_dir() / "commands", nc.home_dir() / "commands"]


def list_commands():
    """name (without /) -> {"description", "model", "agent", "body", "path"}."""
    found = {}
    for root in command_dirs():
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            name = path.stem.lower()
            if not NAME_RE.match(name) or name in found:
                continue
            try:
                meta, body = nc.parse_skill(path)
            except (OSError, UnicodeDecodeError):
                continue
            if not body:
                continue
            first = next((line.strip("# ").strip() for line in body.splitlines() if line.strip()), "")
            found[name] = {"description": meta.get("description") or first[:60], "model": meta.get("model") or None,
                           "agent": meta.get("agent") or None, "body": body, "path": path}
    return found


def refresh():
    """Registers every command file as a slash command (and drops the ones whose file is gone). A built-in or
    feature command with the same name wins."""
    found = list_commands()
    for name in list(OURS):
        if name[1:] not in found:
            OURS.discard(name)
            nanokit.COMMANDS.pop(name, None)
            nc.COMMANDS.pop(name, None)
    for name, info in found.items():
        slash = "/" + name
        if slash not in OURS and (slash in nanokit.COMMANDS or slash in nc.COMMANDS or slash in nc.ALIASES):
            continue
        OURS.add(slash)
        description = info["description"] or "custom command"
        nanokit.COMMANDS[slash] = {"fn": runner(name), "description": description, "anytime": False}
        nc.COMMANDS[slash] = description
    return found


def split_args(arg):
    """$1, $2…: words, with "quoted words" kept together, the same on every system. Backslashes stay as they
    are (no escapes), so a Windows path like C:\\work\\a.txt is one argument as typed."""
    lexer = shlex.shlex(arg, posix=True)
    lexer.whitespace_split, lexer.escape = True, ""
    try:
        return list(lexer)
    except ValueError:
        return arg.split()


def quote(value):
    """An argument as one shell word: nothing in it can end the word or run something."""
    if os.name == "nt":                                  # cmd.exe: inside "…", & | < > are plain text
        return '"' + value.replace('"', "").replace("%", "") + '"'
    return shlex.quote(value)


def fill_arguments(body, arg, quoted=False, append=True):
    """$ARGUMENTS and $1 $2 … filled in (each shell-quoted when `quoted`, for inside a !`cmd`)."""
    words = split_args(arg)

    def value(match):
        key = match.group(1)
        if key == "ARGUMENTS":
            text = arg
        else:
            index = int(key) - 1
            text = words[index] if 0 <= index < len(words) else ""
        return quote(text) if quoted else text

    if not ARG_RE.search(body):
        return body + (f"\n\nARGUMENTS: {arg}" if arg and append else "")
    return ARG_RE.sub(value, body)


async def run_shell(cmd):
    """One !`cmd` of a command file: its output, after asking (like the bash tool)."""
    if not await nc.allowed("bash", cmd):
        return f"(not run: `{cmd}` was denied)"
    extra = {"start_new_session": True} if os.name != "nt" else {}
    try:
        proc = await asyncio.create_subprocess_shell(await nc.wrapped_command(cmd), stdin=subprocess.DEVNULL,
                                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **extra)
    except OSError as e:
        return f"(`{cmd}` failed to start: {e})"
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), SHELL_TIMEOUT)
    except asyncio.TimeoutError:
        nc.kill_tree(proc)
        return f"(`{cmd}` timed out after {SHELL_TIMEOUT}s)"
    text = output.decode("utf-8", "replace").rstrip()
    if proc.returncode:
        text += f"\n(exit code {proc.returncode})"
    return nc.cap(text, 20_000)


async def expand(body, arg):
    """The prompt a command file makes: arguments filled in, then shell commands run (in order)."""
    # The shell commands are found in the file's own text, before the arguments go in: an argument can't add a
    # !`cmd` of its own, and inside a command each argument is quoted, so it can't run anything either.
    texts, commands, last = [], [], 0
    for match in SHELL_RE.finditer(body):
        texts.append(body[last:match.start()])
        commands.append(match.group(1))
        last = match.end()
    texts.append(body[last:])
    plain_text = "".join(texts)
    has_args = bool(ARG_RE.search(plain_text)) or any(ARG_RE.search(c) for c in commands)
    parts = []
    for i, text in enumerate(texts):
        parts.append(fill_arguments(text, arg, append=False) if has_args else text)
        if i < len(commands):
            parts.append(await run_shell(fill_arguments(commands[i], arg, quoted=True, append=False).strip()))
    result = "".join(parts)
    return result if has_args or not arg else result + f"\n\nARGUMENTS: {arg}"


def runner(name):
    async def run(arg):
        info = list_commands().get(name)
        if info is None:
            refresh()
            nc.out(f"{nc.RED}⏺ /{name} is gone (its file was removed){nc.RESET}\n")
            return
        display = f"/{name}" + (f" {arg}" if arg else "")
        prompt = await expand(info["body"], arg)
        if info["agent"]:
            if "agents" not in nanokit.LOADED:
                nc.out(f"{nc.RED}⏺ /{name} uses agent {info['agent']}, but custom agents aren't loaded{nc.RESET}\n")
                return
            agents = importlib.import_module("nanokit.agents")
            if info["agent"].lower() not in agents.list_agents():
                nc.out(f"{nc.RED}⏺ /{name}: no agent named {info['agent']} (/agents lists them){nc.RESET}\n")
                return
            await agents.run_visible(info["agent"].lower(), prompt, display)
            return
        images = []
        token = nc.CALL_MODEL.set(info["model"]) if info["model"] else None
        try:
            await nc.send(nc.expand_mentions(nc.expand_chips(prompt, images), images), display=display,
                          images=images)
        finally:
            if token is not None:
                nc.CALL_MODEL.reset(token)
        nc.out()
    return run


@on("startup")
def load_at_startup():
    refresh()


@on("message")
def refresh_on_slash(text):
    """A command file added while nanocode runs (or a headless `nanocode -p "/review"`) works right away."""
    if text.startswith("/") and text.split(" ", 1)[0] not in nanokit.COMMANDS:
        refresh()
    return None


@command("/commands", "custom commands from .nanocode/commands/*.md: list (and reload) them")
async def commands_command(arg):
    found = refresh()
    lines = [f"{nc.BOLD}Custom commands{nc.RESET} {nc.DIM}(markdown files in .nanocode/commands and "
             f"~/.nanocode/commands){nc.RESET}"]
    if not found:
        lines.append(f"  {nc.DIM}none yet: write .nanocode/commands/<name>.md, then type /<name>{nc.RESET}")
    for name, info in found.items():
        note = "" if "/" + name in OURS else f" {nc.YELLOW}(hidden by a built-in command){nc.RESET}"
        extra = " · ".join(x for x in (info["agent"] and "@" + info["agent"], info["model"]) if x)
        lines.append(f"  {nc.CYAN}/{name:<16}{nc.RESET} {info['description'][:60]}{note}  "
                     f"{nc.DIM}{extra + ' · ' if extra else ''}{nc.tilde(info['path'])}{nc.RESET}")
    nc.out("\n".join(lines) + "\n")
