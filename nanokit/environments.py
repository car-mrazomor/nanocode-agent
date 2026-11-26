"""Where shell commands run: here (local), in a Docker or Podman container, on another machine over SSH, in a
Singularity/Apptainer image, or through any wrapper command (Modal, Daytona…). Config "environment":

    {"type": "local"}                                     the default
    {"type": "docker", "image": "python:3.12"}            also "engine": "podman", "container": "name", "mount"
    {"type": "ssh", "host": "user@box", "dir": "/srv/app", "options": ["-p", "2222"]}
    {"type": "apptainer", "image": "env.sif"}             or "singularity"
    {"type": "custom", "wrap": "daytona exec ws -- sh -c {command}"}

Only shell commands (bash, shell) move: the file tools keep working on the local files. See docs/environments.md.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import nanocode as nc
from nanokit import command, on

TYPES = ("local", "docker", "podman", "ssh", "singularity", "apptainer", "custom")

_override = None   # set by /env for this session, over the config
_started = {}      # container name -> (engine, created): the containers this nanocode started
_locks = {}        # container name -> (asyncio.Lock, its loop): one caller starts a container at a time
CMD_SPECIAL = re.compile(r'([\^&|<>()%!"])')


def config():
    """The environment in use, as a dict with at least "type"."""
    env = _override if _override is not None else nc.st.config.get("environment")
    if isinstance(env, str):
        env = {"type": env}
    if not isinstance(env, dict) or not env.get("type"):
        return {"type": "local"}
    env = dict(env)
    if env["type"] == "podman":                # shorthand for docker with the podman engine
        env["type"], env["engine"] = "docker", "podman"
    return env


def on_windows():
    return os.name == "nt"


def cmd_join(argv):
    """argv as a line for `cmd.exe /c`. Quoted for the program first (list2cmdline: the C runtime's rules), then
    every cmd.exe metacharacter, quotes included, gets a ^: cmd never sees a quoted section, an operator
    (& | < > ( )) or a %variable% (^% leaves %VAR^% undefined), and takes the carets out before the program
    gets the line. A line break can't be escaped for cmd, so it's refused (see sh_script)."""
    line = subprocess.list2cmdline([str(a) for a in argv])
    if "\n" in line or "\r" in line or "\0" in line:
        raise nc.ToolError("a line break can't be passed through cmd.exe here")
    return CMD_SPECIAL.sub(r"^\1", line)


def host_join(argv):
    """argv as one command line for the shell nanocode runs commands with (sh, or cmd.exe on Windows)."""
    return cmd_join(argv) if on_windows() else shlex.join(argv)


def host_quote(text):
    return cmd_join([text]) if on_windows() else shlex.quote(text)


def sh_script(command_text):
    """The command for the far side's `sh -c`. On Windows it goes through cmd.exe first, where a line break
    ends the command: a multi-line script travels base64-encoded instead (only letters, digits, + / =)."""
    if not on_windows() or not re.search(r"[\r\n\0]", command_text):
        return command_text
    encoded = base64.b64encode(command_text.encode("utf-8")).decode("ascii")
    return f'eval "$(echo {encoded} | base64 -d)"'


def container_name(env):
    return env.get("container") or f"nanocode-{os.getpid()}"


def mount_root():
    return Path(os.getcwd()).resolve()


def container_dir(path, root):
    """Where a local folder is inside the container: the same path (on Windows, under /workspace)."""
    if not on_windows():
        return str(path)
    try:
        rel = Path(path).resolve().relative_to(root)
    except ValueError:
        return "/workspace"
    return "/workspace" + ("/" + rel.as_posix() if rel.parts else "")


def container_lock(name):
    loop = asyncio.get_running_loop()
    lock, owner = _locks.get(name, (None, None))
    if owner is not loop:
        lock = asyncio.Lock()
        _locks[name] = (lock, loop)
    return lock


async def ensure_container(env):
    """Starts the long-lived container (or reuses one that's there) and returns its name. Commands running in
    parallel wait for the first one's start instead of starting (or naming) the same container twice."""
    name = container_name(env)
    if name in _started:
        return name
    async with container_lock(name):
        return await start_container(env, name)


async def start_container(env, name):
    engine = env.get("engine") or "docker"
    if name in _started:
        return name
    rc, output = await nc.run_cmd(engine, "inspect", "-f", "{{.State.Running}}", name, timeout=30)
    if rc == 0:
        if output.strip() != "true":
            rc, output = await nc.run_cmd(engine, "start", name, timeout=60)
            if rc != 0:
                raise nc.ToolError(f"{engine} couldn't start the container {name}: {output.strip()}")
            _started[name] = (engine, False)
        return name
    image = env.get("image")
    if not image:
        raise nc.ToolError(f'environment {engine} needs an "image", e.g. /env {engine} python:3.12')
    argv = [engine, "run", "-d", "--name", name, "--init"]
    if env.get("mount", True):
        root = mount_root()
        target = container_dir(root, root)
        argv += ["-v", f"{root}:{target}", "-w", target]
    argv += [str(a) for a in env.get("run_options") or []]
    argv += [image, "sleep", "infinity"]
    nc.out(f"  {nc.DIM}⎿  starting {engine} container {name} ({image})…{nc.RESET}")
    rc, output = await nc.run_cmd(*argv, timeout=600)   # may pull the image first
    if rc != 0:
        raise nc.ToolError(f"{engine} couldn't start {image}: {output.strip()[-500:]}")
    _started[name] = (engine, True)
    return name


async def wrap(command_text, cwd, env):
    kind = env["type"]
    command_text = sh_script(command_text)
    if kind == "docker":
        name = await ensure_container(env)
        engine = env.get("engine") or "docker"
        workdir = container_dir(cwd, mount_root()) if env.get("mount", True) else env.get("dir") or "/"
        return host_join([engine, "exec", "-e", "PAGER=cat", "-e", "GIT_PAGER=cat", "-w", workdir, name,
                          "sh", "-c", command_text])
    if kind == "ssh":
        host = env.get("host")
        if not host:
            raise nc.ToolError('environment ssh needs a "host", e.g. /env ssh user@box /path/to/project')
        remote = f"cd {shlex.quote(env['dir'])} && {command_text}" if env.get("dir") else command_text
        return host_join(["ssh", *[str(o) for o in env.get("options") or []], "-o", "BatchMode=yes", host, remote])
    if kind in ("singularity", "apptainer"):
        if not env.get("image"):
            raise nc.ToolError(f'environment {kind} needs an "image" (a .sif file or docker://…)')
        return host_join([kind, "exec", "--pwd", cwd, env["image"], "sh", "-c", command_text])
    if kind == "custom":
        template = env.get("wrap") or ""
        if "{command}" not in template:
            raise nc.ToolError('environment custom needs a "wrap" with {command} in it, '
                               'e.g. "daytona exec ws -- sh -c {command}"')
        return template.replace("{cwd}", host_quote(cwd)).replace("{command}", host_quote(command_text))
    raise nc.ToolError(f"unknown environment {kind!r} (one of: {', '.join(TYPES)})")


@on("wrap_command")
async def wrap_command(command, cwd):
    env = config()
    if env["type"] == "local":
        return None
    try:
        return await wrap(command, cwd, env)
    except nc.ToolError as e:
        # Never fall back to running it here: run a command that fails with the reason instead.
        return host_join([sys.executable, "-c", "import sys; sys.exit(sys.argv[1])", f"environment: {e}"])


async def stop_containers():
    """Stops the containers this nanocode started; removes the ones it created without a name you gave."""
    for name, (engine, created) in list(_started.items()):
        _started.pop(name, None)
        try:
            if created and name.startswith("nanocode-"):
                await nc.run_cmd(engine, "rm", "-f", name, timeout=30)
            else:
                await nc.run_cmd(engine, "stop", "-t", "2", name, timeout=30)   # yours: kept, for next time
        except Exception as e:
            nc.log_error(e)


@on("shutdown")
async def shutdown():
    await stop_containers()


def describe(env):
    kind = env["type"]
    if kind == "docker":
        engine = env.get("engine") or "docker"
        return f"{engine} container {container_name(env)}" + (f" ({env['image']})" if env.get("image") else "")
    if kind == "ssh":
        return f"ssh {env.get('host', '?')}" + (f":{env['dir']}" if env.get("dir") else "")
    if kind in ("singularity", "apptainer"):
        return f"{kind} {env.get('image', '?')}"
    if kind == "custom":
        return f"custom: {env.get('wrap', '')}"
    return "local"


@on("system_prompt")
def prompt_section():
    env = config()
    if env["type"] == "local":
        return None
    text = f"# Environment\nShell commands (bash, shell) run in: {describe(env)}."
    if env["type"] == "docker" and env.get("mount", True):
        text += " The project folder is mounted there, so file tools and commands see the same files."
    else:
        text += (" File tools (read, write, edit, glob, grep) still work on the LOCAL files, which commands "
                 "don't see unless they're synced; use bash to read and change files over there.")
    return text


def parse(arg):
    """/env arguments -> an environment dict."""
    words = shlex.split(arg, posix=os.name != "nt")
    kind = words[0].lower()
    rest = words[1:]
    if kind == "local":
        return {"type": "local"}
    if kind in ("docker", "podman"):
        if not rest:
            raise ValueError(f"usage: /env {kind} <image> [container name]")
        env = {"type": "docker", "engine": kind, "image": rest[0]}
        if len(rest) > 1:
            env["container"] = rest[1]
        return env
    if kind == "ssh":
        if not rest:
            raise ValueError("usage: /env ssh <user@host> [remote dir]")
        env = {"type": "ssh", "host": rest[0]}
        if len(rest) > 1:
            env["dir"] = rest[1]
        return env
    if kind in ("singularity", "apptainer"):
        if not rest:
            raise ValueError(f"usage: /env {kind} <image.sif>")
        return {"type": kind, "image": rest[0]}
    if kind == "custom":
        template = arg.strip()[len(words[0]):].strip()
        if "{command}" not in template:
            raise ValueError("usage: /env custom <wrapper with {command}>, e.g. /env custom daytona exec ws -- "
                             "sh -c {command}")
        return {"type": "custom", "wrap": template}
    raise ValueError(f"unknown environment {kind!r} (one of: {', '.join(TYPES)})")


@command("/env", "where commands run: local, docker <image>, ssh <host> [dir], apptainer <sif>, custom <wrap>")
async def cmd_env(arg):
    global _override
    arg = (arg or "").strip()
    if not arg:
        nc.out(f"  environment: {nc.BOLD}{describe(config())}{nc.RESET}")
        nc.out(f"  {nc.DIM}switch: /env local · /env docker python:3.12 · /env ssh user@host /dir · "
               f"/env apptainer img.sif · /env custom <wrap {{command}}>{nc.RESET}")
        return
    try:
        env = parse(arg)
    except ValueError as e:
        nc.out(f"  {nc.RED}{e}{nc.RESET}")
        return
    await stop_containers()
    _override = env
    nc.out(f"  commands now run in: {nc.BOLD}{describe(config())}{nc.RESET}")
    nc.out(f"  {nc.DIM}for this session; to keep it, set \"environment\" in the config{nc.RESET}")
