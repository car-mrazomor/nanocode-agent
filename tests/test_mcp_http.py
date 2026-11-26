import asyncio
import json
import os
import time
from urllib.parse import parse_qs, urlparse

import httpx

import nanocode as nc
from nanokit import mcp_http
from test_nanocode import mock_http, plain, run

TOOL = {"name": "echo", "description": "Echo text.", "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}


def reply(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def answer(body):
    method = body.get("method")
    if method == "initialize":
        return reply(body["id"], {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}})
    if method == "tools/list":
        return reply(body["id"], {"tools": [TOOL]})
    if method == "tools/call":
        return reply(body["id"], {"content": [{"type": "text", "text": "echo: " + body["params"]["arguments"]["text"]}]})
    return None


def test_streamable_http_json_and_sse_replies(sandbox, monkeypatch):
    monkeypatch.setenv("MYKEY", "secret")
    nc.st.config = {"mcp_servers": {"r": {"url": "https://mcp.test/mcp", "headers": {"X-Key": "${MYKEY}"}}}}
    seen = []

    def handler(req):
        body = json.loads(req.content) if req.content else {}
        seen.append((req.method, req.headers.get("mcp-session-id"), req.headers.get("x-key"),
                     req.headers.get("mcp-protocol-version"), body.get("method")))
        if req.method == "DELETE":
            return httpx.Response(200)
        assert "text/event-stream" in req.headers["accept"]
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "tools/list":     # an SSE answer, with a ping from the server first
            events = [{"jsonrpc": "2.0", "id": "p1", "method": "ping"}, answer(body)]
            text = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=answer(body), headers={"mcp-session-id": "S1"})

    mock_http(monkeypatch, handler)

    async def scenario():
        await nc.ensure_mcp()
        result = await nc.run_tool("mcp__r__echo", {"text": "hi"})
        await nc.close_mcp()
        return result

    text, error = run(scenario())
    assert not error and text == "echo: hi"
    assert seen[0] == ("POST", None, "secret", None, "initialize")
    later = [s for s in seen if s[4] == "tools/call"][0]
    assert later[1] == "S1" and later[3] == "2025-06-18"
    assert ("POST", "S1", "secret", "2025-06-18", None) in seen          # our answer to the ping
    assert seen[-1][0] == "DELETE" and seen[-1][1] == "S1"


def test_legacy_sse_transport(sandbox, monkeypatch):
    nc.st.config = {"mcp_servers": {"old": {"url": "https://mcp.test/sse"}}}
    queue = asyncio.Queue()
    posts = []

    async def stream():
        yield b"event: endpoint\ndata: /messages?session=7\n\n"
        while True:
            msg = await queue.get()
            if msg is None:
                return
            yield f"event: message\ndata: {json.dumps(msg)}\n\n".encode()

    async def handler(req):
        if req.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())
        if req.url.path == "/sse":               # Streamable HTTP POST: not here
            return httpx.Response(405)
        body = json.loads(req.content)
        posts.append(str(req.url))
        if answer(body):
            queue.put_nowait(answer(body))
        return httpx.Response(202)

    mock_http(monkeypatch, handler)

    async def scenario():
        await nc.ensure_mcp()
        result = await nc.run_tool("mcp__old__echo", {"text": "yo"})
        client = nc.st.mcp["old"]
        legacy = client.legacy
        queue.put_nowait(None)
        await nc.close_mcp()
        return result, legacy

    (text, error), legacy = run(scenario())
    assert not error and text == "echo: yo" and legacy
    assert all(p == "https://mcp.test/messages?session=7" for p in posts)


def test_any_http_error_becomes_a_tool_error(sandbox, monkeypatch):
    current = []

    def handler(req):
        raise current[-1]
    mock_http(monkeypatch, handler)
    for error in (httpx.ReadTimeout("slow"), httpx.DecodingError("bad gzip"), httpx.TooManyRedirects("loop"),
                  httpx.ProxyError("proxy")):
        current.append(error)
        client = mcp_http.McpHttpClient("r", {"url": "https://mcp.test/mcp"})

        async def go():
            try:
                await client.ensure()
            except nc.ToolError as e:
                return str(e)
        message = run(go())
        assert message and "MCP r" in message and "mcp.test" in message, error


def test_legacy_endpoint_on_another_server_is_refused(sandbox, monkeypatch):
    nc.st.config = {"mcp_servers": {"old": {"url": "https://mcp.test/sse", "headers": {"Authorization": "x"}}}}
    posts = []

    async def stream():
        yield b"event: endpoint\ndata: https://evil.test/messages\n\n"
        await asyncio.sleep(30)

    async def handler(req):
        if req.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())
        if req.url.path == "/sse":
            return httpx.Response(405)
        posts.append(str(req.url))
        return httpx.Response(202)

    mock_http(monkeypatch, handler)

    client = mcp_http.McpHttpClient("old", {"url": "https://mcp.test/sse", "type": "sse"})

    async def scenario():
        try:
            await client.ensure()
        except nc.ToolError as e:
            return str(e)

    assert "another server (evil.test)" in run(scenario())
    assert not any("evil.test" in p for p in posts)
    assert mcp_http.same_origin("https://a.test/x", "https://A.test:443/y")
    assert not mcp_http.same_origin("https://a.test/x", "http://a.test/x")
    assert not mcp_http.same_origin("https://a.test:8443/x", "https://a.test/x")


