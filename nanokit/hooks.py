"""Your own shell commands at points in a turn, like Claude Code's hooks. In ~/.nanocode/config.json:

    "hooks": {
      "before_tool": [{"match": "bash", "run": "./check-command.sh"}],
      "after_tool":  [{"match": "edit|write", "run": "ruff format \\"$NANOCODE_FILE\\""}],
      "turn_end":    [{"run": "afplay /System/Library/Sounds/Glass.aiff"}],
      "startup":     [{"run": "git fetch -q"}]
    }

Each command gets the details in environment variables (NANOCODE_TOOL, NANOCODE_ARGS…) and as JSON on stdin.
A before_tool command that exits non-zero stops the tool; what it printed is the reason the model sees. One that
can't start, times out or fails in any other way stops the tool too (it fails closed).
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import subprocess

import nanocode as nc
from nanokit import command, on

EVENTS = ("startup", "shutdown", "turn_start", "turn_end", "before_tool", "after_tool")
DEFAULT_TIMEOUT = 30
ENV_LIMIT = 8000  # Windows caps the whole environment at 32k characters


def hooks_for(event, tool=None):
    """The configured hooks for an event (and tool), each {"run", "match", "timeout"}."""
    config = nc.st.config.get("hooks")
    entries = config.get(event) if isinstance(config, dict) else None
    if isinstance(entries, (str, dict)):
        entries = [entries]
    found = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            entry = {"run": entry}
        if not isinstance(entry, dict) or not str(entry.get("run") or "").strip():
            continue
        if tool is not None and not matches(str(entry.get("match") or "*"), tool):
            continue
        found.append(entry)
    return found


def matches(pattern, tool):
    """A tool name pattern: "*" or empty for all, a regex like "edit|write", or a glob like "mcp__github__*"."""
    if pattern in ("", "*"):
        return True
    try:
        if re.fullmatch(pattern, tool):
            return True
    except re.error:
        pass
    return fnmatch.fnmatchcase(tool, pattern)


def clip(text, limit=ENV_LIMIT):
    text = str(text if text is not None else "")
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more characters]"


async def run_hook(entry, event, data):
    """Runs one hook command; returns (exit code or None on a timeout or failure to start, output)."""
    env = {**os.environ, "NANOCODE_EVENT": event, "NANOCODE_PROJECT": os.getcwd()}
    if "name" in data:
        env["NANOCODE_TOOL"] = data["name"]
        env["NANOCODE_ARGS"] = clip(json.dumps(data.get("args") or {}, default=str))
        path = (data.get("args") or {}).get("path")
        if path:
            env["NANOCODE_FILE"] = str(nc.resolve(path).absolute())
    if "result" in data:
        env["NANOCODE_RESULT"] = clip(data["result"])
        env["NANOCODE_ERROR"] = "1" if data.get("is_error") else "0"
    if "text" in data:
        env["NANOCODE_TEXT"] = clip(data["text"])
    if data.get("error"):
        env["NANOCODE_ERROR"] = clip(data["error"], 2000)
    stdin = json.dumps({"event": event, **data}, default=str).encode()
    timeout = entry.get("timeout") if isinstance(entry.get("timeout"), (int, float)) else DEFAULT_TIMEOUT
    extra = {"start_new_session": True} if os.name != "nt" else {}
    try:
        proc = await asyncio.create_subprocess_shell(str(entry["run"]), stdin=subprocess.PIPE,
                                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                     env=env, cwd=os.getcwd(), **extra)
    except OSError as e:
        return None, f"couldn't start: {e}"
    try:
        output, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        nc.kill_tree(proc)
        return None, f"timed out after {timeout:g}s"
    return proc.returncode, output.decode("utf-8", "replace").strip()


def note(event, entry, text):
    nc.out(f"  {nc.DIM}⎿  {event} hook `{nc.clip_cells(str(entry['run']), 50)}`: {text[:300]}{nc.RESET}")


async def run_all(event, tool=None, **data):
    """Runs an event's hooks one after another; returns [(entry, code, output)]."""
    results = []
    for entry in hooks_for(event, tool):
        code, output = await run_hook(entry, event, data)
        if code is None:
            note(event, entry, output)
        results.append((entry, code, output))
    return results


@on("before_tool")
async def before_tool(name, args):
    """A hook that exits non-zero stops the tool; so does one that times out, can't start or fails: a check
    that didn't run mustn't let the tool through."""
    try:
        results = await run_all("before_tool", name, name=name, args=args)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        nc.log_error(e)
        return {"deny": f"a before_tool hook failed ({type(e).__name__}: {e}), so {name} was stopped"}
    for entry, code, output in results:
        if code is None:
            note("before_tool", entry, f"stopped {name}")
            return {"deny": f"a before_tool hook didn't finish ({output}), so {name} was stopped"}
        if code:
            note("before_tool", entry, f"stopped {name}")
            return {"deny": f"a before_tool hook stopped this: {output or f'exit code {code}'}"}
    return None


@on("after_tool")
async def after_tool(name, args, result, is_error):
    notes = []
    for entry, code, output in await run_all("after_tool", name, name=name, args=args, result=result,
                                             is_error=is_error):
        if output and code is not None:
            notes.append(f"[after_tool hook{' failed' if code else ''}] {nc.cap(output, 4000)}")
    return "\n".join(notes) or None


@on("turn_start")
async def turn_start(text):
    await failures("turn_start", await run_all("turn_start", text=text))


@on("turn_end")
async def turn_end(text, error):
    await failures("turn_end", await run_all("turn_end", text=text, error=error))


@on("startup")
async def startup():
    await failures("startup", await run_all("startup"))


@on("shutdown")
async def shutdown():
    await run_all("shutdown")


async def failures(event, results):
    for entry, code, output in results:
        if code:
            note(event, entry, f"exit code {code}" + (f": {output}" if output else ""))


@command("/hooks", "your shell hooks (\"hooks\" in the config): list them")
async def hooks_command(arg):
    lines = [f"{nc.BOLD}Hooks{nc.RESET} {nc.DIM}(\"hooks\" in {nc.tilde(nc.config_file())}){nc.RESET}"]
    count = 0
    for event in EVENTS:
        for entry in hooks_for(event):
            count += 1
            match = f" {nc.DIM}match {entry.get('match')}{nc.RESET}" if entry.get("match") else ""
            lines.append(f"  {nc.CYAN}{event:<12}{nc.RESET} {entry['run']}{match}")
    if not count:
        lines.append(f"  {nc.DIM}none. Example: \"hooks\": {{\"after_tool\": [{{\"match\": \"edit|write\", "
                     f"\"run\": \"ruff format $NANOCODE_FILE\"}}]}}{nc.RESET}")
    nc.out("\n".join(lines) + "\n")
