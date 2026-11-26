"""Python plugins: each *.py in ~/.nanocode/plugins/ (yours, loaded always) and .nanocode/plugins/ (the
project's, loaded once you trust it) is imported when nanocode starts. A plugin uses the same API as these
feature modules:

    import nanocode as nc
    from nanokit import command, on

    @command("/hello", "say hello")
    async def hello(arg):
        nc.out("hello " + arg)

A project plugin is code from whoever wrote the repository, so nanocode asks before running it, and again
whenever the file changes (trusted files are remembered by their hash in "trusted_plugins").
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
import traceback

import nanocode as nc
import nanokit
from nanokit import cli, command, on

PLUGINS = {}  # path -> {"name", "scope", "status": loaded|failed|untrusted|declined, "error", "hash"}
STATE = {"loaded": False}


def plugin_dirs():
    return [("global", nc.home_dir() / "plugins"), ("project", nc.project_dir() / "plugins")]


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trusted():
    value = nc.st.config.get("trusted_plugins")
    return set(value) if isinstance(value, list) else set()


def found_plugins():
    """[(scope, path)] for every plugin file, global ones first."""
    found = []
    for scope, root in plugin_dirs():
        if root.is_dir():
            found += [(scope, path) for path in sorted(root.glob("*.py")) if not path.name.startswith("_")]
    return found


def load_plugin(scope, path):
    key = str(path)
    info = PLUGINS.setdefault(key, {"name": path.stem, "scope": scope, "status": None, "error": None})
    if info["status"] == "loaded":
        return True
    name = f"nanocode_plugin_{scope}_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(name, None)
        nc.log_error(e)
        last = traceback.extract_tb(e.__traceback__)[-1:]
        where = f" (line {last[0].lineno})" if last and last[0].filename == str(path) else ""
        info.update(status="failed", error=f"{type(e).__name__}: {e}{where}")
        return False
    info.update(status="loaded", error=None)
    for slash, entry in nanokit.COMMANDS.items():   # its commands show in /help and complete with Tab
        nc.COMMANDS.setdefault(slash, entry["description"])
    return True


def load_all():
    """Loads your plugins and the trusted project ones; returns the project plugins still waiting for trust."""
    STATE["loaded"] = True
    waiting, ok = [], trusted()
    for scope, path in found_plugins():
        if scope == "global":
            load_plugin(scope, path)
            continue
        try:
            digest = file_hash(path)
        except OSError:
            continue
        info = PLUGINS.setdefault(str(path), {"name": path.stem, "scope": scope, "status": None, "error": None})
        info["hash"] = digest
        if digest in ok:
            load_plugin(scope, path)
        elif info["status"] != "declined":
            info["status"] = "untrusted"
            waiting.append(path)
    return waiting


def trust_and_load(paths):
    hashes = trusted()
    for path in paths:
        try:
            hashes.add(file_hash(path))
        except OSError:
            continue
    nc.st.config["trusted_plugins"] = sorted(hashes)
    try:
        nc.save_config()
    except OSError as e:
        nc.log_error(e)
    loaded = [path.name for path in paths if load_plugin("project", path)]
    failed = [path.name for path in paths if path.name not in loaded]
    nc.out(f"{nc.GREEN}⏺ Trusted project plugins:{nc.RESET} {', '.join(loaded) or 'none loaded'}"
           + (f" {nc.RED}(failed: {', '.join(failed)}; /plugins shows why){nc.RESET}" if failed else "") + "\n")


def decline(paths):
    for path in paths:
        PLUGINS[str(path)]["status"] = "declined"
    nc.out(f"{nc.DIM}⏺ Project plugins not loaded ({', '.join(p.name for p in paths)}); "
           f"/plugins trust loads them{nc.RESET}\n")


def ask_trust(paths):
    """Asks in the UI whether to run the project's plugins."""
    names = ", ".join(path.name for path in paths)
    nc.out(f"{nc.YELLOW}⏺ This project has plugins:{nc.RESET} {names} {nc.DIM}(in "
           f"{nc.tilde(paths[0].parent)}). They're Python code that runs with your permissions.{nc.RESET}")
    if nc.st.ui.get("open_dialog"):
        nc.st.ui["open_dialog"](f"Run this project's plugins? {names}"[:58], [
            ("Trust and load", "run them now and whenever they're unchanged", lambda: trust_and_load(paths)),
            ("Don't load", "/plugins trust loads them later", lambda: decline(paths))])
    else:
        nc.out(f"  {nc.DIM}/plugins trust loads them{nc.RESET}\n")


@on("startup")
async def startup():
    waiting = load_all()
    if waiting:
        await asyncio.sleep(0.3)               # the app draws first; then the question
        ask_trust(waiting)


@on("turn_start")
def headless(text):
    """`nanocode -p` doesn't start the UI: load the plugins (only trusted project ones) on the first turn."""
    if not STATE["loaded"]:
        load_all()
    return None


@command("/plugins", "Python plugins: list them with errors, or /plugins trust (load this project's)")
async def plugins_command(arg):
    if arg.strip() == "trust":
        load_all()
        pending = [p for scope, p in found_plugins() if scope == "project" and PLUGINS.get(str(p), {}).get("status")
                   in ("untrusted", "declined")]
        if pending:
            trust_and_load(pending)
        else:
            nc.out(f"{nc.DIM}no project plugins waiting for trust{nc.RESET}\n")
        return
    if not STATE["loaded"]:
        load_all()
    lines = [f"{nc.BOLD}Plugins{nc.RESET} {nc.DIM}(*.py in {nc.tilde(nc.home_dir() / 'plugins')} and "
             f".nanocode/plugins){nc.RESET}"]
    marks = {"loaded": (nc.fg("success"), "loaded"), "failed": (nc.fg("error"), "failed"),
             "untrusted": (nc.fg("warning"), "not trusted yet: /plugins trust"),
             "declined": (nc.fg("muted"), "not loaded: /plugins trust")}
    for scope, path in found_plugins():
        info = PLUGINS.get(str(path)) or {"status": None, "error": None}
        color, label = marks.get(info["status"], (nc.fg("muted"), "changed since loaded; restart to reload"))
        lines.append(f"  {nc.CYAN}{path.stem:<18}{nc.RESET} {color}{label}{nc.RESET} {nc.DIM}{scope} · "
                     f"{nc.tilde(path)}{nc.RESET}")
        if info.get("error"):
            lines.append(f"    {nc.fg('error')}{info['error']}{nc.RESET}")
    if not found_plugins():
        lines.append(f"  {nc.DIM}none yet: put a .py file in one of those folders (see docs/plugins.md){nc.RESET}")
    nc.out("\n".join(lines) + "\n")


@cli("plugin", "run a plugin's command: nanocode plugin <name> [args]")
def plugin_cli(argv):
    """Plugins load after the command line is read, so their `@cli` commands run through this one."""
    before = set(nanokit.CLI)
    load_all()
    added = [name for name in nanokit.CLI if name not in before]
    if not argv or argv[0] not in added:
        names = ", ".join(added) or "none"
        print(f"usage: nanocode plugin <name> [args]   (plugin commands: {names})")
        for scope, path in found_plugins():
            info = PLUGINS.get(str(path)) or {}
            if info.get("status") in ("failed", "untrusted"):
                print(f"  {path.name}: {info.get('error') or 'not trusted yet (run /plugins trust in nanocode)'}")
        return 2
    return nanokit.CLI[argv[0]]["fn"](list(argv[1:]))
