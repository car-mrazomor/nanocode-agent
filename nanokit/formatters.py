"""Formatters: after the model writes or edits a file, run the project's own formatter on it.

Only when the project is set up for one (a [tool.ruff] or [tool.black] section, a prettier config, ...), so
nanocode never reformats a project that doesn't format its code. gofmt and rustfmt are the standard for their
languages, so they run whenever they're installed. Tools are looked up in the project first
(node_modules/.bin, .venv), then on PATH; nothing is downloaded. A formatter that is the project's own code (one
from its node_modules or .venv, or prettier, whose config and plugins can be JavaScript) runs only in a project
you trusted: asked once per project, remembered in "trusted_projects".

Config: "formatters": false turns this off; {".py": "black {file}"} sets the command for an extension
(false for one extension turns it off there).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import nanocode as nc
from nanokit import on

TIMEOUT = 20
DECLINED = set()   # projects whose own tools you said no to, this session
PRETTIER = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".scss", ".less", ".json", ".md", ".yaml",
            ".yml", ".html", ".vue"}
PRETTIER_CONFIGS = [".prettierrc", ".prettierrc.json", ".prettierrc.yaml", ".prettierrc.yml", ".prettierrc.json5",
                    ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.mjs", ".prettierrc.toml", "prettier.config.js",
                    "prettier.config.cjs", "prettier.config.mjs"]


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _folders(path):
    """The file's folder and its parents up to the project folder (where config files are looked for)."""
    root = Path(os.getcwd()).resolve()
    folder = Path(path).resolve().parent
    folders = [folder]
    while folder != root and root in folder.parents:
        folder = folder.parent
        folders.append(folder)
    if root not in folders:
        folders.append(root)
    return folders


def _find(name, folders, local=()):
    """A tool from the project (node_modules/.bin, a virtualenv) or from PATH."""
    for folder in folders:
        for sub in local:
            found = shutil.which(name, path=str(folder / sub))
            if found:
                return found
    return shutil.which(name)


def _python(folders):
    venvs = [".venv/bin", "venv/bin", ".venv/Scripts", "venv/Scripts"]
    text = "".join(_read(f / name) for f in folders for name in ("pyproject.toml", "setup.cfg", "tox.ini"))
    precommit = "".join(_read(f / ".pre-commit-config.yaml") for f in folders)
    ruff_file = any((f / name).exists() for f in folders for name in ("ruff.toml", ".ruff.toml"))
    if ruff_file or "[tool.ruff" in text or "ruff-format" in precommit:
        tool = _find("ruff", folders, venvs)
        if tool:
            return "ruff", [tool, "format", "--quiet"]
    if "[tool.black" in text or "black" in precommit or re.search(r"^\s*black\b", text, re.M):
        tool = _find("black", folders, venvs)
        if tool:
            return "black", [tool, "--quiet"]
    return None


def _prettier(folders):
    configured = any((f / name).exists() for f in folders for name in PRETTIER_CONFIGS)
    for folder in folders:
        try:
            package = json.loads(_read(folder / "package.json") or "{}")
        except ValueError:
            continue
        deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})} \
            if isinstance(package, dict) else {}
        configured = configured or "prettier" in package or "prettier" in deps
    if configured:
        tool = _find("prettier", folders, ["node_modules/.bin"])
        if tool:
            return "prettier", [tool, "--write"]
    return None


def _rustfmt(folders):
    tool = shutil.which("rustfmt")
    if not tool:
        return None
    edition = next((m.group(1) for f in folders for m in [re.search(r'^edition\s*=\s*"(\d+)"',
                                                                     _read(f / "Cargo.toml"), re.M)] if m), None)
    return "rustfmt", [tool] + (["--edition", edition] if edition else [])


