import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import gateway  # noqa: E402
from nanokit.gateway import Gateway, Incoming, Platform, split_message  # noqa: E402
from nanokit.gateway import telegram, discord, slack, whatsapp  # noqa: E402
from test_api import fake_openrouter, text_reply  # noqa: E402
from test_nanocode import tool_call_chunk  # noqa: E402


class FakePlatform(Platform):
    name = "telegram"
    limit = 200

    def __init__(self):
        super().__init__({})
        self.sent, self.typed = [], 0

    async def post(self, chat, text, thread=None):
        self.sent.append((chat, text, thread))

    async def typing(self, chat):
        self.typed += 1


def setup_gateway(allowed=("1",), permissions="auto"):
    nc.st.config["gateway"] = {"permissions": permissions, "telegram": {"token": "t", "allowed_users": list(allowed)}}
    nc.save_config()
    nc.st.config = nc.load_config()
    nc.st.headless = nc.st.serving = True
    nc.st.auto_approve = permissions == "auto"
    platform = FakePlatform()
    gw = Gateway([platform])
    platform.gateway = gw
    return gw, platform


async def settle(gw):
    for _ in range(100):
        await asyncio.sleep(0.01)
        workers = [c.worker for c in gw.conversations.values() if c.worker and not c.worker.done()]
        if workers:
            await asyncio.wait(workers)
        else:
            return


def msg(text, user="1", chat="100", **kw):
    return Incoming("telegram", chat, user, text, name="ivan", **kw)


# --- splitting and formatting ---

def test_split_message_keeps_code_blocks_whole():
    text = "intro\n\n```python\n" + "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n```\n\nend"
    parts = split_message(text, 120)
    assert all(len(p) <= 120 for p in parts) and len(parts) > 3
    for p in parts:
        assert p.count("```") % 2 == 0, p     # every piece has its code block closed
    assert "".join(parts).count("line_39") == 1
    assert split_message("short", 100) == ["short"] and split_message("  ", 100) == []
    assert all(len(p) <= 50 for p in split_message("x" * 500, 50))


def test_telegram_markdown_v2():
    out = telegram.to_markdown_v2("# Title\nSome *x* (1+1=2). **bold** `a_b` [link](https://e.com/a)\n"
                                  "```py\nprint('`hi`')\n```")
    assert out.startswith("*Title*")
    assert "\\(1\\+1\\=2\\)\\." in out and "*bold*" in out and "`a_b`" in out
    assert "[link](https://e.com/a)" in out and "print('\\`hi\\`')" in out


def test_slack_and_whatsapp_formatting():
    assert slack.to_mrkdwn("## Hi\n**b** [x](https://a.b) a<b") == "*Hi*\n*b* <https://a.b|x> a&lt;b"
    assert whatsapp.to_whatsapp("**b** [x](https://a.b)\n- item") == "*b* x (https://a.b)\n• item"


# --- the gateway ---

def test_a_conversation_is_a_chat_that_continues(sandbox, monkeypatch):
    requests = fake_openrouter(monkeypatch, [[tool_call_chunk("c1", "glob", {"pattern": "*.md"})],
                                             text_reply("No markdown files."), text_reply("Still none.")])

    async def main():
        gw, platform = setup_gateway()
        await gw.handle(msg("any markdown here?"))
        await settle(gw)
        assert platform.sent[-1] == ("100", "No markdown files.", None)
        session = gw.state["chats"]["telegram:100"]
        assert (nc.sessions_dir() / f"{session}.json").is_file()
        await gw.handle(msg("and now?"))
        await settle(gw)
        assert platform.sent[-1][1] == "Still none."
        assert any(m.get("content") == "No markdown files." for m in requests[-1]["messages"])
        # a gateway started later continues the same chat
        assert json.loads(gateway.state_file().read_text())["chats"]["telegram:100"] == session

    asyncio.run(main())


