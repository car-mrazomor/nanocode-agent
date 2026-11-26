"""nanocode in the browser: a chat page served by `nanocode serve` (GET /), and `nanocode web` to open it.

The page (nanokit/web/index.html, one file, no CDN) talks to nanocode's HTTP API like any other client: it lists
the saved chats, starts turns with POST /v1/runs and follows their events. It asks for the API key once and keeps
it in the browser. It can be installed as an app (a PWA): the manifest, icon and a small service worker are
served next to it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path

import nanocode as nc
from nanokit import cli

PAGE = Path(__file__).resolve().parent / "web" / "index.html"

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="512" height="512">
<title>nanocode</title>
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#6fb6c6"/><stop offset=".5" stop-color="#8a8cc4"/><stop offset="1" stop-color="#bf89ae"/>
</linearGradient></defs>
<rect x="1" y="1" width="46" height="46" rx="12" fill="#1b1d21" stroke="#34373d"/>
<rect x="12" y="11" width="5" height="26" rx="2.5" fill="url(#g)"/>
<path d="M24 17l9 7-9 7" fill="none" stroke="#e6e7ea" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""

MANIFEST = {
    "name": "nanocode", "short_name": "nanocode", "description": "A small coding agent",
    "start_url": "./", "scope": "./", "display": "standalone",
    "background_color": "#0a0a0a", "theme_color": "#0a0a0a",
    "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
}

# Makes the page installable; it never caches anything (the chat is always live).
SERVICE_WORKER = """self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
"""

CSP = ("default-src 'none'; script-src 'unsafe-inline' 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data:; manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


def static(path):
    """(content type, bytes, extra headers) of one of the page's files, or None. No key needed: they hold no data."""
    if path in ("/", "/index.html"):
        try:
            return "text/html; charset=utf-8", PAGE.read_bytes(), {"Content-Security-Policy": CSP,
                                                                   "Cache-Control": "no-cache"}
        except OSError:
            return None
    if path == "/manifest.webmanifest":
        return "application/manifest+json", json.dumps(MANIFEST).encode(), {}
    if path == "/icon.svg":
        return "image/svg+xml", ICON.encode(), {"Cache-Control": "max-age=86400"}
    if path == "/sw.js":
        return "text/javascript", SERVICE_WORKER.encode(), {"Cache-Control": "no-cache"}
    return None


def session_turns(data):
    """A saved chat as the page shows it: [{role: user|assistant|tool|tool_result|screen, ...}]."""
    from nanokit.sessions import content_text, screen_text
    turns = []
    for m in data.get("messages") or []:
        role = m.get("role")
        text = content_text(m.get("content"))
        if role == "user" and text:
            turns.append({"role": "user", "text": text})
        elif role == "assistant":
            if text.strip():
                turns.append({"role": "assistant", "text": text})
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                turns.append({"role": "tool", "id": call.get("id"), "name": fn.get("name"), "arguments": args})
        elif role == "tool":
            turns.append({"role": "tool_result", "id": m.get("tool_call_id"), "text": nc.cap(text, 20_000),
                          "is_error": text.startswith("error:")})
    if not turns and data.get("backend") == "sdk":
        # Chats on the Claude subscription: nanocode's own copy ("turns", kept since 0.9.3), or Claude Code's
        # transcript for older chats that started before that copy (whichever holds more of the chat).
        own = [t for t in data.get("turns") or [] if isinstance(t, dict) and t.get("role")]
        claude = claude_turns(data["sdk_session_id"]) if data.get("sdk_session_id") else []
        turns = claude if len(claude) > len(own) else own
    if not turns and data.get("screen"):
        turns.append({"role": "screen", "text": screen_text(data["screen"])})
    return turns


@cli("web", "open nanocode's chat page in your browser (starts the HTTP API on this machine)")
def web_cli(argv):
    parser = argparse.ArgumentParser(prog="nanocode web", description="nanocode in the browser.")
    parser.add_argument("--port", type=int, help="port (default: the API's, 8765)")
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    parser.add_argument("-m", "--model")
    parser.add_argument("--provider", choices=list(nc.PROVIDERS))
    args = parser.parse_args(argv)
    try:
        from api import server
    except ImportError:
        print("the api/ folder is missing next to nanocode.py: run `nanocode update`", file=sys.stderr)
        return 1
    if args.model:
        os.environ["MODEL"] = args.model
    nc.migrate_legacy()
    error = nc.apply_config(args.provider)
    if error:
        print(f"nanocode web: {error}", file=sys.stderr)
        return 1
    server.nc = nc
    host, port, key = server.api_settings("127.0.0.1", args.port)
    url = f"http://{host}:{port}/#key={key}"

    async def main():
        if not args.no_open:
            asyncio.get_running_loop().call_later(1.0, webbrowser.open, url)
        return await server.serve(nc, host, port)
    print(f"nanocode web: {url.split('#')[0]}" + ("" if args.no_open else "  (opening it in your browser)"),
          file=sys.stderr)
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 0


# --- Chats on the Claude subscription: read from Claude Code's own transcript ---

def claude_transcript(sdk_session_id):
    """The .jsonl file Claude Code keeps for a conversation, or None."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", sdk_session_id or ""):
        return None
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    exact = base / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()) / f"{sdk_session_id}.jsonl"
    if exact.is_file():
        return exact
    return next(iter(base.glob(f"*/{sdk_session_id}.jsonl")), None)


def claude_turns(sdk_session_id):
    """The turns of a Claude Code conversation, in the same shape as session_turns: your messages, replies, tool
    calls and their results. Sub-agents' work and Claude Code's own notes are left out."""
    path = claude_transcript(sdk_session_id)
    if path is None:
        return []
    turns = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") not in ("user", "assistant") or entry.get("isSidechain") or entry.get("isMeta") \
                or entry.get("isCompactSummary"):
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = (block.get("text") or "").strip()
                if not text or text.startswith(("<system-reminder>", "<command-", "<local-command")):
                    continue
                role = "user" if message.get("role") == "user" else "assistant"
                if role == "assistant" and turns and turns[-1]["role"] == "assistant":
                    turns[-1]["text"] += "\n\n" + text
                else:
                    turns.append({"role": role, "text": text})
            elif kind == "image" and message.get("role") == "user":
                if turns and turns[-1]["role"] == "user":
                    turns[-1]["text"] += "\n[image]"
                else:
                    turns.append({"role": "user", "text": "[image]"})
            elif kind == "tool_use":
                name = str(block.get("name") or "tool")
                name = name[len("mcp__nano__"):] if name.startswith("mcp__nano__") else name   # nanocode's own tools
                turns.append({"role": "tool", "id": block.get("id"), "name": name,
                              "arguments": block.get("input") if isinstance(block.get("input"), dict) else {}})
            elif kind == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    result = "\n".join(p.get("text") or "" for p in result if isinstance(p, dict))
                turns.append({"role": "tool_result", "id": block.get("tool_use_id"), "text": nc.cap(str(result or ""), 20_000),
                              "is_error": bool(block.get("is_error"))})
    return turns