def oauth_server(tokens, issued):
    """A server that wants a token, with discovery, registration and a token endpoint."""
    def handler(req):
        url = str(req.url)
        if url == "https://mcp.test/mcp":
            if req.headers.get("authorization") != f"Bearer {issued[-1] if issued else 'none'}":
                return httpx.Response(401, headers={"www-authenticate": 'Bearer resource_metadata='
                                                   '"https://mcp.test/.well-known/oauth-protected-resource/mcp"'})
            body = json.loads(req.content)
            return httpx.Response(202) if "id" not in body else httpx.Response(200, json=answer(body))
        if url == "https://mcp.test/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": "https://mcp.test/mcp",
                                             "authorization_servers": ["https://auth.test"], "scopes_supported": ["read"]})
        if url == "https://auth.test/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"authorization_endpoint": "https://auth.test/authorize",
                                             "token_endpoint": "https://auth.test/token",
                                             "registration_endpoint": "https://auth.test/register"})
        if url == "https://auth.test/register":
            assert json.loads(req.content)["redirect_uris"][0].startswith("http://127.0.0.1:")
            return httpx.Response(201, json={"client_id": "cid"})
        if url == "https://auth.test/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            tokens.append(form)
            issued.append(f"tok{len(issued) + 1}")
            return httpx.Response(200, json={"access_token": issued[-1], "refresh_token": "r1", "expires_in": 3600})
        return httpx.Response(404)
    return handler


def test_oauth_login_and_refresh(sandbox, monkeypatch):
    nc.st.config = {"mcp_servers": {"secure": {"url": "https://mcp.test/mcp"}}}
    tokens, issued, opened = [], [], []
    mock_http(monkeypatch, oauth_server(tokens, issued))

    def browser(link):                  # the user logs in: the browser goes to our callback
        opened.append(link)
        query = {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}
        callback = urlparse(query["redirect_uri"])

        async def visit():
            reader, writer = await asyncio.open_connection("127.0.0.1", callback.port)
            writer.write(f"GET /callback?code=C0DE&state={query['state']} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await writer.drain()
            await reader.read()
            writer.close()
        asyncio.get_running_loop().create_task(visit())

    async def scenario():
        await nc.ensure_mcp()
        needs = nc.st.mcp_errors.get("secure", "")
        message = await mcp_http.login("secure", nc.st.config["mcp_servers"]["secure"], browser)
        await mcp_http.reconnect("secure")
        await nc.ensure_mcp()
        first = await nc.run_tool("mcp__secure__echo", {"text": "a"})
        data = mcp_http.load_tokens()           # the token runs out: it's refreshed before the next start
        data["https://mcp.test/mcp"]["expires_at"] = time.time() - 10
        mcp_http.save_tokens(data)
        await mcp_http.reconnect("secure")
        await nc.ensure_mcp()
        second = await nc.run_tool("mcp__secure__echo", {"text": "b"})
        await nc.close_mcp()
        return needs, message, first, second

    needs, message, first, second = run(scenario())
    assert "/mcp login secure" in needs and message == "logged in to secure"
    query = {k: v[0] for k, v in parse_qs(urlparse(opened[0]).query).items()}
    assert query["code_challenge_method"] == "S256" and query["client_id"] == "cid" and query["scope"] == "read"
    assert query["resource"] == "https://mcp.test/mcp"
    assert tokens[0]["grant_type"] == "authorization_code" and tokens[0]["code"] == "C0DE" and tokens[0]["code_verifier"]
    assert tokens[1]["grant_type"] == "refresh_token" and tokens[1]["refresh_token"] == "r1"
    assert first == ("echo: a", False) and second == ("echo: b", False)
    saved = json.loads(mcp_http.tokens_file().read_text())
    assert saved["https://mcp.test/mcp"]["access_token"] == "tok2"
    if os.name != "nt":
        assert oct(mcp_http.tokens_file().stat().st_mode & 0o777) == "0o600"


def test_mcp_add_url_and_list(sandbox, monkeypatch):
    def handler(req):
        body = json.loads(req.content) if req.content else {}
        if req.method == "DELETE" or "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, json=answer(body))

    mock_http(monkeypatch, handler)

    async def scenario():
        await nc.run_input("/mcp add docs https://docs.test/mcp")
        await nc.close_mcp()

    run(scenario())
    assert nc.load_config()["mcp_servers"] == {"docs": {"url": "https://docs.test/mcp"}}
    screen = plain(nc.st.output)
    assert "Added remote MCP server docs" in screen and "1/1 tools" in screen and "https://docs.test/mcp" in screen
    run(nc.run_input("/mcp login nope"))
    assert "no remote MCP server 'nope'" in plain(nc.st.output)