def test_unknown_people_get_a_code_the_owner_approves(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [text_reply("Hello stranger.")])

    async def main():
        gw, platform = setup_gateway()
        await gw.handle(msg("hi", user="2", chat="200"))
        refusal = platform.sent[-1][1]
        assert "approve" in refusal and platform.sent[-1][0] == "200"
        code = next(iter(gw.state["pending"]))
        assert code in refusal
        await gw.handle(msg("hi again", user="2", chat="200"))
        assert len(platform.sent) == 1         # not told twice in a row
        await gw.handle(msg(f"/approve {code}"))
        assert "Approved" in platform.sent[-1][1]
        assert ("200", "You're approved: I'm listening. Send /help to see what I can do.", None) in platform.sent
        assert "2" in nc.read_config_file()["gateway"]["telegram"]["allowed_users"]
        await gw.handle(msg("hi", user="2", chat="200"))
        await settle(gw)
        assert platform.sent[-1][1] == "Hello stranger."

    asyncio.run(main())


def test_approving_from_the_command_line(sandbox, capsys):
    gw, _ = setup_gateway()                    # running, with a copy of the config from before
    gateway.save_state({"pending": {"ABC234": {"platform": "discord", "user": "9", "name": "bob", "chat": "5",
                                                "time": time.time()},
                                    "OLD234": {"platform": "discord", "user": "8", "name": "eve", "chat": "5",
                                               "time": time.time() - 2 * 3600}}})
    assert gateway.gateway_cli(["approve", "abc234"]) == 0
    assert nc.read_config_file()["gateway"]["discord"]["allowed_users"] == ["9"]
    assert nc.read_config_file()["gateway"]["discord"]["owners"] == ["9"]   # the first one approved
    assert gateway.gateway_cli(["approve", "old234"]) == 1                  # codes expire after an hour
    assert gateway.gateway_cli(["approve", "NOPE"]) == 1
    nc.st.config = {"gateway": {"telegram": {"token": "t", "allowed_users": ["1"]}}}
    assert gw.allowed("discord", "9") and not gw.allowed("discord", "8")


def test_commands_new_status_and_read_only(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [[tool_call_chunk("c1", "write", {"path": "x.txt", "content": "hi"})],
                                  text_reply("I can't write files here.")])

    async def main():
        gw, platform = setup_gateway(permissions="read-only")
        await gw.handle(msg("write x.txt"))
        await settle(gw)
        assert not Path("x.txt").exists() and platform.sent[-1][1] == "I can't write files here."
        first = gw.state["chats"]["telegram:100"]
        before = len(platform.sent)
        await gw.handle(msg("/status@nanobot"))
        status = "\n".join(text for _, text, _ in platform.sent[before:])  # long folder paths split it in parts
        assert "read-only" in status and "write x.txt" in status
        await gw.handle(msg("/new"))
        assert platform.sent[-1][1] == "New chat started." and gw.state["chats"]["telegram:100"] != first
        await gw.handle(msg("/help"))
        assert "/stop" in "".join(text for _, text, _ in platform.sent[-2:])

    asyncio.run(main())


def test_stop_and_the_queue(sandbox, monkeypatch):
    calls = []

    async def slow(request):
        calls.append(json.loads(request.content)["messages"][-1]["content"])
        if "slow" in calls[-1]:
            await asyncio.sleep(30)
        from test_nanocode import sse
        return sse(text_reply(f"done: {calls[-1][-5:]}"))

    real = httpx.AsyncClient
    monkeypatch.setattr(nc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(slow), **kw))
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "z-ai/glm-5.3", "k"

    async def main():
        gw, platform = setup_gateway()
        await gw.handle(msg("be slow"))
        await asyncio.sleep(0.2)
        conv = gw.conversations["telegram:100"]
        assert conv.busy and platform.typed >= 1
        await gw.handle(msg("first"))
        assert "queued" in platform.sent[-1][1] and len(conv.queue) == 1
        await gw.handle(msg("/stop"))
        assert platform.sent[-1][1] == "Stopped." and not conv.queue
        await gw.handle(msg("quick one"))
        await settle(gw)
        assert platform.sent[-1][1] == "done: k one"
        await gw.handle(msg("/stop"))
        assert platform.sent[-1][1] == "Nothing is running."

    asyncio.run(main())


