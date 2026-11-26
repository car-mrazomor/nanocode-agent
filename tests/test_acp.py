import asyncio
import io
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import acp  # noqa: E402
from test_api import fake_openrouter, text_reply  # noqa: E402
from test_nanocode import tool_call_chunk  # noqa: E402


class Editor:
    """A fake ACP client: sends messages to the server, collects what comes back, answers permission questions."""

    def __init__(self, permission="allow"):
        self.sent, self.permission = [], permission
        self.server = acp.AcpServer(self.receive)
        self.next_id = 0
        self.answers = {}

    def receive(self, message):
        message = json.loads(json.dumps(message))     # must be plain JSON
        self.sent.append(message)
        if message.get("method") == "session/request_permission":
            outcome = {"outcome": "selected", "optionId": self.permission} if self.permission else {"outcome": "cancelled"}
            asyncio.get_running_loop().call_soon(self.server.receive, {"jsonrpc": "2.0", "id": message["id"],
                                                                       "result": {"outcome": outcome}})
        elif "id" in message and "method" not in message:
            self.answers[message["id"]] = message

    async def call(self, method, params=None):
        self.next_id += 1
        mid = self.next_id
        self.server.receive({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        while mid not in self.answers:
            await asyncio.sleep(0.005)
        answer = self.answers[mid]
        if "error" in answer:
            raise RuntimeError(answer["error"]["message"])
        return answer["result"]

    def updates(self, kind=None):
        return [m["params"]["update"] for m in self.sent if m.get("method") == "session/update"
                and (kind is None or m["params"]["update"]["sessionUpdate"] == kind)]


def start(monkeypatch, replies, permission="allow"):
    requests = fake_openrouter(monkeypatch, replies)
    nc.st.headless = nc.st.serving = True
    editor = Editor(permission)
    monkeypatch.setattr(acp, "SERVER", editor.server)
    return editor, requests


def test_prompt_streams_text_tools_and_plan(sandbox, monkeypatch):
    Path("notes.txt").write_text("the notes\n")
    editor, requests = start(monkeypatch, [
        [tool_call_chunk("c1", "todo", {"items": [{"text": "read", "status": "in_progress"}]})],
        [tool_call_chunk("c2", "read", {"path": "notes.txt"})],
        text_reply("All read.")])

    async def main():
        init = await editor.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        assert init["protocolVersion"] == 1 and init["agentCapabilities"]["loadSession"]
        sid = (await editor.call("session/new", {"cwd": str(sandbox), "mcpServers": []}))["sessionId"]
        uri = Path(sandbox / "notes.txt").as_uri()
        result = await editor.call("session/prompt", {"sessionId": sid, "prompt": [
            {"type": "text", "text": "summarize"}, {"type": "resource_link", "uri": uri, "name": "notes.txt"}]})
        return sid, result
    sid, result = asyncio.run(main())
    assert result == {"stopReason": "end_turn"}
    first = requests[0]["messages"][-1]["content"]
    assert first.startswith("summarize @notes.txt") and "the notes" in first      # the link became an @mention
    assert "".join(u["content"]["text"] for u in editor.updates("agent_message_chunk")) == "All read."
    calls = editor.updates("tool_call")
    assert [c["kind"] for c in calls] == ["think", "read"] and calls[1]["locations"][0]["path"].endswith("notes.txt")
    done = editor.updates("tool_call_update")
    assert [d["status"] for d in done] == ["completed", "completed"] and "the notes" in done[1]["content"][0]["content"]["text"]
    assert editor.updates("plan")[0]["entries"] == [{"content": "read", "priority": "medium", "status": "in_progress"}]
    assert (nc.sessions_dir() / f"{sid}.json").exists()


def test_permission_goes_to_the_editor(sandbox, monkeypatch):
    real_client = nc.httpx.AsyncClient
    for answer, exists in (("allow", True), ("deny", False), (None, False)):
        monkeypatch.setattr(nc.httpx, "AsyncClient", real_client)
        nc.st.auto_approve = False
        nc.st.always_allow.clear()
        if Path("out.txt").exists():
            Path("out.txt").unlink()
        editor, _ = start(monkeypatch, [[tool_call_chunk("c1", "write", {"path": "out.txt", "content": "hi"})],
                                        text_reply("ok")], permission=answer)

        async def main():
            sid = (await editor.call("session/new", {"cwd": str(sandbox)}))["sessionId"]
            return await editor.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
        asyncio.run(main())
        asked = [m for m in editor.sent if m.get("method") == "session/request_permission"]
        assert len(asked) == 1 and asked[0]["params"]["toolCall"]["toolCallId"].startswith("call_")
        assert Path("out.txt").exists() == exists


def test_dangerous_commands_are_denied_without_asking(sandbox, monkeypatch):
    nc.st.auto_approve = True
    editor, _ = start(monkeypatch, [[tool_call_chunk("c1", "bash", {"command": "rm -rf /"})], text_reply("ok")])

    async def main():
        sid = (await editor.call("session/new", {"cwd": str(sandbox)}))["sessionId"]
        await editor.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
    asyncio.run(main())
    assert not [m for m in editor.sent if m.get("method") == "session/request_permission"]
    assert editor.updates("tool_call_update")[0]["status"] == "failed"


def test_load_replays_the_chat(sandbox, monkeypatch):
    editor, _ = start(monkeypatch, [text_reply("Hello there.")])

    async def main():
        sid = (await editor.call("session/new", {"cwd": str(sandbox)}))["sessionId"]
        await editor.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]})
        other = Editor()
        monkeypatch.setattr(acp, "SERVER", other.server)
        assert await other.call("session/load", {"sessionId": sid, "cwd": str(sandbox), "mcpServers": []}) is None
        try:
            await other.call("session/load", {"sessionId": "nope", "cwd": str(sandbox)})
        except RuntimeError as e:
            assert "no saved chat" in str(e)
        else:
            raise AssertionError("loading a missing chat should fail")
        return other
    other = asyncio.run(main())
    assert other.updates("user_message_chunk")[0]["content"]["text"] == "hi"
    assert other.updates("agent_message_chunk")[0]["content"]["text"] == "Hello there."


