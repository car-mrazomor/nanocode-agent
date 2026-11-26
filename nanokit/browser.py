"""Browser automation: open pages, read them, click, type, scroll and take screenshots, in the user's own
Chrome, Chromium, Edge or Brave, driven over the Chrome DevTools Protocol (CDP).

The browser is started on first use with a throwaway profile (headless unless "browser_headless": false), or
nanocode connects to one that's already running ("browser_cdp_url": "http://127.0.0.1:9222"). It's closed when
nanocode exits, by browser_close, or when it fails to start.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time

import httpx

import nanocode as nc
from nanokit import on

TIMEOUT = 30          # seconds for one CDP command
LOAD_TIMEOUT = 20     # seconds to wait for a page to finish loading
MAX_SNAPSHOT = 30_000

# Numbers the visible interactive elements (data-nc-ref) and returns the page as text with [N] marks.
SNAPSHOT_JS = r"""
(() => {
  const INTERACTIVE = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=checkbox],' +
    '[role=tab],[role=menuitem],[role=option],[onclick],[contenteditable=""],[contenteditable=true]';
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'HEAD']);
  const BLOCK = /^(P|DIV|LI|TR|SECTION|ARTICLE|HEADER|FOOTER|H[1-6]|PRE|TABLE|UL|OL|FORM|MAIN|NAV|ASIDE|BR|HR)$/;
  document.querySelectorAll('[data-nc-ref]').forEach(e => e.removeAttribute('data-nc-ref'));
  let n = 0, out = [];
  const visible = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const field = e => /^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName) && !/^(submit|button|reset)$/.test(e.type);
  const label = e => (e.getAttribute('aria-label') || (field(e) ? e.placeholder || e.name : e.innerText || e.value) ||
    e.title || e.alt || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const walk = node => {
    if (node.nodeType === 3) { const t = node.textContent.replace(/\s+/g, ' '); if (t.trim()) out.push(t); return; }
    if (node.nodeType !== 1 || SKIP.has(node.tagName.toUpperCase())) return;
    if (node.matches(INTERACTIVE) && visible(node)) {
      n += 1; node.setAttribute('data-nc-ref', String(n));
      const tag = node.tagName.toLowerCase();
      let kind = node.getAttribute('role') || (tag === 'a' ? 'link' : tag);
      if (tag === 'input') kind = 'input ' + (node.type || 'text');
      let extra = '';
      if (tag === 'input' || tag === 'textarea') extra = node.value ? ` = "${String(node.value).slice(0, 60)}"` : '';
      if (node.checked) extra += ' (checked)';
      out.push(` [${n}] ${kind} "${label(node)}"${extra} `);
      if (tag === 'select' || tag === 'input' || tag === 'textarea' || tag === 'button') return;
      if (tag === 'a') return;
    }
    if (BLOCK.test(node.tagName)) out.push('\n');
    for (const c of node.childNodes) walk(c);
    if (node.shadowRoot) for (const c of node.shadowRoot.childNodes) walk(c);
    if (BLOCK.test(node.tagName)) out.push('\n');
  };
  if (document.body) walk(document.body);
  const text = out.join('').replace(/[ \t]+/g, ' ').replace(/ *\n\s*/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  return JSON.stringify({title: document.title, url: location.href, text: text, refs: n});
})()
"""


class Browser:
    """One browser page driven over CDP: the websocket, and the process when nanocode started it."""

    def __init__(self):
        self.proc = None       # the browser nanocode started (None when connected to one)
        self.profile = None    # its throwaway profile folder
        self.ws = None
        self.next_id = 0
        self.lock = asyncio.Lock()

    async def start(self):
        cdp_url = str(nc.st.config.get("browser_cdp_url") or "").strip()
        try:
            if cdp_url:
                ws_url = cdp_url if cdp_url.startswith(("ws://", "wss://")) else await page_ws_url(cdp_url)
            else:
                ws_url = await self.launch()
            self.ws = await connect(ws_url)
            await self.send("Page.enable")
            await self.send("Runtime.enable")
        except BaseException:
            await self.close()
            raise

    async def launch(self):
        exe = nc.find_browser()
        if not exe:
            raise nc.ToolError("no Chrome, Chromium, Edge or Brave found. Install one, or set \"browser\" (its path) "
                               "or \"browser_cdp_url\" in the config")
        port = free_port()
        self.profile = tempfile.mkdtemp(prefix="nanocode-browser-")
        args = [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--window-size=1280,900", "about:blank"]
        if nc.st.config.get("browser_headless", True) is not False:
            args[1:1] = ["--headless=new", "--disable-gpu", "--mute-audio"]
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            **({"start_new_session": True} if os.name != "nt" else {}))   # so its helper processes stop with it
        deadline = time.monotonic() + 20
        while True:
            if self.proc.returncode is not None:
                raise nc.ToolError(f"the browser exited at start (code {self.proc.returncode})")
            try:
                return await page_ws_url(f"http://127.0.0.1:{port}")
            except (httpx.HTTPError, nc.ToolError):
                if time.monotonic() > deadline:
                    raise nc.ToolError("the browser didn't open its debugging port in 20s") from None
                await asyncio.sleep(0.2)

    async def send(self, method, params=None):
        """Runs one CDP command and returns its result (events that arrive meanwhile are skipped)."""
        if self.ws is None:
            raise nc.ToolError("the browser isn't open")
        async with self.lock:
            self.next_id += 1
            msg_id = self.next_id
            try:
                await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                deadline = time.monotonic() + TIMEOUT
                while True:
                    raw = await asyncio.wait_for(self.ws.recv(), max(0.1, deadline - time.monotonic()))
                    msg = json.loads(raw)
                    if msg.get("id") == msg_id:
                        break
            except asyncio.TimeoutError:
                raise nc.ToolError(f"the browser didn't answer {method} in {TIMEOUT}s") from None
            except (asyncio.CancelledError, nc.ToolError):
                raise
            except Exception as e:             # the connection dropped: the browser crashed or was closed
                await asyncio.shield(self.close())
                raise nc.ToolError(f"lost the browser ({type(e).__name__}); the next browser tool opens it "
                                   "again") from e
        if "error" in msg:
            raise nc.ToolError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    async def evaluate(self, expression):
        result = await self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                      "awaitPromise": True})
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or "exception"
            raise nc.ToolError(f"JavaScript error: {text}")
        return (result.get("result") or {}).get("value")

    async def wait_loaded(self, timeout=LOAD_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await self.evaluate("document.readyState") == "complete":
                    return
            except nc.ToolError:
                pass                           # mid-navigation: the old page's context went away
            await asyncio.sleep(0.25)

    async def close(self):
        ws, proc, profile = self.ws, self.proc, self.profile
        self.ws = self.proc = self.profile = None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), 3)
            except Exception:
                pass
        if proc is not None:
            await nc.reap(proc)
        if profile:
            await asyncio.sleep(0)             # Windows: the profile's files are free once the browser exits
            shutil.rmtree(profile, ignore_errors=True)


BROWSER = None


async def browser():
    """The open browser, started on first use."""
    global BROWSER
    if BROWSER is None or BROWSER.ws is None:
        BROWSER = Browser()
        await BROWSER.start()
    return BROWSER


async def connect(ws_url):
    try:
        import websockets
    except ImportError:
        raise nc.ToolError("the browser tools need the websockets package: pip install websockets") from None
    try:
        return await websockets.connect(ws_url, max_size=None, open_timeout=15)
    except Exception as e:
        raise nc.ToolError(f"couldn't connect to the browser at {ws_url}: {e}") from e


async def page_ws_url(base):
    """The websocket of a page tab of the browser at base (http://host:port), opening one if there's none."""
    base = base.rstrip("/")
    async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
        targets = (await http.get(f"{base}/json/list")).json()
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            resp = await http.put(f"{base}/json/new?about:blank")
            pages = [resp.json()] if resp.status_code < 400 else []
    if not pages or not pages[0].get("webSocketDebuggerUrl"):
        raise nc.ToolError(f"no page to drive at {base}")
    return pages[0]["webSocketDebuggerUrl"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def element_js(target):
    """A JS expression for the element a ref number ("12") or a CSS selector names."""
    target = str(target).strip()
    if re.fullmatch(r"\[?\d+\]?", target):
        selector = f'[data-nc-ref="{target.strip("[]")}"]'
    else:
        selector = target
    return f"document.querySelector({json.dumps(selector)})"


async def locate(page, target):
    """Scrolls the element into view and returns its center (x, y), or None when it has no box."""
    found = await page.evaluate(
        f"(() => {{ const e = {element_js(target)}; if (!e) return null; e.scrollIntoView({{block: 'center'}});"
        " const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2, r.width * r.height]; })()")
    if found is None:
        raise nc.ToolError(f"no element {target!r} on the page; take a browser_snapshot for the current numbers")
    x, y, area = found
    return (x, y) if area > 0 else None


async def snapshot_text(page):
    data = json.loads(await page.evaluate(SNAPSHOT_JS) or "{}")
    text = data.get("text") or "(no text on this page)"
    if len(text) > MAX_SNAPSHOT:
        text = text[:MAX_SNAPSHOT] + f"\n\n… [{len(text) - MAX_SNAPSHOT} more characters cut]"
    return (f"{data.get('title') or '(untitled)'}\n{data.get('url', '')} · {data.get('refs', 0)} interactive "
            f"elements, marked [N]: use the number with browser_click / browser_type\n\n{text}")


def web_url(url):
    """An http(s) URL to open; anything else (file:, javascript:, data:, chrome:…) is refused."""
    url = url.strip()
    if re.match(r"(?:localhost|[\w.-]+\.[\w-]+|\d+\.\d+\.\d+\.\d+|\[[\da-f:]+\]):\d+(?:[/?#]|$)", url, re.I):
        url = "http://" + url                       # localhost:3000, example.com:8080/x
    elif not re.match(r"[a-z][a-z0-9+.-]*:", url, re.I):
        url = "https://" + url
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise nc.ToolError(f"browser_open only opens http and https pages, not {scheme}: URLs")
    return url


async def real_browser_allows(action):
    """Attached to the user's own browser (browser_cdp_url), with their logins: clicks and typing ask first."""
    if not str(nc.st.config.get("browser_cdp_url") or "").strip():
        return True
    return await nc.allowed("browser", f"{action} in your browser")


# --- tools ---

@nc.tool("browser_open", "Open a URL in a real browser (JavaScript runs; the browser stays open between calls). "
         "Returns the page as text with numbered interactive elements, like browser_snapshot. Use this to "
         "interact with pages (click, type, log in); for just reading a page, web_fetch is lighter.",
         {"url": {"type": "string"}}, ["url"])
async def tool_browser_open(args):
    url = web_url(args["url"])
    if not await nc.allowed("browser", f"open {url} in the browser"):
        return nc.DENIED
    page = await browser()
    result = await page.send("Page.navigate", {"url": url})
    if result.get("errorText"):
        raise nc.ToolError(f"couldn't open {url}: {result['errorText']}")
    await page.wait_loaded()
    return await snapshot_text(page)


@nc.tool("browser_snapshot", "The current browser page as text, with interactive elements numbered [N] "
         "(numbers change after the page changes: take a new snapshot before clicking).", {})
async def tool_browser_snapshot(args):
    return await snapshot_text(await browser())


@nc.tool("browser_click", "Click an element in the browser: its [N] number from the last snapshot, or a CSS selector.",
         {"ref": {"type": "string", "description": "the element's number, or a CSS selector"}}, ["ref"])
async def tool_browser_click(args):
    if not await real_browser_allows(f"click {args['ref']}"):
        return nc.DENIED
    page = await browser()
    point = await locate(page, args["ref"])
    if point:
        x, y = point
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await page.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                         "clickCount": 0 if kind == "mouseMoved" else 1})
    else:                                      # no box (hidden, zero size): a script click
        await page.evaluate(f"{element_js(args['ref'])}.click()")
    await asyncio.sleep(0.5)                   # whatever the click starts (navigation, a menu)
    await page.wait_loaded()
    return f"clicked {args['ref']}\n\n" + await snapshot_text(page)


