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

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="96" fill="#0a0a0a"/>
<path d="M150 360V152h36l140 156V152h36v208h-36L186 204v156z" fill="#7fa7c9"/>
<rect x="150" y="384" width="212" height="18" rx="9" fill="#9d8fc4"/>
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
