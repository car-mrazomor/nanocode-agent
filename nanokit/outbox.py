"""Files the model gives you, in the apps: `send_file` puts a picture or a file in the chat of the web app or the
iOS app (anything talking to `nanocode serve`). You talk to nanocode from a browser or a phone there, and can't
open files on the computer it runs on, so this is how it hands you a chart it drew, a PDF, a CSV, a build.

The file is copied to .nanocode/outbox, so what you get is what it sent even if the original changes later.
The API serves it at GET /v1/files/<id> (with the key); the turn's events have a `file` event, and the tool's
result names it ([file:<id>:<type>]), so a chat shows its files again when it's opened later.

Links need nothing special: the model writes them in its reply as Markdown, and the apps make them clickable.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil

import nanocode as nc
from nanokit import on

OUTBOX_LIMIT = 50 * 1024 * 1024        # bytes per file
FILE_ID_RE = re.compile(r"[0-9a-f]{16}")
RESULT_RE = re.compile(r"\[file:([0-9a-f]{16}):([^\]]+)\]")

nc.READ_ONLY_TOOLS.add("send_file")    # it only reads: fine on a read-only server and in plan mode


def outbox_dir():
    return nc.project_dir() / "outbox"


def safe_name(name):
    name = re.sub(r"[^\w.\- ]", "_", os.path.basename(str(name)))[:120].strip(" .")
    return name or "file"


def find(file_id):
    """(path, name, mime type) of a sent file, or None."""
    if not FILE_ID_RE.fullmatch(str(file_id or "")):
        return None
    folder = outbox_dir()
    if not folder.is_dir():
        return None
    for path in folder.glob(f"{file_id}-*"):
        if path.is_file():
            name = path.name[len(file_id) + 1:]
            return path, name, mimetypes.guess_type(name)[0] or "application/octet-stream"
    return None


def size_text(n):
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"


@nc.tool("send_file",
         "Send a file or a picture to the user: it shows in the chat of the app they talk to you from (a browser or "
         "their phone), where they can see or download it. They can't open files on this computer, so use this "
         "to hand them anything they asked for or should see: an image or chart you made, a PDF, a CSV, a zip, a "
         "build. Pictures show right in the chat. Links don't need this: write them in your reply as Markdown.",
         {"path": {"type": "string", "description": "the file to send"},
          "caption": {"type": "string", "description": "a short note shown with it (optional)"}},
         required=["path"])
async def send_file(args):
    path = nc.resolve(args.get("path") or "")
    if not path.is_file():
        raise nc.ToolError(f"{args.get('path')} isn't a file")
    size = path.stat().st_size
    if size > OUTBOX_LIMIT:
        raise nc.ToolError(f"{path.name} is {size_text(size)}; files up to {OUTBOX_LIMIT // (1024 * 1024)} MB can be sent")
    file_id, name = os.urandom(8).hex(), safe_name(path.name)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    nc.ensure_project_dir()
    outbox_dir().mkdir(exist_ok=True)
    await asyncio.to_thread(shutil.copyfile, path, outbox_dir() / f"{file_id}-{name}")
    caption = str(args.get("caption") or "").strip()
    nc.emit("file", id=file_id, name=name, mime=mime, size=size, caption=caption, url=f"/v1/files/{file_id}")
    return f"sent to the user: {name} ({size_text(size)}) [file:{file_id}:{mime}]" + (f"\n{caption}" if caption else "")


@on("system_prompt")
def app_note():
    if nc.SURFACE != "api":
        return None
    return ("# The user's app\nThe user talks to you from an app (a browser or their phone), not this computer's "
            "terminal: they can't open files here. To give them a file or a picture, use send_file; it shows in "
            "their chat. Write links as Markdown.")