@nc.tool("browser_type", "Type text into a field in the browser (its [N] number or a CSS selector), replacing "
         "what's there. submit=true presses Enter after.",
         {"ref": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["ref", "text"])
async def tool_browser_type(args):
    action = f"type {str(args['text'])[:80]!r} into {args['ref']}" + (" and press Enter" if args.get("submit") else "")
    if not await real_browser_allows(action):
        return nc.DENIED
    page = await browser()
    found = await page.evaluate(
        f"(() => {{ const e = {element_js(args['ref'])}; if (!e) return false; e.scrollIntoView({{block: 'center'}});"
        " e.focus(); if ('value' in e) { e.value = ''; e.dispatchEvent(new Event('input', {bubbles: true})); }"
        " else if (e.isContentEditable) e.textContent = ''; return true; })()")
    if not found:
        raise nc.ToolError(f"no element {args['ref']!r} on the page; take a browser_snapshot for the current numbers")
    await page.send("Input.insertText", {"text": str(args["text"])})
    if args.get("submit"):
        for kind in ("keyDown", "keyUp"):
            await page.send("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                       "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                                                       **({"text": "\r"} if kind == "keyDown" else {})})
        await asyncio.sleep(0.5)
        await page.wait_loaded()
        return f"typed into {args['ref']} and pressed Enter\n\n" + await snapshot_text(page)
    return f"typed into {args['ref']}"


