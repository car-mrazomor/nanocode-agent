import asyncio
import base64
import json

import httpx
import pytest

import nanocode as nc
import nanokit
from nanokit import browser as br
from test_nanocode import call, mock_http, run

PAGE = {"title": "Example", "url": "https://example.com/", "text": "Hello [1] link \"More\"", "refs": 1}


class FakeWS:
    """A CDP page websocket: answers each command from `answers` (method -> result, or fn(params) -> result)."""

    def __init__(self, answers=None):
        self.sent, self.inbox, self.closed = [], asyncio.Queue(), False
        self.answers = answers or {}

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        await self.inbox.put(json.dumps({"method": "Page.loadEventFired", "params": {}}))   # an event, skipped
        answer = self.answers.get(msg["method"], {})
        if callable(answer):
            answer = answer(msg["params"])
        if isinstance(answer, Exception):
            await self.inbox.put(answer)
        elif isinstance(answer, dict) and "error" in answer:
            await self.inbox.put(json.dumps({"id": msg["id"], **answer}))
        else:
            await self.inbox.put(json.dumps({"id": msg["id"], "result": answer}))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True

    def methods(self):
        return [m["method"] for m in self.sent]


def evaluate(params):
    expr = params["expression"]
    if expr == "document.readyState":
        value = "complete"
    elif "data-nc-ref" in expr and "JSON.stringify" in expr:
        value = json.dumps(PAGE)
    elif "scrollIntoView" in expr and "e.focus()" not in expr:
        value = [50, 60, 100]
    elif "scrollBy" in expr:
        value = [900, 4000]
    elif "e.focus()" in expr:
        value = True
    else:
        value = {"answer": 42}
    return {"result": {"value": value}}


class FakeProc:
    pid = 999999
    returncode = None
    stdin = None