def test_images_and_voice_messages(sandbox, monkeypatch):
    requests = fake_openrouter(monkeypatch, [text_reply("A red dot. You said hello.")])
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
    from nanokit import voice

    async def fake_transcribe(path):
        assert Path(path).read_bytes() == b"OGG"
        return "hello there"
    monkeypatch.setattr(voice, "transcribe", fake_transcribe)

    async def main():
        gw, platform = setup_gateway()
        await gw.handle(msg("", images=[("image/png", png)], audio=(b"OGG", ".ogg")))
        await settle(gw)
        assert platform.sent[-1][1] == "A red dot. You said hello."
        content = requests[0]["messages"][-1]["content"]
        assert any(part.get("type") == "image_url" for part in content)
        assert any("(voice message) hello there" in (part.get("text") or "") for part in content)

    asyncio.run(main())


# --- platforms ---

def mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Recorder:
    def __init__(self):
        self.messages = []

    async def handle(self, message):
        self.messages.append(message)


def test_telegram_polls_reads_photos_and_voice_and_replies(sandbox):
    calls = []
    updates = [{"update_id": 5, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 1, "username": "iv"},
                                            "text": "hello"}},
               {"update_id": 6, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 1},
                                            "photo": [{"file_id": "s", "file_size": 1}, {"file_id": "b", "file_size": 9}],
                                            "caption": "look"}},
               {"update_id": 7, "message": {"chat": {"id": 8, "type": "group"}, "from": {"id": 1}, "text": "not for me"}},
               {"update_id": 8, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 1},
                                            "voice": {"file_id": "v"}}}]

    def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        calls.append((path, body))
        if path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 99, "username": "nanobot"}})
        if path.endswith("/getUpdates"):
            if body.get("offset") is None:
                return httpx.Response(200, json={"ok": True, "result": updates})
            raise httpx.ReadTimeout("no more")
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": f"f/{body['file_id']}"}})
        if path.startswith("/file/"):
            return httpx.Response(200, content=b"DATA-" + path.rsplit("/", 1)[-1].encode())
        if path.endswith("/sendMessage"):
            if body.get("parse_mode"):
                return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False})

    async def main():
        recorder = Recorder()
        bot = telegram.Telegram({"token": "123:SECRET"}, recorder, http=mock(handler))
        task = asyncio.ensure_future(bot.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(recorder.messages) == 3:
                break
        task.cancel()
        texts = {m.text: m for m in recorder.messages}
        assert set(texts) == {"hello", "look", ""}
        assert texts["look"].images == [("image/jpeg", b"DATA-b")]
        assert texts[""].audio == (b"DATA-v", ".ogg") and texts["hello"].chat == "7" and texts["hello"].user == "1"
        await bot.send_text("7", "a *b*")     # MarkdownV2 refused: sent again as plain text
        sends = [b for p, b in calls if p.endswith("/sendMessage")]
        assert sends[0]["parse_mode"] == "MarkdownV2" and sends[1]["text"] == "a *b*" and "parse_mode" not in sends[1]
        assert "SECRET" not in bot.hide("https://api.telegram.org/bot123:SECRET/getMe")

    asyncio.run(main())


class FakeSocket:
    """A websocket that plays back server messages and records what's sent."""

    def __init__(self, incoming, close_code=None):
        self.incoming, self.sent, self.close_code = list(incoming), [], close_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        return json.dumps(self.incoming.pop(0))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.incoming:
            raise StopAsyncIteration
        return json.dumps(self.incoming.pop(0))

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000):
        self.close_code = code


