"""Background shells: commands that keep running (dev servers, watchers, long builds), in tmux, screen or nanocode itself."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import shlex
import signal
import subprocess
import time

import nanocode as nc

__all__ = [
    'SHELL_KEYS',
    'shell_backend',
    'run_cmd',
    'clean_terminal_text',
    'tail_file',
    'shell_start',
    'sweep_orphan_shells',
    'pid_alive',
    'shell_session',
    'shell_alive',
    'shell_output',
    'shell_stop',
    'stop_all_shells',
    'tool_shell',
]


# --- Background shells: commands that keep running (dev servers, watchers, long builds) ---
# Backends: tmux (you can watch: tmux attach -t nanocode-<name>), then screen, then nanocode itself
# (works everywhere, incl. Windows). Shells stop when nanocode exits.

SHELL_KEYS = {"enter": ("Enter", "\r"), "ctrl-c": ("C-c", "\x03"), "ctrl-d": ("C-d", "\x04"),
              "escape": ("Escape", "\x1b"), "none": (None, "")}


def shell_backend():
    choice = nc.st.config.get("shell_backend", "auto")
    if choice in ("tmux", "screen", "builtin"):
        return choice if choice == "builtin" or shutil.which(choice) else "builtin"
    if os.name != "nt":
        for name in ("tmux", "screen"):
            if shutil.which(name):
                return name
    return "builtin"


async def run_cmd(*cmd, timeout=15):
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        nc.kill_tree(proc)
        raise nc.ToolError(f"{cmd[0]} didn't respond within {timeout}s")
    return proc.returncode, stdout.decode(errors="replace")


def clean_terminal_text(text):
    lines = []
    for line in nc.CONTROL_RE.sub("", text.replace("\r\n", "\n")).split("\n"):
        lines.append(line.rsplit("\r", 1)[-1].rstrip())
    return "\n".join(lines).strip("\n")


def tail_file(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            data = f.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(clean_terminal_text(data).split("\n")[-n:])


async def shell_start(name, command):
    backend, cwd = shell_backend(), os.getcwd()
    nc.ensure_project_dir()
    log = nc.project_dir() / "shells" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    # The session name includes this nanocode's PID: two nanocodes (other projects or terminals) can both have
    # a shell called "dev" without stopping each other's.
    session = f"nanocode-{os.getpid()}-{name}"
    info = {"name": name, "command": command, "backend": backend, "log": str(log), "started": time.time(),
            "session": session}
    if backend == "tmux":
        await sweep_orphan_shells()
        await run_cmd("tmux", "kill-session", "-t", f"={session}")
        rc, output = await run_cmd("tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", cwd)
        if rc != 0:
            raise nc.ToolError(f"tmux couldn't start: {output.strip()}")
        await run_cmd("tmux", "send-keys", "-t", f"={session}:", "-l", command)
        await run_cmd("tmux", "send-keys", "-t", f"={session}:", "Enter")
        info["attach"] = f"tmux attach -t {session}"
    elif backend == "screen":
        # Logging set in a config file at startup and plain `sh` inside: works the same on screen 4.0 (macOS) to 5.x.
        await run_cmd("screen", "-S", session, "-X", "quit")
        rc_file = log.with_suffix(".screenrc")
        rc_file.write_text(f"logfile {log}\nlogfile flush 1\ndeflog on\n")
        rc, output = await run_cmd("screen", "-c", str(rc_file), "-dmS", session, "sh")
        if rc != 0:
            raise nc.ToolError(f"screen couldn't start: {output.strip()}")
        await asyncio.sleep(0.3)
        await run_cmd("screen", "-S", session, "-p", "0", "-X", "stuff", f"cd {shlex.quote(cwd)} && {command}\r")
        info["attach"] = f"screen -r {session}"
    else:
        extra = {"start_new_session": True} if os.name != "nt" else {}
        handle = open(log, "ab")
        info["proc"] = await asyncio.create_subprocess_shell(
            command, stdin=subprocess.PIPE, stdout=handle, stderr=subprocess.STDOUT, cwd=cwd,
            env={**os.environ, "PAGER": "cat", "GIT_PAGER": "cat"}, **extra)
        info["handle"] = handle
    nc.st.shells[name] = info
    return info


async def sweep_orphan_shells():
    """Stops tmux shells left by a nanocode that crashed or was killed (its PID is gone)."""
    try:
        rc, output = await run_cmd("tmux", "list-sessions", "-F", "#{session_name}", timeout=5)
    except nc.ToolError:
        return
    for session in output.split() if rc == 0 else []:
        found = re.match(r"nanocode-(\d+)-", session)
        if found and int(found.group(1)) != os.getpid() and not pid_alive(int(found.group(1))):
            try:
                await run_cmd("tmux", "kill-session", "-t", f"={session}", timeout=5)
            except nc.ToolError:
                pass


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def shell_session(info):
    return info.get("session") or f"nanocode-{info['name']}"


async def shell_alive(info):
    session = shell_session(info)
    if info["backend"] == "tmux":
        rc, output = await run_cmd("tmux", "list-panes", "-t", f"={session}:", "-F", "#{pane_current_command}")
        shell_names = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh"}
        return rc == 0 and output.strip().split("\n")[0] not in shell_names
    if info["backend"] == "screen":
        rc, output = await run_cmd("screen", "-ls")
        return re.search(rf"\.{re.escape(session)}\s", output) is not None
    return info["proc"].returncode is None


async def shell_output(info, lines):
    if info["backend"] == "tmux":
        rc, output = await run_cmd("tmux", "capture-pane", "-p", "-J", "-t", f"={shell_session(info)}:",
                                   "-S", f"-{max(lines, 50)}")
        if rc == 0:
            return "\n".join(clean_terminal_text(output).split("\n")[-lines:])
        return ""
    return tail_file(info["log"], lines)


async def shell_stop(info):
    session = shell_session(info)
    try:
        if info["backend"] == "tmux":
            await run_cmd("tmux", "kill-session", "-t", f"={session}")
        elif info["backend"] == "screen":
            await run_cmd("screen", "-S", session, "-X", "quit")
        else:
            await nc.reap(info["proc"])
    except nc.ToolError:
        pass
    if info.get("handle"):
        info["handle"].close()
    nc.st.shells.pop(info["name"], None)


async def stop_all_shells():
    for info in list(nc.st.shells.values()):
        await shell_stop(info)


@nc.tool("shell", "Background shells for commands that keep running: dev servers, watchers, long builds, REPLs. "
      "action=start (name, command), read (name, lines), send (name, text and/or key), stop (name), list. "
      "Use bash for commands that finish on their own.",
      {"action": {"type": "string", "enum": ["start", "read", "send", "stop", "list"]},
       "name": {"type": "string", "description": "short name, e.g. dev or tests"},
       "command": {"type": "string", "description": "for start"},
       "text": {"type": "string", "description": "for send: text to type"},
       "key": {"type": "string", "enum": list(SHELL_KEYS), "description": "for send: key after the text (default enter)"},
       "lines": {"type": "integer", "description": "for read: how many recent lines (default 60)"}},
      ["action"])
async def tool_shell(args):
    action = args["action"]
    if action == "list":
        if not nc.st.shells:
            return "no background shells running"
        rows = []
        for info in nc.st.shells.values():
            state = "running" if await shell_alive(info) else "finished"
            rows.append(f"{info['name']}: {state} · {info['command']} · started {nc.fmt_duration(time.time() - info['started'])} ago")
        return "\n".join(rows)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(args.get("name") or "")).strip("-")[:40]
    if not name:
        raise nc.ToolError("`name` is required (e.g. dev)")
    if action == "start":
        command = args.get("command")
        if not command:
            raise nc.ToolError("`command` is required to start a shell")
        if name in nc.st.shells:
            if await shell_alive(nc.st.shells[name]):
                raise nc.ToolError(f"shell '{name}' is already running; read it, stop it, or pick another name")
            await shell_stop(nc.st.shells[name])
        if not await nc.allowed("shell", command):
            return nc.DENIED
        info = await shell_start(name, await nc.wrapped_command(command))
        await asyncio.sleep(2)
        state = "running" if await shell_alive(info) else "already finished"
        output = await shell_output(info, 20)
        attach = f"\n(the user can watch it: {info['attach']})" if info.get("attach") else ""
        return f"started '{name}' ({info['backend']}), {state}. First output:\n{output or '(none yet)'}{attach}"
    info = nc.st.shells.get(name)
    if info is None:
        known = ", ".join(nc.st.shells) or "none"
        raise nc.ToolError(f"no shell named '{name}' (running: {known})")
    if action == "read":
        lines = min(max(int(args.get("lines") or 60), 1), 500)
        state = "running" if await shell_alive(info) else "finished"
        return f"[{name}: {state}]\n" + (await shell_output(info, lines) or "(no output yet)")
    if action == "send":
        text, key = args.get("text") or "", (args.get("key") or "enter").lower()
        if key not in SHELL_KEYS:
            raise nc.ToolError(f"unknown key '{key}' (use: {', '.join(SHELL_KEYS)})")
        if text and not await nc.allowed("shell", f"{name} ← {text}"):
            return nc.DENIED
        tmux_key, raw = SHELL_KEYS[key]
        session = shell_session(info)
        if info["backend"] == "tmux":
            if text:
                await run_cmd("tmux", "send-keys", "-t", f"={session}:", "-l", text)
            if tmux_key:
                await run_cmd("tmux", "send-keys", "-t", f"={session}:", tmux_key)
        elif info["backend"] == "screen":
            await run_cmd("screen", "-S", session, "-p", "0", "-X", "stuff", text + raw)
        else:
            proc = info["proc"]
            if key == "ctrl-c" and proc.returncode is None:
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGINT)
                    else:
                        proc.terminate()
                except (ProcessLookupError, OSError):
                    pass
            elif proc.returncode is None and proc.stdin:
                proc.stdin.write((text + ("\n" if key == "enter" else raw)).encode())
                await proc.stdin.drain()
        await asyncio.sleep(1)
        return f"sent. Recent output:\n{await shell_output(info, 15) or '(none)'}"
    if action == "stop":
        await shell_stop(info)
        return f"stopped '{name}'"
    raise nc.ToolError(f"unknown action '{action}'")