@pytest.fixture
def cdp(monkeypatch, tmp_path):
    """A fake browser: find_browser, the launched process, its /json/list and the page websocket."""
    ws = FakeWS({"Runtime.evaluate": evaluate})
    launched, reaped = [], []

    async def fake_exec(*args, **kw):
        launched.append(args)
        return FakeProc()

    async def fake_reap(proc, grace=3):
        reaped.append(proc)

    async def fake_connect(url):
        ws.url = url
        return ws

    def handler(request):
        assert request.url.path == "/json/list"
        return httpx.Response(200, json=[{"type": "service_worker"},
                                         {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/A"}])

    monkeypatch.setattr(nc, "find_browser", lambda: str(tmp_path / "chrome"))
    monkeypatch.setattr(br.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(nc, "reap", fake_reap)
    monkeypatch.setattr(br, "connect", fake_connect)
    mock_http(monkeypatch, handler)
    monkeypatch.setattr(br, "BROWSER", None)
    ws.launched, ws.reaped = launched, reaped
    return ws


def test_open_launches_headless_and_snapshots(cdp):
    text, is_error = call("browser_open", url="example.com")
    assert not is_error, text
    assert "Example" in text and "[1] link" in text and "1 interactive" in text
    args = cdp.launched[0]
    assert "--headless=new" in args and any(a.startswith("--remote-debugging-port=") for a in args)
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert cdp.url == "ws://127.0.0.1:1/devtools/page/A"
    nav = next(m for m in cdp.sent if m["method"] == "Page.navigate")
    assert nav["params"]["url"] == "https://example.com"


def test_open_asks_permission(cdp):
    nc.st.auto_approve, nc.st.headless = False, True
    text, _ = call("browser_open", url="https://example.com")
    assert text == nc.DENIED and not cdp.launched


def test_open_only_http_and_https(cdp):
    for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "chrome://settings",
                "view-source:https://x.test"):
        text, is_error = call("browser_open", url=url)
        assert is_error and "only opens http and https" in text, url
    assert not cdp.launched
    assert br.web_url("localhost:3000/x") == "http://localhost:3000/x"
    assert br.web_url("HTTP://Example.com") == "HTTP://Example.com"


def test_open_has_its_own_permission_name(cdp, monkeypatch):
    asked = []

    async def allowed(tool, preview):
        asked.append(tool)
        return True
    monkeypatch.setattr(nc, "allowed", allowed)
    call("browser_open", url="https://example.com")
    assert asked == ["browser"]


def test_click_and_type_ask_in_the_users_own_browser(cdp):
    nc.st.config["browser_cdp_url"] = "http://127.0.0.1:9222"
    nc.st.auto_approve, nc.st.headless = False, True
    assert call("browser_click", ref="1")[0] == nc.DENIED
    assert call("browser_type", ref="2", text="hi", submit=True)[0] == nc.DENIED
    assert not any(m["method"] in ("Input.dispatchMouseEvent", "Input.insertText") for m in cdp.sent)
    nc.st.auto_approve = True
    assert not call("browser_click", ref="1")[1]


def test_click_and_type_dont_ask_in_the_throwaway_browser(cdp):
    nc.st.auto_approve, nc.st.headless = False, True
    assert not call("browser_click", ref="1")[1] and not call("browser_type", ref="2", text="hi")[1]


def test_headless_off_and_reuse(cdp):
    nc.st.config["browser_headless"] = False

    async def two():
        await nc.run_tool("browser_open", {"url": "https://a.test"})
        await nc.run_tool("browser_snapshot", {})
    run(two())
    assert len(cdp.launched) == 1 and "--headless=new" not in cdp.launched[0]


def test_click_by_ref_and_selector(cdp):
    async def go():
        a = await nc.run_tool("browser_click", {"ref": "1"})
        cdp.answers["Runtime.evaluate"] = lambda p: (
            {"result": {"value": [0, 0, 0]}} if "scrollIntoView" in p["expression"] else evaluate(p))
        b = await nc.run_tool("browser_click", {"ref": "#go"})
        return a, b
    (a, err_a), (b, err_b) = run(go())
    assert not err_a and not err_b and a.startswith("clicked 1"), b
    mouse = [m["params"]["type"] for m in cdp.sent if m["method"] == "Input.dispatchMouseEvent"]
    assert mouse == ["mouseMoved", "mousePressed", "mouseReleased"]
    exprs = [m["params"]["expression"] for m in cdp.sent if m["method"] == "Runtime.evaluate"]
    assert any('[data-nc-ref=\\"1\\"]' in e or '[data-nc-ref="1"]' in e for e in exprs)
    assert any('document.querySelector("#go").click()' in e for e in exprs)


def test_click_missing_element(cdp):
    cdp.answers["Runtime.evaluate"] = lambda p: (
        {"result": {"value": None}} if "scrollIntoView" in p["expression"] else evaluate(p))
    text, is_error = call("browser_click", ref="7")
    assert is_error and "browser_snapshot" in text


def test_type_and_submit(cdp):
    text, is_error = call("browser_type", ref="2", text="hello", submit=True)
    assert not is_error and "pressed Enter" in text
    assert next(m for m in cdp.sent if m["method"] == "Input.insertText")["params"] == {"text": "hello"}
    keys = [m for m in cdp.sent if m["method"] == "Input.dispatchKeyEvent"]
    assert [k["params"]["type"] for k in keys] == ["keyDown", "keyUp"] and keys[0]["params"]["key"] == "Enter"


def test_scroll(cdp):
    text, is_error = call("browser_scroll", direction="up", pages=2)
    assert not is_error and "scrolled to 900 of 4000" in text
    assert any("* -2.0" in m["params"]["expression"] for m in cdp.sent if m["method"] == "Runtime.evaluate")


def test_screenshot_saved_in_images(cdp, sandbox):
    png = b"\x89PNG\r\n\x1a\nfake"
    cdp.answers["Page.captureScreenshot"] = {"data": base64.b64encode(png).decode()}
    text, is_error = call("browser_screenshot", full_page=True)
    assert not is_error, text
    saved = list((sandbox / ".nanocode" / "images").glob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == png and str(saved[0]) in text


def test_eval_asks_and_returns_json(cdp):
    text, is_error = call("browser_eval", js="({answer: 42})")
    assert not is_error and json.loads(text) == {"answer": 42}
    nc.st.auto_approve, nc.st.headless = False, True
    assert call("browser_eval", js="1")[0] == nc.DENIED


def test_js_exception_is_an_error(cdp):
    cdp.answers["Runtime.evaluate"] = {"result": {}, "exceptionDetails": {"text": "Uncaught",
                                                                          "exception": {"description": "boom"}}}
    text, is_error = call("browser_eval", js="throw 1")
    assert is_error and "boom" in text


def test_cdp_url_connects_without_launching(cdp):
    nc.st.config["browser_cdp_url"] = "http://127.0.0.1:9222"
    _, is_error = call("browser_snapshot")
    assert not is_error and not cdp.launched and cdp.url.endswith("/page/A")


def test_lost_connection_resets(cdp):
    cdp.answers["Page.captureScreenshot"] = ConnectionError("gone")
    text, is_error = call("browser_screenshot")
    assert is_error and "lost the browser" in text
    assert cdp.closed and cdp.reaped and br.BROWSER.ws is None


def test_close_and_shutdown_clean_up(cdp):
    async def go():
        await nc.run_tool("browser_snapshot", {})
        profile = br.BROWSER.profile
        await nanokit.emit("shutdown")
        return profile
    profile = run(go())
    assert cdp.closed and len(cdp.reaped) == 1 and br.BROWSER is None
    assert not __import__("os").path.exists(profile)
    assert call("browser_close")[0] == "the browser isn't open"


def test_no_browser_found(cdp, monkeypatch):
    monkeypatch.setattr(nc, "find_browser", lambda: None)
    text, is_error = call("browser_open", url="https://x.test")
    assert is_error and "browser_cdp_url" in text


def test_element_js():
    assert br.element_js("[3]") == 'document.querySelector("[data-nc-ref=\\"3\\"]")'
    assert br.element_js("a.b") == 'document.querySelector("a.b")'