def test_discord_identifies_answers_dms_and_mentions_and_resumes(sandbox):
    posts = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            posts.append(json.loads(request.content))
        return httpx.Response(200, json={"url": "wss://fake"})

    hello = {"op": 10, "d": {"heartbeat_interval": 45000}}
    ready = {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "S", "resume_gateway_url": "wss://resume",
                                                  "user": {"id": "42", "username": "nano"}}}
    dm = {"op": 0, "t": "MESSAGE_CREATE", "s": 2, "d": {"id": "m1", "channel_id": "c1", "content": "hi",
                                                         "author": {"id": "1", "username": "iv"}}}
    guild_no_mention = {"op": 0, "t": "MESSAGE_CREATE", "s": 3, "d": {
        "id": "m2", "channel_id": "c2", "guild_id": "g", "content": "chatter", "author": {"id": "1"}, "mentions": []}}
    mention = {"op": 0, "t": "MESSAGE_CREATE", "s": 4, "d": {
        "id": "m3", "channel_id": "c2", "guild_id": "g", "content": "<@42> run tests", "author": {"id": "1"},
        "mentions": [{"id": "42"}]}}
    sockets = [FakeSocket([hello, ready, dm, guild_no_mention, mention, {"op": 7, "d": None}]),
               FakeSocket([hello, {"op": 0, "t": "RESUMED", "s": 5, "d": {}}], close_code=4014)]
    urls = []

    def connect(url, **kw):
        urls.append(url)
        return sockets[len(urls) - 1]

    async def main():
        recorder = Recorder()
        bot = discord.Discord({"token": "T", "channels": "c2, c3"}, recorder, http=mock(handler), connect=connect)
        try:
            await bot.run()
        except gateway.GatewayError as e:
            assert "MESSAGE CONTENT" in str(e)
        await asyncio.sleep(0.05)
        assert [m.text for m in recorder.messages] == ["hi", "run tests"]
        identify = sockets[0].sent[0]
        assert identify["op"] == 2 and identify["d"]["intents"] & (1 << 15) and identify["d"]["intents"] & (1 << 12)
        assert urls[1].startswith("wss://resume") and sockets[1].sent[0] == {
            "op": 6, "d": {"token": "T", "session_id": "S", "seq": 4}}
        await bot.send_text("c1", "x" * 2500)
        assert len(posts) == 2 and all(len(p["content"]) <= 2000 for p in posts)
        assert "".join(p["content"] for p in posts) == "x" * 2500
        assert posts[0]["allowed_mentions"] == {"parse": []}

    asyncio.run(main())


def test_slack_acks_envelopes_and_answers_in_threads(sandbox):
    posted = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "auth.test":
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT", "user": "nano", "team": "T"})
        if method == "apps.connections.open":
            assert request.headers["authorization"] == "Bearer xapp-1"
            return httpx.Response(200, json={"ok": True, "url": "wss://slack"})
        if method == "chat.postMessage":
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    def event(eid, ev):
        return {"type": "events_api", "envelope_id": f"env-{eid}", "payload": {"event_id": eid, "event": ev}}

    socket = FakeSocket([
        {"type": "hello"},
        event("e1", {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "111.1", "text": "<@UBOT> hello"}),
        event("e1", {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "111.1", "text": "dup"}),
        event("e2", {"type": "message", "channel_type": "im", "user": "U1", "channel": "D1", "text": "dm &amp; more"}),
        event("e3", {"type": "message", "channel_type": "im", "bot_id": "B", "channel": "D1", "text": "a bot"}),
        {"type": "disconnect"}])
    connects = []

    def connect(url, **kw):
        connects.append(url)
        if len(connects) > 1:
            raise gateway.GatewayError("done")
        return socket

    async def main():
        recorder = Recorder()
        bot = slack.Slack({"app_token": "xapp-1", "bot_token": "xoxb-1"}, recorder, http=mock(handler), connect=connect)
        try:
            await bot.run()
        except gateway.GatewayError:
            pass
        await asyncio.sleep(0.05)
        assert [e["envelope_id"] for e in socket.sent] == ["env-e1", "env-e1", "env-e2", "env-e3"]
        assert [(m.text, m.chat, m.thread) for m in recorder.messages] == [("hello", "C1", "111.1"),
                                                                          ("dm & more", "D1", None)]
        assert recorder.messages[0].key == "slack:C1:111.1"
        await bot.send_text("C1", "**done**", "111.1")
        assert posted == [{"channel": "C1", "text": "*done*", "unfurl_links": False, "thread_ts": "111.1"}]

    asyncio.run(main())