def test_cancel_stops_the_turn(sandbox, monkeypatch):
    editor, _ = start(monkeypatch, [text_reply("x")])
    started = asyncio.Event()

    async def slow_send(text, display=None, images=None):
        started.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(nc, "send", slow_send)

    async def main():
        sid = (await editor.call("session/new", {"cwd": str(sandbox)}))["sessionId"]
        prompt = asyncio.ensure_future(editor.call("session/prompt", {"sessionId": sid, "prompt": [
            {"type": "text", "text": "go"}]}))
        await started.wait()
        editor.server.receive({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
        return await asyncio.wait_for(prompt, 5)
    assert asyncio.run(main()) == {"stopReason": "cancelled"}


def test_overlapping_requests_keep_their_own_ids():
    sent = []
    server = acp.AcpServer(sent.append)

    async def main():
        first = asyncio.ensure_future(server.request("a", {}))
        second = asyncio.ensure_future(server.request("b", {}))
        await asyncio.sleep(0)
        server.receive({"jsonrpc": "2.0", "id": 1, "result": "one"})
        assert await first == "one"
        assert 2 in server.waiting                 # the first one's cleanup didn't drop the second's
        server.receive({"jsonrpc": "2.0", "id": 2, "result": "two"})
        return await second
    assert asyncio.run(main()) == "two"


def test_other_folder_waits_while_a_turn_runs(sandbox, monkeypatch, tmp_path):
    editor, _ = start(monkeypatch, [text_reply("x")])
    other = tmp_path / "other"
    other.mkdir()
    started, release = asyncio.Event(), None

    async def slow_send(text, display=None, images=None):
        started.set()
        await release.wait()
    monkeypatch.setattr(nc, "send", slow_send)

    async def main():
        nonlocal release
        release = asyncio.Event()
        a = (await editor.call("session/new", {"cwd": str(sandbox)}))["sessionId"]
        b = (await editor.call("session/new", {"cwd": str(other)}))["sessionId"]
        await editor.call("session/new", {"cwd": str(sandbox)})
        prompt = asyncio.ensure_future(editor.call("session/prompt", {"sessionId": a, "prompt": [
            {"type": "text", "text": "go"}]}))
        await started.wait()
        try:
            await editor.call("session/prompt", {"sessionId": b, "prompt": [{"type": "text", "text": "go"}]})
            error = None
        except RuntimeError as e:
            error = str(e)
        here = Path.cwd().resolve()
        release.set()
        await asyncio.wait_for(prompt, 5)
        return error, here
    error, here = asyncio.run(main())
    assert error and "one folder at a time" in error
    assert here == sandbox.resolve()                    # the running turn's folder didn't change under it


def test_unknown_methods_and_prompt_blocks(sandbox, monkeypatch):
    editor, _ = start(monkeypatch, [text_reply("x")])

    async def main():
        try:
            await editor.call("nope/nothing")
        except RuntimeError as e:
            return str(e)
    assert "method not found" in asyncio.run(main())
    text, images = acp.prompt_text([{"type": "text", "text": "look at"},
                                    {"type": "resource", "resource": {"uri": "file:///x/y.py", "text": "print(1)"}},
                                    {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}])
    assert text.startswith("look at") and '<file path="/x/y.py">' in text and "print(1)" in text and len(images) == 1


def test_stdio_loop(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [text_reply("x")])
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "rb")
    stdout = io.BytesIO()

    def feed():
        with os.fdopen(write_fd, "wb") as w:
            w.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n')
            w.write(b"not json\n")
    threading.Thread(target=feed).start()
    assert asyncio.run(acp.serve(stdin, stdout)) == 0
    lines = [json.loads(line) for line in stdout.getvalue().decode().splitlines()]
    assert {line.get("id") for line in lines} == {1, None}
    assert acp.SERVER is None
