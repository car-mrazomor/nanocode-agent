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
    page = web.PAGE.read_text(encoding="utf-8")
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


# --- The page's workspace: commands, search, fork, export ---

def run_events(api, body):
    return [e for e in api.runs[json.loads(body)["run_id"]].events]


def test_commands_run_and_their_output_streams(sandbox):
    nc.st.auto_approve = True

    async def test(port, api):
        status, _, body = await request(port, "POST", "/v1/runs", {"input": "/think high", "command": True})
        assert status == 202
        run = api.runs[json.loads(body)["run_id"]]
        await run.task
        text = "".join(e["text"] for e in run.events if e["event"] == "output")
        assert run.status == "completed" and "Thinking: high" in text and nc.st.config["think"] == "high"
        status, _, body = await request(port, "POST", "/v1/runs", {"input": "/memory", "command": True})
        await api.runs[json.loads(body)["run_id"]].task
        assert any(e["event"] == "output" for e in run_events(api, body))
    with_server(test)


def test_listing_commands_dont_wait_for_a_running_reply(sandbox):
    import asyncio

    async def test(port, api):
        async with api.lock:                       # a reply is running
            status, _, body = await request(port, "POST", "/v1/runs", {"input": "/help", "command": True})
            run = api.runs[json.loads(body)["run_id"]]
            await asyncio.wait_for(run.task, 5)
            assert run.status == "completed" and "Commands" in run.text + "".join(
                e.get("text", "") for e in run.events if e["event"] == "output")
    with_server(test)


def test_commands_that_need_a_terminal_or_change_things_are_refused(sandbox):
    async def test(port, api):
        async def status_of(text):
            status, _, body = await request(port, "POST", "/v1/runs", {"input": text, "command": True})
            if status == 202:
                await api.runs[json.loads(body)["run_id"]].task
            return status
        nc.st.auto_approve = True
        assert await status_of("/tab new") == 400 and await status_of("/provider ollama") == 400
        assert await status_of("/auto on") == 400 and await status_of("/editor") == 400
        assert await status_of("/permissions") == 202
        nc.st.auto_approve = False                 # a read-only API
        assert await status_of("/permissions allow bash") == 400
        assert await status_of("/plan /permissions allow bash") == 400      # not even after switching modes
        assert await status_of("# remember this") == 400 and await status_of("/undo") == 400
        assert await status_of("/think low") == 202 and await status_of("/rollback") == 202
        assert await status_of("/cron list") == 202 and await status_of("/cron add every day | hi") == 400
    with_server(test)


def test_undo_from_the_page_counts_as_asked_but_the_model_never_gets_that_yes(sandbox, monkeypatch):
    import nanokit
    from nanokit import snapshots
    seen = []

    async def fake_undo(arg):
        seen.append(("command", await snapshots._confirm("undo", "x")))
        await nc.send("go on")

    async def fake_send(text, display=None, images=None):
        seen.append(("turn", await snapshots._confirm("rollback", "x")))
    monkeypatch.setitem(nanokit.COMMANDS, "/undo", {**nanokit.COMMANDS["/undo"], "fn": fake_undo})
    monkeypatch.setattr(nc, "_send", fake_send)
    nc.st.auto_approve = True

    async def test(port, api):
        for confirmed in (False, True):
            status, _, body = await request(port, "POST", "/v1/runs", {"input": "/undo", "command": True,
                                                                       "confirmed": confirmed})
            await api.runs[json.loads(body)["run_id"]].task
        assert seen == [("command", False), ("turn", False), ("command", True), ("turn", False)]
    with_server(test)


def test_workspace_has_models_mode_themes_and_the_commands_the_page_may_run(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [text_reply("hi")])
    nc.st.auto_approve = True

    async def test(port, api):
        status, _, body = await request(port, "GET", "/v1/workspace")
        ws = json.loads(body)
        names = {c["name"] for c in ws["commands"]}
        assert status == 200 and ws["model"] == "z-ai/glm-5.3" and ws["mode"] == "build"
        assert ws["themes"]["opencode"] == nc.THEMES["opencode"] and "tokyonight" in ws["themes"]
        assert {"/memory", "/undo", "/cron", "/stats"} <= names and not names & {"/tab", "/q", "/provider", "/auto"}
        assert ws["think"] in ws["think_levels"] and ws["models"]
    with_server(test)


def test_mode_comes_with_the_message(sandbox, monkeypatch):
    from nanokit import modes
    fake_openrouter(monkeypatch, [text_reply("A plan.")])

    async def test(port, api):
        status, _, body = await request(port, "POST", "/v1/runs", {"input": "plan it", "mode": "plan"})
        await api.runs[json.loads(body)["run_id"]].task
        assert modes.current_mode() == "plan"
        status, _, _ = await request(port, "POST", "/v1/runs", {"input": "x", "mode": "yolo"})
        assert status == 400
    with_server(test)


def test_search_fork_and_export_a_chat(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [text_reply("Zebras are striped.")])

    async def test(port, api):
        status, _, body = await request(port, "POST", "/v1/runs", {"input": "tell me about zebras"})
        run = api.runs[json.loads(body)["run_id"]]
        await run.task
        status, _, body = await request(port, "GET", "/v1/search?q=zebras")
        hits = json.loads(body)["data"]
        assert status == 200 and hits and hits[0]["id"] == run.session_id
        status, _, body = await request(port, "GET", "/v1/search?q=")
        assert json.loads(body)["data"] == []
        status, _, body = await request(port, "POST", f"/v1/sessions/{run.session_id}/fork", {})
        fork = json.loads(body)
        assert status == 200 and fork["id"] != run.session_id and fork["title"].startswith("fork of")
        status, _, body = await request(port, "GET", f"/v1/sessions/{fork['id']}")
        assert [t["role"] for t in json.loads(body)["turns"]] == ["user", "assistant"]
        status, _, body = await request(port, "GET", f"/v1/sessions/{run.session_id}/export?format=md")
        export = json.loads(body)
        assert status == 200 and export["filename"].endswith(".md") and "Zebras are striped." in export["content"]
        status, _, body = await request(port, "GET", f"/v1/sessions/{run.session_id}/export?format=json")
        assert json.loads(json.loads(body)["content"])["id"] == run.session_id
        status, _, _ = await request(port, "GET", f"/v1/sessions/{run.session_id}/export?format=pdf")
        assert status == 400
        status, _, _ = await request(port, "POST", "/v1/sessions/nope/fork", {})
        assert status == 404
    with_server(test)


def test_a_forked_claude_chat_sends_the_earlier_screen_with_its_first_message(sandbox):
    nc.st.backend = "sdk"
    nc.sessions_dir().mkdir(parents=True, exist_ok=True)
    (nc.sessions_dir() / "orig.json").write_text(json.dumps({"id": "orig", "title": "t", "backend": "sdk",
                                                              "messages": [], "screen": "> hi\nearlier answer"}))
    notes = {}
    fork = server.api_fork("orig", notes)
    assert notes == {fork["id"]: "> hi\nearlier answer"}