def test_whatsapp_webhook_checks_the_signature(sandbox):
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"messages": [{"id": "x"}]})
        if request.url.path.endswith("/MEDIA"):
            return httpx.Response(200, json={"url": "https://cdn.example/m", "mime_type": "image/jpeg"})
        return httpx.Response(200, content=b"JPEGDATA")

    cfg = {"access_token": "EA", "app_secret": "shh", "phone_number_id": "555", "verify_token": "vt"}

    async def main():
        recorder = Recorder()
        bot = whatsapp.WhatsApp(cfg, recorder, http=mock(handler))
        assert await bot.route("GET", "/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=abc", {}, b"") \
            == (200, "abc")
        assert (await bot.route("GET", "/webhook?hub.mode=subscribe&hub.verify_token=no&hub.challenge=abc", {}, b""))[0] == 403
        body = json.dumps({"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": "4477", "profile": {"name": "Ivan"}}],
            "messages": [{"id": "w1", "from": "4477", "type": "text", "text": {"body": "hi"}},
                         {"id": "w2", "from": "4477", "type": "image", "image": {"id": "MEDIA", "caption": "pic"}}]}}]}]}).encode()
        assert (await bot.route("POST", "/webhook", {"x-hub-signature-256": "sha256=bad"}, body))[0] == 401
        sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        assert await bot.route("POST", "/webhook", {"x-hub-signature-256": sig}, body) == (200, "ok")
        await asyncio.sleep(0.05)
        assert [(m.text, m.user, m.name) for m in recorder.messages] == [("hi", "4477", "Ivan"), ("pic", "4477", "Ivan")]
        assert recorder.messages[1].images == [("image/jpeg", b"JPEGDATA")]
        await bot.send_text("4477", "**ok**")
        assert posted == [{"messaging_product": "whatsapp", "to": "4477", "type": "text",
                           "text": {"body": "*ok*", "preview_url": False}}]

    asyncio.run(main())