def formatter_for(path):
    """(name, argv without the file) for this file, or None."""
    setting = nc.st.config.get("formatters", True)
    if setting is False:
        return None
    ext = Path(path).suffix.lower()
    if isinstance(setting, dict) and ext in setting:
        custom = setting[ext]
        if not custom:
            return None
        argv = custom if isinstance(custom, list) else shlex.split(str(custom), posix=os.name != "nt")
        return (Path(argv[0]).stem if argv else "?"), [str(a) for a in argv]
    folders = _folders(path)
    if ext in (".py", ".pyi"):
        return _python(folders)
    if ext in PRETTIER:
        return _prettier(folders)
    if ext == ".go" and shutil.which("gofmt"):
        return "gofmt", [shutil.which("gofmt"), "-w"]
    if ext == ".rs":
        return _rustfmt(folders)
    return None


def runs_project_code(tool, argv):
    """True for a formatter that runs code from the repository: a binary inside the project (node_modules/.bin,
    .venv), or prettier (prettier.config.js, .prettierrc.cjs and plugins are JavaScript it loads)."""
    if tool == "prettier":
        return True
    root = os.path.normcase(os.path.realpath(os.getcwd()))
    return bool(argv) and os.path.normcase(os.path.realpath(argv[0])).startswith(root.rstrip(os.sep) + os.sep)


def project_trusted():
    value = nc.st.config.get("trusted_projects")
    return isinstance(value, list) and os.path.realpath(os.getcwd()) in value


async def trust_project(why):
    """Asks once per project (even with /auto) whether its own tools may run; remembered in "trusted_projects"
    (shared with the language servers). Headless, only an already trusted project."""
    root = os.path.realpath(os.getcwd())
    if project_trusted():
        return True
    if root in DECLINED or nc.st.headless:
        return False
    if nc.st.permission_lock is None:
        nc.st.permission_lock = asyncio.Lock()
    async with nc.st.permission_lock:
        if project_trusted() or root in DECLINED:
            return project_trusted()
        if not await nc.ask_permission("project_tools", f"trust {nc.tilde(root)}? {why}", False):
            DECLINED.add(root)
            return False
        value = nc.st.config.get("trusted_projects")
        nc.st.config["trusted_projects"] = (list(value) if isinstance(value, list) else []) + [root]
        try:
            nc.save_config()
        except OSError as e:
            nc.log_error(e)
    return True


def _run(argv, path):
    """Runs the formatter; returns an error message, or None when it worked."""
    argv = [a.replace("{file}", path) for a in argv] if any("{file}" in a for a in argv) else argv + [path]
    if not shutil.which(argv[0]) and not os.path.isfile(argv[0]):
        return f"{argv[0]} isn't installed"
    try:
        done = subprocess.run(argv, capture_output=True, timeout=TIMEOUT, cwd=os.getcwd(), stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return f"took over {TIMEOUT}s"
    except OSError as e:
        return str(e)
    if done.returncode != 0:
        lines = (done.stderr or done.stdout).decode(errors="replace").strip().splitlines()
        return (lines[0][:200] if lines else f"exit code {done.returncode}")
    return None


@on("after_tool")
async def format_file(name, args, result, is_error):
    if name not in ("write", "edit") or is_error or not args.get("path"):
        return None
    path = os.path.abspath(nc.resolve(args["path"]))
    if not os.path.isfile(path):
        return None
    found = formatter_for(path)
    if not found:
        return None
    tool, argv = found
    setting = nc.st.config.get("formatters")
    custom = isinstance(setting, dict) and Path(path).suffix.lower() in setting     # your own choice: trusted
    if not custom and runs_project_code(tool, argv) and \
            not await trust_project(f"its formatter {tool} runs code from the project"):
        return None
    before = _read(path)
    error = await asyncio.to_thread(_run, argv, path)
    nc.remember_stamp(path)                    # its own formatted file isn't a "changed on disk" surprise
    if error:
        nc.out(f"     {nc.fg('faint')}{tool} failed: {error[:80]}{nc.RESET}")
        return f"(formatter {tool} failed on this file: {error})"
    if _read(path) == before:
        return None
    nc.out(f"     {nc.fg('faint')}formatted with {tool}{nc.RESET}")
    return f"(formatted with {tool}; read the file again before editing the lines it changed)"
