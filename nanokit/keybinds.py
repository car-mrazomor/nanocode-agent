"""Your own key bindings: "keybinds" in the config maps an action to a key (or a list of keys), in
prompt_toolkit's notation ("c-t", "c-x w" for a two-key sequence, "escape n" for Alt+N) or the friendlier
"ctrl+t", "ctrl+x w", "alt+n". Your keys are added on top of the built-in ones (which keep working).

    "keybinds": {"new_tab": "c-n", "palette": "c-k", "editor": "c-x c-e", "models": "alt+m"}
"""
from __future__ import annotations

import asyncio

import nanocode as nc
from nanokit import command, on


def run(text):
    return lambda ui: nc.start_input(text)


def ui_action(name):
    def call(ui):
        if ui.get(name):
            ui[name]()
    return call


def cycle(step):
    def call(ui):
        if len(nc.st.tabs) > 1:
            nc.switch_tab((nc.st.active + step) % len(nc.st.tabs))
    return call


def open_editor(ui):
    try:
        from nanokit import editor
    except ImportError:
        return
    editor.start()


# action -> (what it does, fn(ui))
ACTIONS = {
    "palette": ("the command palette (built in: Ctrl+P)", ui_action("palette")),
    "new_tab": ("new tab (Ctrl+T)", lambda ui: nc.new_tab()),
    "close_tab": ("close this tab (Ctrl+X W)", lambda ui: asyncio.ensure_future(nc.close_tab())),
    "next_tab": ("next tab (Ctrl+X →)", cycle(1)),
    "prev_tab": ("previous tab (Ctrl+X ←)", cycle(-1)),
    "tabs": ("list the tabs", ui_action("tabs_dialog")),
    "sessions": ("saved chats (Ctrl+X L)", ui_action("sessions_dialog")),
    "models": ("switch model (Ctrl+X M)", ui_action("models_dialog")),
    "new_chat": ("new chat in this tab (Ctrl+X N)", run("/c")),
    "compact": ("summarize the chat (Ctrl+X C)", run("/compact")),
    "undo": ("undo the last file change (Ctrl+X U)", run("/undo")),
    "help": ("commands and keys (Ctrl+X H)", run("/help")),
    "recap": ("what happened this session", run("/recap")),
    "cost": ("tokens and cost", run("/cost")),
    "auto": ("auto-approve on or off", run("/auto")),
    "editor": ("write the prompt in your editor (Ctrl+X E)", open_editor),
    "quit": ("quit (Ctrl+X Q)", lambda ui: nc.st.app.exit()),
}
WORDS = {"ctrl": "c", "control": "c", "shift": "s"}


def parse_keys(text):
    """"ctrl+x w" or "c-x w" -> ("c-x", "w"); "alt+n" -> ("escape", "n")."""
    keys = []
    for part in str(text).split():
        part = part.strip()
        lower = part.lower()
        if lower.startswith(("alt+", "meta+", "option+")):
            keys += ["escape", parse_keys(part.split("+", 1)[1])[0]]
            continue
        if "+" in part and len(part) > 1:
            mod, key = lower.split("+", 1)
            if mod in WORDS:
                keys.append(f"{WORDS[mod]}-{key}")
                continue
        keys.append(lower if len(part) > 1 else part)
    return tuple(keys)


def configured():
    """[(action, keys text)] from the config, unknown actions included (they're reported)."""
    value = nc.st.config.get("keybinds")
    pairs = []
    for action, keys in (value.items() if isinstance(value, dict) else []):
        for key in keys if isinstance(keys, list) else [keys]:
            if isinstance(key, str) and key.strip():
                pairs.append((action, key.strip()))
    return pairs


PROBLEMS = []  # (action, key, why) found when the keys were bound


@on("keybindings")
def bind(kb, ui):
    from prompt_toolkit.filters import Condition
    no_dialog = Condition(lambda: nc.st.dialog is None)
    PROBLEMS.clear()
    for action, key in configured():
        if action not in ACTIONS:
            PROBLEMS.append((action, key, "unknown action"))
            continue
        fn = ACTIONS[action][1]
        try:
            kb.add(*parse_keys(key), filter=no_dialog)(lambda event, fn=fn: fn(ui))
        except (ValueError, IndexError) as e:
            PROBLEMS.append((action, key, str(e) or "not a key"))


@command("/keybinds", "your key bindings (\"keybinds\" in the config) and the actions you can bind")
async def keybinds_command(arg):
    mine = {}
    for action, key in configured():
        mine.setdefault(action, []).append(key)
    lines = [f"{nc.BOLD}Key bindings{nc.RESET} {nc.DIM}(\"keybinds\" in {nc.tilde(nc.config_file())}; "
             f"they apply when nanocode starts){nc.RESET}"]
    for action, (desc, _) in ACTIONS.items():
        keys = f"{nc.fg('accent2')}{', '.join(mine.get(action, []))}{nc.RESET} " if action in mine else ""
        lines.append(f"  {nc.CYAN}{action:<10}{nc.RESET} {keys}{nc.DIM}{desc}{nc.RESET}")
    for action, key, why in PROBLEMS:
        lines.append(f"  {nc.fg('error')}{action}: {key}: {why}{nc.RESET}")
    lines.append(f"  {nc.DIM}e.g. \"keybinds\": {{\"new_tab\": \"ctrl+n\", \"editor\": \"ctrl+x ctrl+e\", "
                 f"\"models\": \"alt+m\"}}{nc.RESET}")
    nc.out("\n".join(lines) + "\n")
