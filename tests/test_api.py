import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from api import server  # noqa: E402
from test_nanocode import mock_http, sse, tool_call_chunk  # noqa: E402

KEY = "test-key-123456"


def fake_openrouter(monkeypatch, replies):
    """Each model call gets the next reply: a list of stream chunks."""
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return sse(replies[min(len(requests), len(replies)) - 1])

    mock_http(monkeypatch, handler)
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "z-ai/glm-5.3", "k"
    return requests


def text_reply(text, tokens=(10, 2)):
    return [{"choices": [{"delta": {"content": text}}]},
            {"choices": [], "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "cost": 0.001}}]


async def request(port, method, path, body=None, key=KEY, headers=None):
    """Raw HTTP, so the test client isn't affected by the mocked httpx."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = json.dumps(body).encode() if body is not None else b""
    head = [f"{method} {path} HTTP/1.1", "Host: localhost", f"Content-Length: {len(data)}",
            "Content-Type: application/json"]
    if key:
        head.append(f"Authorization: Bearer {key}")
    head += [f"{k}: {v}" for k, v in (headers or {}).items()]
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split()[1])
    hdrs = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    return status, hdrs, payload.decode()


def sse_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        events.append((name, data if data == "[DONE]" else json.loads(data)))
    return events


def with_server(test):
    async def main():
        server.nc = nc
        nc.st.headless = nc.st.serving = True
        nc.new_session()
        api = server.ApiServer(KEY)
        srv = await asyncio.start_server(api.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            return await test(port, api)
        finally:
            srv.close()
    return asyncio.run(main())


def test_auth_health_and_discovery(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [text_reply("hi")])

    async def test(port, api):
        assert (await request(port, "GET", "/health", key=None))[0] == 200
        assert (await request(port, "GET", "/v1/models", key=None))[0] == 401
        assert (await request(port, "GET", "/v1/models", key="wrong-key-0000"))[0] == 401
        status, _, body = await request(port, "GET", "/v1/models")
        assert status == 200 and json.loads(body)["data"][0]["id"] == "nanocode"
        caps = json.loads((await request(port, "GET", "/v1/capabilities"))[2])
        assert caps["features"]["responses_api"] and caps["auth"]["required"]
        tools = json.loads((await request(port, "GET", "/v1/tools"))[2])
        assert {"name": "read", "read_only": True} .items() <= next(t for t in tools if t["name"] == "read").items()
        assert (await request(port, "GET", "/v1/nope"))[0] == 404
        assert (await request(port, "POST", "/v1/chat/completions", {"messages": []}))[0] == 400

    with_server(test)


def test_chat_completion_runs_tools_and_answers(sandbox, monkeypatch):
    Path("app.py").write_text("")
    requests = fake_openrouter(monkeypatch, [
        [tool_call_chunk("c1", "glob", {"pattern": "*.py"})], text_reply("One file: app.py", (100, 5))])

    async def test(port, api):
        status, headers, body = await request(port, "POST", "/v1/chat/completions", {
            "model": "nanocode", "messages": [{"role": "system", "content": "Be brief."},
                                              {"role": "user", "content": "Which python files are there?"}]})
        assert status == 200, body
        data = json.loads(body)
        assert data["choices"][0]["message"]["content"] == "One file: app.py"
        assert data["usage"]["prompt_tokens"] == 100 + 0 and headers["x-nanocode-session-id"] == data["session_id"]
        prompt = requests[0]["messages"][-1]["content"]
        assert "Be brief." in prompt and "Which python files" in prompt
        assert requests[1]["messages"][-1]["role"] == "tool" and requests[1]["messages"][-1]["content"] == "app.py"
        assert (nc.sessions_dir() / f"{data['session_id']}.json").is_file()   # resumable in the app

    with_server(test)


def test_chat_completion_streams(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [[tool_call_chunk("c1", "glob", {"pattern": "*.md"})], text_reply("Nothing.")])

    async def test(port, api):
        status, headers, body = await request(port, "POST", "/v1/chat/completions", {
            "stream": True, "messages": [{"role": "user", "content": "find docs"}]})
        assert status == 200 and headers["content-type"] == "text/event-stream"
        events = sse_events(body)
        assert events[-1] == (None, "[DONE]")
        assert any(n == "nanocode.tool.progress" and d["tool"] == "glob" for n, d in events)
        text = "".join(d["choices"][0]["delta"].get("content", "") for n, d in events
                       if n is None and isinstance(d, dict) and "choices" in d)
        assert "Nothing." in text
        assert events[-2][1]["choices"][0]["finish_reason"] == "stop"

    with_server(test)


def test_responses_continue_a_conversation(sandbox, monkeypatch):
    requests = fake_openrouter(monkeypatch, [text_reply("Noted: 42."), text_reply("It was 42.")])

    async def test(port, api):
        first = json.loads((await request(port, "POST", "/v1/responses",
                                          {"input": "remember 42", "conversation": "demo"}))[2])
        assert first["status"] == "completed" and first["output_text"] == "Noted: 42."
        assert first["output"][-1]["content"][0]["text"] == "Noted: 42."
        second = json.loads((await request(port, "POST", "/v1/responses",
                                           {"input": "what number?", "conversation": "demo"}))[2])
        assert second["previous_response_id"] == first["id"] and second["session_id"] == first["session_id"]
        assert [m["content"] for m in requests[1]["messages"] if m["role"] == "user"] == ["remember 42", "what number?"]
        third = json.loads((await request(port, "POST", "/v1/responses",
                                          {"input": "and again?", "previous_response_id": first["id"]}))[2])
        assert third["session_id"] == first["session_id"]
        status, _, body = await request(port, "GET", f"/v1/responses/{first['id']}")
        assert status == 200 and json.loads(body)["id"] == first["id"]
        assert (await request(port, "DELETE", f"/v1/responses/{first['id']}"))[0] == 200
        assert (await request(port, "GET", f"/v1/responses/{first['id']}"))[0] == 404
        assert (await request(port, "POST", "/v1/responses", {"input": "x", "previous_response_id": "resp_x"}))[0] == 404

    with_server(test)
    assert "demo" in server.ApiServer(KEY).conversations   # stored on disk, survives a restart


def test_runs_events_and_status(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [[tool_call_chunk("c1", "glob", {"pattern": "*"})], text_reply("Done.")])

    async def test(port, api):
        status, _, body = await request(port, "POST", "/v1/runs", {"input": "list files", "session_id": "my-run"})
        assert status == 202
        run_id = json.loads(body)["run_id"]
        events = sse_events((await request(port, "GET", f"/v1/runs/{run_id}/events"))[2])
        kinds = [n for n, _ in events]
        assert kinds[0] == "run.started" and kinds[-1] == "run.completed"
        assert "tool.started" in kinds and "tool.completed" in kinds and "delta" in kinds
        info = json.loads((await request(port, "GET", f"/v1/runs/{run_id}"))[2])
        assert info["status"] == "completed" and info["output"] == "Done." and info["session_id"] == "my-run"
        assert nc.st.session["id"] == "my-run"

    with_server(test)


def test_read_only_permissions_deny_edits(sandbox, monkeypatch):
    fake_openrouter(monkeypatch, [[tool_call_chunk("c1", "write", {"path": "x.txt", "content": "hi"})],
                                  text_reply("Couldn't write.")])

    async def test(port, api):
        nc.st.auto_approve = False      # "api": {"permissions": "read-only"}
        status, _, body = await request(port, "POST", "/v1/chat/completions",
                                        {"messages": [{"role": "user", "content": "write x.txt"}]})
        assert status == 200 and not Path("x.txt").exists()

    with_server(test)


def test_settings_make_and_keep_a_key(sandbox, monkeypatch):
    server.nc = nc
    nc.st.config = {}
    host, port, key = server.api_settings()
    assert (host, port) == ("127.0.0.1", 8765) and len(key) >= 32
    assert server.api_settings()[2] == key and nc.load_config()["api"]["key"] == key
    monkeypatch.setenv("NANOCODE_API_KEY", "from-env-key")
    assert server.api_settings(port=9000) == ("127.0.0.1", 9000, "from-env-key")


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24


def b64(data):
    return base64.b64encode(data).decode()


def test_message_content(sandbox):
    assert server.message_text([{"type": "text", "text": "a"}, {"type": "input_text", "text": "b"}]) == "a\nb"
    with pytest.raises(server.ApiError):
        server.message_text([{"type": "input_audio", "input_audio": {}}])
    prompt, history, instructions, images = server.api_prompt(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "q1"},
         {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}])
    assert (prompt, history, instructions, images) == ("q2", [("user", "q1"), ("assistant", "a1")], "sys", [])
    assert "Our conversation so far" in server.api_with_history("q2", history)


def test_image_parts(sandbox):
    # chat completions, Responses and Anthropic shapes; the type comes from the bytes, not the label
    text, images = server.content_parts([
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64(JPEG)}"}},
        {"type": "input_image", "image_url": f"data:image/png;base64,{b64(PNG)}"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64(PNG)}}])
    assert text == "look" and images == [("image/jpeg", JPEG), ("image/png", PNG), ("image/png", PNG)]
    assert server.content_parts([{"type": "image_url", "image_url": {"url": b64(PNG)}}])[1] == [("image/png", PNG)]
    assert server.message_text(["hi", {"type": "image_url", "image_url": {"url": "x"}}]) == "hi\n[image]"
    for url in ("https://example.com/a.png", "data:image/png;base64,not base64!", f"data:image/png,{b64(PNG)}",
                f"data:image/png;base64,{b64(b'just text')}"):
        with pytest.raises(server.ApiError):
            server.content_parts([{"type": "image_url", "image_url": {"url": url}}])


def test_chat_completion_with_an_image(sandbox, monkeypatch):
    requests = fake_openrouter(monkeypatch, [text_reply("A red square."), text_reply("Still red.")])

    async def test(port, api):
        status, _, body = await request(port, "POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64(PNG)}"}}]}]},
            headers={"X-Nanocode-Session-Id": "pics"})
        assert status == 200 and json.loads(body)["choices"][0]["message"]["content"] == "A red square."
        sent = requests[0]["messages"][-1]["content"]
        assert sent[0] == {"type": "text", "text": "what is this?"}
        assert sent[1]["image_url"]["url"] == f"data:image/png;base64,{b64(PNG)}"
        saved = json.loads((nc.sessions_dir() / "pics.json").read_text())
        name = saved["messages"][0]["images"][0]
        assert b64(PNG) not in json.dumps(saved) and (nc.project_dir() / "images" / name).read_bytes() == PNG
        # next turn in the same session: only text is sent, and the image is still in the history
        await request(port, "POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "and now?"}]},
                      headers={"X-Nanocode-Session-Id": "pics"})
        assert requests[1]["messages"][1]["content"][1]["image_url"]["url"].endswith(b64(PNG))

    with_server(test)


def test_images_for_ollama(sandbox):
    name = nc.save_image(PNG, "image/png")
    msgs = nc.to_ollama_messages([{"role": "user", "content": "see", "images": [name]}])
    assert msgs == [{"role": "user", "content": "see", "images": [b64(PNG)]}]


def test_images_for_claude_subscription(sandbox, monkeypatch):
    name = nc.save_image(JPEG, "image/jpeg")
    sent = []

    class Client:
        async def query(self, prompt):
            sent.extend([m async for m in prompt])

        async def receive_response(self):
            return
            yield

    monkeypatch.setattr(nc.st, "client", Client())
    monkeypatch.setattr(nc, "sdk", type("sdk", (), {"StreamEvent": None, "types": None}))
    asyncio.run(nc.sdk_query("what's this?", [name]))
    content = sent[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "what's this?"}
    assert content[1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": b64(JPEG)}


def test_saved_images_are_not_committed(sandbox):
    nc.save_image(PNG, "image/png")
    ignored = (nc.project_dir() / ".gitignore").read_text().splitlines()
    assert "images/" in ignored and "api.json" in ignored
