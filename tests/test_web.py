import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from api import server  # noqa: E402
from nanokit import web  # noqa: E402
from test_api import fake_openrouter, request, text_reply, with_server  # noqa: E402
from test_nanocode import tool_call_chunk  # noqa: E402


def test_page_and_pwa_files_need_no_key(sandbox):
    async def test(port, api):
        status, headers, body = await request(port, "GET", "/", key=None)
        assert status == 200 and headers["content-type"].startswith("text/html")
        assert "content-security-policy" in headers
        assert "/v1/runs" in body and "localStorage" in body and "manifest.webmanifest" in body
        assert "http://" not in body.replace("http://www.w3.org", "") and "https://cdn" not in body   # no CDN
        status, headers, body = await request(port, "GET", "/manifest.webmanifest", key=None)
        assert status == 200 and json.loads(body)["display"] == "standalone"
        status, _, body = await request(port, "GET", "/icon.svg", key=None)
        assert status == 200 and body.startswith("<svg")
        status, _, body = await request(port, "GET", "/sw.js", key=None)
        assert status == 200 and "fetch" in body
        status, _, _ = await request(port, "GET", "/v1/sessions", key=None)
        assert status == 401                                   # the data still needs the key
    with_server(test)


def test_page_uses_the_theme_colors():
    page = web.PAGE.read_text()
    for name in ("bg", "panel", "text", "accent", "accent2", "error", "success"):
        assert nc.THEMES["opencode"][name] in page


def test_sessions_endpoints_show_a_chat(sandbox, monkeypatch):
    Path("a.txt").write_text("hello\n")
    fake_openrouter(monkeypatch, [[tool_call_chunk("c1", "read", {"path": "a.txt"})], text_reply("It says hello.")])

    async def test(port, api):
        status, _, body = await request(port, "POST", "/v1/runs", {"input": "read a.txt"})
        run = api.runs[json.loads(body)["run_id"]]
        await run.task
        status, _, body = await request(port, "GET", "/v1/sessions")
        sessions = json.loads(body)["data"]
        assert status == 200 and sessions[0]["id"] == run.session_id and sessions[0]["title"] == "read a.txt"
        status, _, body = await request(port, "GET", f"/v1/sessions/{run.session_id}")
        turns = json.loads(body)["turns"]
        assert [t["role"] for t in turns] == ["user", "tool", "tool_result", "assistant"]
        assert turns[1]["name"] == "read" and "hello" in turns[2]["text"] and turns[3]["text"] == "It says hello."
        status, _, _ = await request(port, "GET", "/v1/sessions/nope")
        assert status == 404
        status, _, _ = await request(port, "GET", "/v1/sessions/..%2f..")
        assert status in (400, 404)
    with_server(test)


def test_session_list_is_read_off_the_event_loop(sandbox, monkeypatch):
    import threading
    threads = []
    real = nc.list_sessions
    monkeypatch.setattr(nc, "list_sessions", lambda *a: threads.append(threading.get_ident()) or real(*a))

    async def test(port, api):
        status, _, body = await request(port, "GET", "/v1/sessions")
        assert status == 200 and json.loads(body)["data"] == []
        assert threads and threads[0] != threading.get_ident()
    with_server(test)


def test_subscription_chats_show_their_screen():
    turns = web.session_turns({"messages": [], "screen": nc.user_block("hi") + "\nHello!"})
    assert turns == [{"role": "screen", "text": "> hi\nHello!"}]


def test_web_command_starts_the_server_and_opens_the_browser(sandbox, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    nc.st.config = {"provider": "openrouter"}
    opened, served = [], []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))

    async def fake_serve(module, host=None, port=None):
        import asyncio
        await asyncio.sleep(1.2)
        served.append((host, port))
        return 0
    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setattr(nc, "load_config", lambda: {"provider": "openrouter"})
    assert nc.main(["web", "--port", "9123"]) == 0
    key = nc.st.config["api"]["key"]
    assert served == [("127.0.0.1", 9123)] and opened == [f"http://127.0.0.1:9123/#key={key}"]
