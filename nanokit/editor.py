"""Write the prompt in your editor: Ctrl+X E opens what you've typed in $VISUAL or $EDITOR (or nano, vi,
Notepad on Windows); when you save and quit, the text comes back into the input box, ready to send.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import nanocode as nc
from nanokit import command, on


def editor_command():
    """The editor to run, as a list of arguments (the file goes last), or None if there's none."""
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var, "").strip()
        if value:
            try:
                if os.name == "nt":            # "C:\Program Files\...\code.exe" --wait: keep the backslashes
                    return [part.strip('"') for part in shlex.split(value, posix=False)]
                return shlex.split(value)
            except ValueError:
                return [value]
    if os.name == "nt":
        return ["notepad"]
    for name in ("nano", "vim", "vi"):
        if shutil.which(name):
            return [name]
    return None


def edit_text(text, run=subprocess.call):
    """Opens text in the editor and returns what was saved (the same text if the editor failed)."""
    cmd = editor_command()
    if not cmd:
        raise RuntimeError("no editor found: set $EDITOR (e.g. export EDITOR=nano)")
    fd, name = tempfile.mkstemp(prefix="nanocode-prompt-", suffix=".md")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        code = run(cmd + [str(path)])
        if code not in (0, None):
            raise RuntimeError(f"the editor ({cmd[0]}) exited with code {code}")
        return path.read_text(encoding="utf-8").rstrip("\n")
    finally:
        try:
            path.unlink()
        except OSError:
            pass


async def open_in_editor():
    """Hands the terminal to the editor (the app steps aside meanwhile), then puts the text in the input."""
    from prompt_toolkit.application import run_in_terminal
    box = nc.st.input_box
    if box is None or nc.st.app is None:
        return
    output = nc.st.app.output
    keys = getattr(nc.st, "translate_keys", False)

    def run():
        output.write_raw((nc.KEYS_OFF if keys else "") + nc.FOCUS_OFF)   # plain keys for the editor
        output.flush()
        try:
            return edit_text(box.text)
        finally:
            output.write_raw((nc.KEYS_ON if keys else "") + nc.FOCUS_ON)
            output.flush()

    try:
        text = await run_in_terminal(run, in_executor=True)
    except Exception as e:
        nc.show_toast(str(e), seconds=5, variant="error")
        return
    box.text = text
    box.buffer.cursor_position = len(text)
    nc.invalidate()


def start():
    asyncio.ensure_future(open_in_editor())


@on("keybindings")
def bind(kb, ui):
    from prompt_toolkit.filters import Condition
    kb.add("c-x", "e", filter=Condition(lambda: nc.st.dialog is None))(lambda event: start())


@command("/editor", "write the prompt in your editor ($VISUAL or $EDITOR; also Ctrl+X E)", anytime=True)
async def editor_slash(arg):
    if arg and nc.st.input_box is not None:
        nc.st.input_box.text = arg
    await open_in_editor()


HELP = ("Ctrl+X E", "write the prompt in your editor ($EDITOR)")
if HELP not in nc.KEYS_HELP:
    nc.KEYS_HELP.append(HELP)