@nc.tool("browser_scroll", "Scroll the browser page: direction up or down (default down), by pages (default 1).",
         {"direction": {"type": "string", "enum": ["up", "down"]}, "pages": {"type": "number"}})
async def tool_browser_scroll(args):
    page = await browser()
    pages = float(args.get("pages") or 1) * (-1 if args.get("direction") == "up" else 1)
    where = await page.evaluate(f"(() => {{ window.scrollBy(0, window.innerHeight * {pages}); "
                                "return [Math.round(window.scrollY), document.documentElement.scrollHeight]; })()")
    return f"scrolled to {where[0]} of {where[1]} px\n\n" + await snapshot_text(page)


@nc.tool("browser_screenshot", "Take a PNG screenshot of the browser page (full_page for all of it). Saves it in "
         ".nanocode/images and returns the path.", {"full_page": {"type": "boolean"}})
async def tool_browser_screenshot(args):
    page = await browser()
    result = await page.send("Page.captureScreenshot", {"format": "png",
                                                        "captureBeyondViewport": bool(args.get("full_page"))})
    name = nc.save_image(base64.b64decode(result["data"]), "image/png")
    path = nc.project_dir() / "images" / name
    return (f"screenshot saved: {path}\n(the user can see it by opening the file, or send it to you as an image "
            f"with @{nc.display_path(path)})")


@nc.tool("browser_eval", "Run JavaScript in the browser page and return its result (as JSON).",
         {"js": {"type": "string"}}, ["js"])
async def tool_browser_eval(args):
    if not await nc.allowed("browser_eval", args["js"]):
        return nc.DENIED
    value = await (await browser()).evaluate(args["js"])
    return json.dumps(value, ensure_ascii=False, indent=1) if not isinstance(value, str) else value


@nc.tool("browser_close", "Close the browser (it opens again on the next browser tool).", {})
async def tool_browser_close(args):
    if BROWSER is None or BROWSER.ws is None:
        return "the browser isn't open"
    await close_browser()
    return "closed the browser"


async def close_browser():
    global BROWSER
    current, BROWSER = BROWSER, None
    if current is not None:
        await current.close()


nc.TOOL_PREVIEW.update({"browser_open": "url", "browser_click": "ref", "browser_type": "ref",
                        "browser_eval": "js", "browser_scroll": "direction"})
nc.TOOL_ICONS.update({name: "◰" for name in ("browser_open", "browser_snapshot", "browser_click", "browser_type",
                                              "browser_scroll", "browser_screenshot", "browser_eval", "browser_close")})


@on("shutdown")
async def shutdown():
    await close_browser()