def test_deliver_sends_without_a_running_gateway(sandbox, monkeypatch):
    sent = []

    def handler(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    nc.st.config["gateway"] = {"telegram": {"token": "1:A"}}
    asyncio.run(gateway.deliver("telegram", "77", "report"))
    assert sent[0][0] == "/bot1:A/sendMessage" and sent[0][1]["chat_id"] == "77"
    nc.st.config["gateway"] = {}
    try:
        asyncio.run(gateway.deliver("slack", "C1", "x"))
        assert False
    except gateway.GatewayError as e:
        assert "setup" in str(e)


# --- the daemon ---

def test_daemon_commands_without_a_running_gateway(sandbox, capsys, monkeypatch):
    assert gateway.gateway_cli(["stop"]) == 0 and "isn't running" in capsys.readouterr().out
    assert gateway.start_daemon() == 1 and "gateway setup" in capsys.readouterr().out
    nc.st.config["gateway"] = {"telegram": {"token": "123:ABCDEFGHIJKLMNOP"}}
    assert gateway.show_status() == 0
    out = capsys.readouterr().out
    assert "not running" in out and "telegram" in out and "ABCDEFGHIJKLMNOP" not in out
    assert gateway.pid_alive(__import__("os").getpid()) and not gateway.pid_alive(0)
    gateway.gateway_dir().mkdir(parents=True)
    gateway.pid_file().write_text("999999999")
    assert gateway.running_pid() is None      # a stale pid file

    started = {}

    class FakeProc:
        pid = 4321

        def poll(self):
            return None

    def popen(argv, **kw):
        started.update(argv=argv, **kw)
        gateway.pid_file().write_text("4321")
        return FakeProc()

    monkeypatch.setattr(gateway.subprocess, "Popen", popen)
    monkeypatch.setattr(gateway, "pid_alive", lambda pid: pid == 4321 and started.get("done", False))
    assert gateway.start_daemon() == 0
    assert started["argv"][-2:] == ["gateway", "run"] and started["cwd"] == str(Path.cwd())
    assert "gateway started" in capsys.readouterr().out


def test_approval_codes_are_capped(sandbox):
    async def main():
        gw, platform = setup_gateway()
        for user in range(30):
            await gw.handle(msg("hi", user=str(100 + user), chat=str(100 + user)))
        assert len(gw.state["pending"]) == gateway.GATEWAY_MAX_PENDING
        for entry in gw.state["pending"].values():
            entry["time"] -= 2 * 3600
        await gw.handle(msg("hi", user="999", chat="999"))
        assert list(gw.state["pending"].values())[0]["user"] == "999" and len(gw.state["pending"]) == 1

    asyncio.run(main())


def test_allowed_reads_the_config_only_when_it_changed(sandbox, monkeypatch):
    gw, _ = setup_gateway()
    reads = []
    real = nc.read_config_file
    monkeypatch.setattr(nc, "read_config_file", lambda *a, **kw: reads.append(1) or real(*a, **kw))
    for _ in range(5):
        assert not gw.allowed("telegram", "2")
    assert reads == []
    config = nc.read_config_file()
    config["gateway"]["telegram"]["allowed_users"].append("2")
    nc.config_file().write_text(json.dumps(config) + "\n")
    assert gw.allowed("telegram", "2")


def test_read_only_chat_cron_needs_an_owner(sandbox, monkeypatch):
    async def main():
        gw, platform = setup_gateway(allowed=("1", "2"), permissions="read-only")
        await gw.handle(msg("/cron add every day | delete everything"))
        assert "read-only" in platform.sent[-1][1]
        from nanokit import cron
        assert cron.load_jobs() == []
        nc.st.config["gateway"]["telegram"]["owners"] = ["1"]
        await gw.handle(msg("/cron add every day | morning report -> discord:9"))
        assert platform.sent[-1][1].startswith("Scheduled")
        job = cron.load_jobs()[0]
        assert job["permissions"] == "read-only" and job["deliver"] == "discord:9"
        await gw.handle(msg(f"/cron remove {job['id']}", user="2"))       # not an owner
        assert "read-only" in platform.sent[-1][1] and cron.load_jobs()
        await gw.handle(msg("/cron list", user="2"))
        assert "morning report" in platform.sent[-1][1]

    asyncio.run(main())


def test_auto_chat_cron_answers_only_here(sandbox):
    async def main():
        gw, platform = setup_gateway(permissions="auto")
        await gw.handle(msg("/cron add every day | x -> discord:9"))
        assert "only answer in this chat" in platform.sent[-1][1]
        await gw.handle(msg("/cron add every day | x"))
        from nanokit import cron
        assert cron.load_jobs()[0]["deliver"] == "telegram:100" and cron.load_jobs()[0]["permissions"] == "auto"

    asyncio.run(main())


def test_strangers_attachments_arent_downloaded(sandbox):
    downloads = []

    def handler(request):
        downloads.append(request.url.path)
        return httpx.Response(200, content=b"DATA")

    class Gate(Recorder):
        def allowed(self, platform, user):
            return user == "1"

    async def main():
        gate = Gate()
        bot = discord.Discord({"token": "T"}, gate, http=mock(handler))
        item = {"url": "https://cdn/x.png", "content_type": "image/png"}
        await bot.receive({"id": "a", "channel_id": "c", "content": "hi", "author": {"id": "2"}, "attachments": [item]})
        assert downloads == [] and gate.messages[-1].images == [] and gate.messages[-1].text == "hi"
        await bot.receive({"id": "b", "channel_id": "c", "content": "", "author": {"id": "1"}, "attachments": [item]})
        assert downloads == ["/x.png"] and gate.messages[-1].images == [("image/png", b"DATA")]
        sl = slack.Slack({"app_token": "a", "bot_token": "b"}, gate, http=mock(handler))
        await sl.receive({"type": "message", "channel_type": "im", "user": "2", "channel": "D", "text": "x",
                          "files": [{"mimetype": "image/png", "url_private": "https://files/y.png"}]})
        assert downloads == ["/x.png"]
        wa = whatsapp.WhatsApp({"access_token": "t"}, gate, http=mock(handler))
        await wa.receive({"id": "w", "from": "2", "type": "image", "image": {"id": "M", "caption": "c"}}, {})
        assert downloads == ["/x.png"] and gate.messages[-1].text == "c"
        tg = telegram.Telegram({"token": "1:A"}, gate, http=mock(handler))
        tg.me = {"id": 5, "username": "bot"}
        await tg.receive({"chat": {"id": 7, "type": "private"}, "from": {"id": 2}, "photo": [{"file_id": "p"}],
                          "caption": "look"})
        assert downloads == ["/x.png"] and gate.messages[-1].images == []

    asyncio.run(main())


def test_downloads_are_capped(sandbox):
    def handler(request):
        if request.url.path == "/says-big":
            return httpx.Response(200, headers={"content-length": str(10 ** 9)}, content=b"x")
        return httpx.Response(200, content=b"x" * 5000)

    async def main():
        bot = Platform({}, http=mock(handler))
        for path in ("/says-big", "/is-big"):
            try:
                await bot.download("https://h" + path, limit=1000)
                assert False
            except gateway.GatewayError as e:
                assert "too big" in str(e)
        assert len(await bot.download("https://h/is-big", limit=10000)) == 5000

    asyncio.run(main())


def test_rate_limits_are_waited_out(sandbox, monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    hits = {"telegram": 0, "slack": 0}

    def handler(request):
        if "slack" in request.url.host:
            hits["slack"] += 1
            if hits["slack"] < 3:
                return httpx.Response(429, headers={"retry-after": "2"})
            return httpx.Response(200, json={"ok": True})
        hits["telegram"] += 1
        if hits["telegram"] < 2:
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                             "parameters": {"retry_after": 3}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    async def main():
        await telegram.Telegram({"token": "1:A"}, http=mock(handler)).send_text("7", "a\n\n" * 3000)
        await slack.Slack({"app_token": "a", "bot_token": "b"}, http=mock(handler)).send_text("C", "hi")

    asyncio.run(main())
    assert waits == [3.0, 2.0, 2.0] and hits["slack"] == 3 and hits["telegram"] > 2

    def always(request):
        return httpx.Response(429, headers={"retry-after": "1"})

    try:
        asyncio.run(slack.Slack({"app_token": "a", "bot_token": "b"}, http=mock(always)).api("chat.postMessage"))
        assert False
    except slack.SlackError as e:
        assert "ratelimited" in str(e)


def test_telegram_keeps_polling_after_an_unexpected_error(sandbox, monkeypatch):
    polls = []

    def handler(request):
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "b"}})
        polls.append(1)
        if len(polls) == 1:
            return httpx.Response(200, json={"ok": True, "result": [{"no_update_id": 1}]})   # a KeyError
        if len(polls) > 2:
            raise httpx.ReadTimeout("no more")
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 3, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 1}, "text": "hi"}}]})

    async def main():
        recorder = Recorder()
        bot = telegram.Telegram({"token": "1:A"}, recorder, http=mock(handler))
        task = asyncio.ensure_future(bot.run())
        for _ in range(300):
            await asyncio.sleep(0.01)
            if recorder.messages:
                break
        task.cancel()
        assert recorder.messages and recorder.messages[0].text == "hi"

    asyncio.run(main())


