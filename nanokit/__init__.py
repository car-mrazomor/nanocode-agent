"""nanokit: nanocode's feature modules.

nanocode.py is the core (the UI, the agent loop, the file tools, providers). Each module here adds one feature
on top of it, through a few small hooks, so the core stays readable and a feature can be read (or removed) on
its own. A module registers what it adds when it's imported:

    import nanocode as nc
    from nanokit import command, on, cli

    @command("/stats", "usage across your chats")
    async def stats(arg):           # runs like any other slash command, inside the current tab
        nc.out("...")

    @on("after_tool")               # see EVENTS for every hook and what it can return
    def note(name, args, result, is_error):
        return None

    @cli("gateway", "run the messaging gateway")
    def gateway(argv):              # `nanocode gateway ...`; returns the exit code
        return 0

Tools are added with nanocode's own decorator, `@nc.tool(...)`. A module that fails to import is skipped (the
error is logged and shown by `nanocode doctor`); the rest of nanocode keeps working.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect

# The feature modules, loaded in this order. Each is optional: a missing or broken one is skipped.
MODULES = [
    "snapshots", "formatters", "lsp", "permissions", "modes", "init", "agents", "commands", "hooks", "plugins",
    "sessions", "share", "stats", "providers", "mcp_http", "acp", "github", "web", "editor", "keybinds", "themes",
    "environments", "browser", "voice", "skillshub", "improve", "trajectories", "cron", "gateway", "outbox",
]

COMMANDS = {}  # "/name" -> {"fn": async fn(arg), "description": str, "anytime": bool}
CLI = {}  # "name" -> {"fn": fn(argv) -> int, "description": str}
HOOKS = {}  # event -> [fn]
LOADED = []  # modules that loaded
ERRORS = {}  # module -> why it didn't load

# Every hook nanocode calls, with what it passes and what a hook can return (None means "nothing to add").
EVENTS = {
    "startup": "the interactive app started (no arguments)",
    "shutdown": "the app is closing (no arguments)",
    "turn_start": "text: a user message is about to be sent to the model, in its tab",
    "turn_end": "text, error: that turn ended (error is the message, or None)",
    "before_tool": "name, args: about to run a tool. Return {'deny': reason} to refuse it, or {'args': {...}} "
                   "to change its arguments",
    "after_tool": "name, args, result, is_error: a tool ran. Return text to add to the result the model sees",
    "permission": "tool, preview: before asking the user. Return 'allow', 'deny' or 'ask' to decide",
    "wrap_command": "command, cwd: a shell command is about to run (bash, shell). Return a different shell "
                    "command to run it elsewhere (a container, over SSH)",
    "system_prompt": "the system prompt is being built. Return a section (markdown) to add",
    "keybindings": "kb, ui: the app's key bindings are being made. Add your own to kb",
    "message": "text: a message typed in the prompt, before it's handled. Return new text, or '' to swallow it",
}


def command(name, description, anytime=False):
    """A slash command. anytime=True: it runs even while a reply is being written (instead of being queued)."""
    def wrap(fn):
        COMMANDS[name] = {"fn": fn, "description": description, "anytime": anytime}
        return fn
    return wrap


def cli(name, description):
    """A `nanocode <name> ...` command line command."""
    def wrap(fn):
        CLI[name] = {"fn": fn, "description": description}
        return fn
    return wrap


def on(event):
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r} (see nanokit.EVENTS)")

    def wrap(fn):
        HOOKS.setdefault(event, []).append(fn)
        return fn
    return wrap


def _log(e):
    try:
        import nanocode as nc
        nc.log_error(e)
    except Exception:
        pass


async def emit(event, **data):
    """Calls every hook for an event (sync or async) and returns their answers that aren't None."""
    results = []
    for fn in list(HOOKS.get(event, ())):
        try:
            value = fn(**data)
            if inspect.isawaitable(value):
                value = await value
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a broken feature mustn't break a turn
            _log(e)
            continue
        if value is not None:
            results.append(value)
    return results


def emit_sync(event, **data):
    """For hooks called where nothing can be awaited (async hooks are skipped there)."""
    results = []
    for fn in list(HOOKS.get(event, ())):
        try:
            value = fn(**data)
        except Exception as e:
            _log(e)
            continue
        if inspect.isawaitable(value):
            value.close()
            continue
        if value is not None:
            results.append(value)
    return results


def load():
    """Imports the feature modules (once)."""
    for name in MODULES:
        if name in LOADED or name in ERRORS:
            continue
        try:
            importlib.import_module(f"{__name__}.{name}")
            LOADED.append(name)
        except ModuleNotFoundError as e:
            if e.name == f"{__name__}.{name}":
                continue                      # not written (or not installed): nothing to add
            ERRORS[name] = f"{type(e).__name__}: {e}"
            _log(e)
        except Exception as e:
            ERRORS[name] = f"{type(e).__name__}: {e}"
            _log(e)