def test_a_stale_pid_is_never_stopped(sandbox, monkeypatch):
    killed = []
    class FakeOs:                              # gateway's os, with a kill that only records
        def __getattr__(self, name):
            return getattr(os, name)

        def kill(self, pid, sig):
            killed.append(pid)
    monkeypatch.setattr(gateway, "os", FakeOs())
    monkeypatch.setattr(gateway, "pid_alive", lambda pid: True)       # the pid now belongs to another program
    monkeypatch.setattr(gateway, "looks_like_gateway", lambda pid: False)
    gateway.gateway_dir().mkdir(parents=True)
    gateway.pid_file().write_text("4242")
    assert gateway.running_pid() is None
    assert gateway.stop_daemon() == 0 and killed == []
    # a running gateway holds gateway.lock: then it's ours
    import subprocess
    code = ("import fcntl, sys, time; f = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('ok', flush=True); time.sleep(30)") if os.name != "nt" else \
        ("import msvcrt, sys, time; f = open(sys.argv[1], 'a+'); f.seek(0); msvcrt.locking(f.fileno(), "
         "msvcrt.LK_NBLCK, 1); print('ok', flush=True); time.sleep(30)")
    holder = subprocess.Popen([sys.executable, "-c", code, str(gateway.lock_file())], stdout=subprocess.PIPE,
                              text=True)
    try:
        assert holder.stdout.readline().strip() == "ok"
        assert gateway.running_pid() == 4242
        assert not gateway.hold_lock()
    finally:
        holder.kill()
        holder.wait()
    assert gateway.running_pid() is None and gateway.hold_lock()
    try:
        assert gateway.lock_held()
    finally:
        gateway.drop_lock()
