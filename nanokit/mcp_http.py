"""Remote MCP servers: Streamable HTTP (one POST per JSON-RPC message; the answer comes back as JSON or as an SSE
stream) and the older SSE transport (a GET stream whose "endpoint" event says where to POST). Servers that need a
login get the MCP authorization flow (OAuth 2.1: discovery, dynamic client registration, authorization code with
PKCE through a callback on localhost); tokens are kept in ~/.nanocode/mcp-tokens.json and refreshed.

    "mcp_servers": {"linear": {"url": "https://mcp.linear.app/mcp", "headers": {"X-Key": "${KEY}"}}}
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
import webbrowser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

import nanocode as nc
from nanokit import command

PROTOCOL = "2025-06-18"
LOGIN_TIMEOUT = 300


def same_origin(a, b):
    """Same scheme, host and port."""
    x, y = urlparse(a), urlparse(b)
    port = {"http": 80, "https": 443}
    try:
        return (x.scheme.lower(), (x.hostname or "").lower(), x.port or port.get(x.scheme.lower())) == \
            (y.scheme.lower(), (y.hostname or "").lower(), y.port or port.get(y.scheme.lower()))
    except ValueError:                         # a bad port
        return False


class NeedsLogin(nc.ToolError):
    """The server answered 401 and there's no token that works."""


class SessionExpired(nc.ToolError):
    """The server forgot our session (404 with a session id): start a new one."""


class UseSse(Exception):
    """The URL doesn't take Streamable HTTP POSTs: it's an old-style SSE server."""


async def sse_events(lines):
    """(event, data) for each event of a text/event-stream."""
    event, data = "message", []
    async for line in lines:
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
    if data:
        yield event, "\n".join(data)


# --- Tokens: ~/.nanocode/mcp-tokens.json, readable only by you ---

def tokens_file():
    return nc.home_dir() / "mcp-tokens.json"


def load_tokens():
    try:
        data = json.loads(tokens_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_tokens(data):
    path = tokens_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def token_for(url):
    return load_tokens().get(url) or {}


def store_token(url, entry, response):
    entry = dict(entry)
    entry["access_token"] = response["access_token"]
    entry["refresh_token"] = response.get("refresh_token") or entry.get("refresh_token")
    expires = response.get("expires_in")
    entry["expires_at"] = time.time() + float(expires) if isinstance(expires, (int, float)) else None
    data = load_tokens()
    data[url] = entry
    save_tokens(data)
    return entry


async def refresh(url, force=False):
    """Gets a new access token with the refresh token; True when there's a usable token now."""
    entry = token_for(url)
    if not entry.get("access_token"):
        return False
    expired = entry.get("expires_at") and entry["expires_at"] < time.time() + 60
    if not force and not expired:
        return True
    if not entry.get("refresh_token") or not entry.get("token_endpoint"):
        return False
    form = {"grant_type": "refresh_token", "refresh_token": entry["refresh_token"], "client_id": entry["client_id"]}
    if entry.get("resource"):
        form["resource"] = entry["resource"]
    if entry.get("client_secret"):
        form["client_secret"] = entry["client_secret"]
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(entry["token_endpoint"], data=form, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return False
        store_token(url, entry, resp.json())
        return True
    except (httpx.HTTPError, ValueError, KeyError) as e:
        nc.log_error(e)
        return False


# --- The client ---

class McpHttpClient:
    """Same interface as nanocode's McpClient (ensure, list_tools, call, close, tools, alive), over HTTP."""

    def __init__(self, name, spec):
        self.name, self.label = name, f"MCP {name}"
        self.url = spec["url"]
        self.legacy = spec.get("type") == "sse" or spec.get("transport") == "sse"
        self.extra = spec.get("headers") or {}
        self.spec = None  # the config it was started from (set by the core)
        self.tools = None
        self.ready = False
        self.lock = asyncio.Lock()
        self.http = None
        self.session_id = None
        self.protocol = PROTOCOL
        self.next_id = 0
        self.challenge = ""  # the last 401's WWW-Authenticate header
        self.stream = None   # SSE transport: the GET stream reader
        self.endpoint = None
        self.pending = {}

    def alive(self):
        return self.ready and (not self.legacy or (self.stream is not None and not self.stream.done()))

    def headers(self):
        headers = {k: os.path.expandvars(str(v)) for k, v in self.extra.items()}
        token = token_for(self.url).get("access_token")
        if token and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def needs_login(self, resp):
        self.challenge = resp.headers.get("www-authenticate", "")
        return NeedsLogin(f"{self.label} needs you to log in: /mcp login {self.name}")

    async def ensure(self):
        async with self.lock:
            if self.alive():
                return
            await self.close()
            await refresh(self.url)
            self.http = httpx.AsyncClient(timeout=httpx.Timeout(connect=20, read=None, write=60, pool=20),
                                          follow_redirects=True)
            try:
                if not self.legacy:
                    try:
                        await self._initialize()
                    except UseSse:
                        self.legacy = True
                if self.legacy:
                    await self._open_stream()
                    await self._initialize()
            except BaseException:
                await asyncio.shield(self.close())
                raise
            self.ready = True

    async def _initialize(self):
        result = await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                   "clientInfo": {"name": "nanocode", "version": nc.__version__}}, 60)
        self.protocol = result.get("protocolVersion") or PROTOCOL
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def request(self, method, params=None, timeout=600):
        self.next_id += 1
        rid = self.next_id
        message = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        try:
            reply = await asyncio.wait_for(self._exchange(message), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            self.pending.pop(rid, None)
            cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled",
                      "params": {"requestId": rid, "reason": "cancelled by nanocode"}}
            asyncio.ensure_future(self._quietly(self._send(cancel)))
            if isinstance(e, asyncio.CancelledError):
                raise
            raise nc.ToolError(f"{self.label} didn't finish within {timeout}s; try a narrower request") from e
        if "error" in reply:
            error = reply["error"]
            raise nc.ToolError(f"{self.label}: {error.get('message', error) if isinstance(error, dict) else error}")
        return reply.get("result") or {}

    async def _quietly(self, coro):
        try:
            await coro
        except Exception:
            pass

    async def _exchange(self, message):
        if self.legacy:
            future = asyncio.get_running_loop().create_future()
            self.pending[message["id"]] = future
            await self._send(message)
            return await future
        reply = await self._post(message)
        if reply is None:
            raise nc.ToolError(f"{self.label} sent no reply")
        return reply

    async def _send(self, message):
        """A message that expects no reply here (a notification, or any message on the SSE transport)."""
        if self.legacy:
            if not self.endpoint or not self.http:
                raise nc.ToolError(f"{self.label} isn't connected")
            try:
                resp = await self.http.post(self.endpoint, json=message, headers={**self.headers(),
                                            "Content-Type": "application/json"})
            except httpx.HTTPError as e:
                raise self.http_error(e) from e
            if resp.status_code == 401:
                raise self.needs_login(resp)
            if resp.status_code >= 400:
                raise nc.ToolError(f"{self.label}: HTTP {resp.status_code} {resp.text[:200]}")
            if "json" in resp.headers.get("content-type", "") and resp.content:   # some answer right away
                try:
                    self._dispatch(resp.json())
                except ValueError:
                    pass
            return
        await self._post(message)

    async def _post(self, message, retried=False):
        """Streamable HTTP: POSTs a message; the reply (if it has an id) comes back as JSON or an SSE stream."""
        if not self.http:
            raise nc.ToolError(f"{self.label} isn't connected")
        headers = {**self.headers(), "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.ready:
            headers["MCP-Protocol-Version"] = self.protocol
        try:
            async with self.http.stream("POST", self.url, json=message, headers=headers) as resp:
                if resp.status_code == 401:
                    await resp.aread()
                    if not retried and await refresh(self.url, force=True):
                        return await self._post(message, retried=True)
                    raise self.needs_login(resp)
                if resp.status_code == 404 and self.session_id:
                    self.session_id, self.ready = None, False
                    raise SessionExpired(f"{self.label}: the session ended; it reconnects on the next call")
                if resp.status_code in (400, 404, 405) and message.get("method") == "initialize" and \
                        not self.session_id:
                    await resp.aread()
                    raise UseSse()
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise nc.ToolError(f"{self.label}: HTTP {resp.status_code} {body[:200]}")
                if resp.headers.get("mcp-session-id"):
                    self.session_id = resp.headers["mcp-session-id"]
                if "id" not in message or "method" not in message:
                    return None                    # a notification or a reply: 202 Accepted
                if "text/event-stream" in resp.headers.get("content-type", ""):
                    async for _, data in sse_events(resp.aiter_lines()):
                        try:
                            msg = json.loads(data)
                        except ValueError:
                            continue
                        if msg.get("id") == message["id"] and ("result" in msg or "error" in msg):
                            return msg
                        if "method" in msg and "id" in msg:
                            asyncio.ensure_future(self._quietly(self._send(self._answer(msg))))
                    raise nc.ToolError(f"{self.label} closed the stream without a reply")
                body = await resp.aread()
                data = json.loads(body) if body.strip() else None
                for msg in data if isinstance(data, list) else [data]:
                    if isinstance(msg, dict) and msg.get("id") == message["id"]:
                        return msg
                raise nc.ToolError(f"{self.label} sent no reply")
        except httpx.HTTPError as e:           # connection, timeout, protocol, decoding, bad URL…
            raise self.http_error(e) from e
        except ValueError as e:
            raise nc.ToolError(f"{self.label} sent something that isn't JSON") from e

    def http_error(self, e):
        if isinstance(e, httpx.TimeoutException):
            return nc.ToolError(f"{self.label} at {self.url} didn't answer in time ({type(e).__name__})")
        return nc.ToolError(f"can't reach {self.label} at {self.url} ({type(e).__name__})")

    @staticmethod
    def _answer(request):
        """Our answer to a request from the server: ping is fine, the rest (sampling, elicitation…) isn't done."""
        if request.get("method") == "ping":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {}}
        return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "not supported by nanocode"}}

    # The SSE transport: GET opens a stream; its first event names the URL to POST messages to.

    async def _open_stream(self):
        endpoint = asyncio.get_running_loop().create_future()
        self.stream = asyncio.ensure_future(self._read_stream(endpoint))
        try:
            self.endpoint = await asyncio.wait_for(asyncio.shield(endpoint), 30)
        except asyncio.TimeoutError as e:
            raise nc.ToolError(f"{self.label} didn't send its endpoint within 30s") from e

    async def _read_stream(self, endpoint):
        error = nc.ToolError(f"{self.label} disconnected; it reconnects on the next call")
        try:
            headers = {**self.headers(), "Accept": "text/event-stream"}
            async with self.http.stream("GET", self.url, headers=headers) as resp:
                if resp.status_code == 401:
                    raise self.needs_login(resp)
                if resp.status_code != 200:
                    raise nc.ToolError(f"{self.label}: HTTP {resp.status_code}")
                async for event, data in sse_events(resp.aiter_lines()):
                    if event == "endpoint" and not endpoint.done():
                        target = urljoin(self.url, data.strip())
                        if not same_origin(target, self.url):    # our headers and tokens go there
                            raise nc.ToolError(f"{self.label} named a message endpoint on another server "
                                               f"({urlparse(target).netloc or target}); refusing it")
                        endpoint.set_result(target)
                    elif event == "message":
                        try:
                            self._dispatch(json.loads(data))
                        except ValueError:
                            continue
        except asyncio.CancelledError:
            pass
        except nc.ToolError as e:
            error = e
        except Exception as e:
            nc.log_error(e)
            error = nc.ToolError(f"can't reach {self.label} at {self.url} ({type(e).__name__})")
        finally:
            if not endpoint.done():
                endpoint.set_exception(error)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()

    def _dispatch(self, msg):
        if not isinstance(msg, dict):
            return
        if "id" in msg and ("result" in msg or "error" in msg):
            future = self.pending.pop(msg["id"], None)
            if future and not future.done():
                future.set_result(msg)
        elif "id" in msg and "method" in msg:
            asyncio.ensure_future(self._quietly(self._send(self._answer(msg))))

    async def list_tools(self):
        if self.tools is None:
            await self.ensure()
            tools, cursor = [], None
            while True:
                result = await self.request("tools/list", {"cursor": cursor} if cursor else {}, 120)
                tools += result.get("tools", [])
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            self.tools = {t["name"]: t for t in tools}
        return self.tools

    async def call(self, name, arguments, timeout):
        for attempt in range(2):
            await self.ensure()
            try:
                return await self.request("tools/call", {"name": name, "arguments": arguments}, timeout)
            except SessionExpired:
                if attempt:
                    raise

    async def close(self):
        self.ready = False
        if self.stream:
            self.stream.cancel()
            self.stream = None
        self.endpoint = None
        if self.http:
            if self.session_id and not self.legacy:   # ends the session on the server
                try:
                    await asyncio.wait_for(self.http.delete(self.url, headers={
                        **self.headers(), "Mcp-Session-Id": self.session_id}), 3)
                except Exception:
                    pass
            try:
                await self.http.aclose()
            except Exception:
                pass
            self.http = None
        self.session_id = None


nc.MCP_REMOTE = McpHttpClient


# --- Logging in (the MCP authorization flow) ---

def challenge_params(header):
    return dict(re.findall(r'(\w+)="([^"]*)"', header or ""))


async def first_json(http, urls):
    for url in urls:
        try:
            resp = await http.get(url, headers={"Accept": "application/json", "MCP-Protocol-Version": PROTOCOL})
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, ValueError):
            continue
    return {}


async def discover(http, url, challenge):
    """(the authorization server's metadata, scope, resource) for a server."""
    params = challenge_params(challenge)
    parts = urlparse(url)
    origin, path = f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")
    candidates = [params["resource_metadata"]] if params.get("resource_metadata") else []
    candidates += [f"{origin}/.well-known/oauth-protected-resource{path}", f"{origin}/.well-known/oauth-protected-resource"]
    resource_meta = await first_json(http, list(dict.fromkeys(candidates)))
    issuer = (resource_meta.get("authorization_servers") or [origin])[0]
    issuer_parts = urlparse(issuer)
    issuer_origin, issuer_path = f"{issuer_parts.scheme}://{issuer_parts.netloc}", issuer_parts.path.rstrip("/")
    if issuer_path:
        urls = [f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_path}",
                f"{issuer_origin}/.well-known/openid-configuration{issuer_path}",
                f"{issuer_origin}{issuer_path}/.well-known/openid-configuration"]
    else:
        urls = [f"{issuer_origin}/.well-known/oauth-authorization-server",
                f"{issuer_origin}/.well-known/openid-configuration"]
    meta = await first_json(http, urls) or {"authorization_endpoint": f"{issuer_origin}/authorize",
                                            "token_endpoint": f"{issuer_origin}/token",
                                            "registration_endpoint": f"{issuer_origin}/register"}
    scope = params.get("scope") or " ".join(resource_meta.get("scopes_supported") or [])
    return meta, scope, resource_meta.get("resource") or url


async def wait_for_code(port, state):
    """A one-shot web server on localhost that takes the browser's redirect. Returns (server, port, future)."""
    future = asyncio.get_running_loop().create_future()

    async def handle(reader, writer):
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode(errors="replace")
            while (await asyncio.wait_for(reader.readline(), 10)).strip():
                pass
            target = line.split(" ")[1] if line.count(" ") >= 2 else "/"
            query = {k: v[0] for k, v in parse_qs(urlparse(target).query).items()}
            if query.get("state") == state and (query.get("code") or query.get("error")):
                ok = bool(query.get("code"))
                if not future.done():
                    if ok:
                        future.set_result(query["code"])
                    else:
                        future.set_exception(nc.ToolError(f"login refused: {query.get('error_description') or query['error']}"))
                text = "Logged in. You can close this tab and go back to nanocode." if ok else "Login failed."
            else:
                text = "nanocode is waiting for a login."
            body = f"<html><body style='font-family:sans-serif'><p>{text}</p></body></html>".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: " +
                         str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except (asyncio.TimeoutError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    return server, server.sockets[0].getsockname()[1], future


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def probe(http, url, headers):
    """The WWW-Authenticate header the server sends without a token ('' if it doesn't ask for a login)."""
    resp = await http.post(url, headers={**headers, "Accept": "application/json, text/event-stream",
                                         "Content-Type": "application/json"},
                           json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                               "protocolVersion": PROTOCOL, "capabilities": {},
                               "clientInfo": {"name": "nanocode", "version": nc.__version__}}})
    if resp.status_code != 401:      # an SSE-transport server: the GET says it
        resp = await http.get(url, headers={**headers, "Accept": "text/event-stream"}) \
            if resp.status_code in (400, 404, 405) else resp
    return resp.headers.get("www-authenticate", "") if resp.status_code == 401 else None


async def login(name, spec, open_browser=None):
    """Runs the authorization flow for a server and saves its tokens. Returns a message for the user."""
    url = spec["url"]
    oauth = spec.get("oauth") or {}
    headers = {k: os.path.expandvars(str(v)) for k, v in (spec.get("headers") or {}).items()}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        challenge = await probe(http, url, headers)
        if challenge is None and not oauth:
            return f"{name} doesn't ask for a login"
        meta, scope, resource = await discover(http, url, challenge or "")
        scope = oauth.get("scope") or scope
        state = secrets.token_urlsafe(16)
        server, port, code = await wait_for_code(int(oauth.get("callback_port") or 0), state)
        try:
            redirect = f"http://127.0.0.1:{port}/callback"
            client = {"client_id": oauth.get("client_id"), "client_secret": oauth.get("client_secret")}
            if not client["client_id"]:
                if not meta.get("registration_endpoint"):
                    raise nc.ToolError(f"{name}'s login server doesn't register apps by itself: add "
                                       f'"oauth": {{"client_id": "..."}} to the server in the config')
                resp = await http.post(meta["registration_endpoint"], json={
                    "client_name": "nanocode", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
                if resp.status_code not in (200, 201):
                    raise nc.ToolError(f"{name}: app registration failed (HTTP {resp.status_code}) {resp.text[:200]}")
                registered = resp.json()
                client = {"client_id": registered["client_id"], "client_secret": registered.get("client_secret")}
            verifier, challenge_code = pkce()
            query = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
                     "state": state, "code_challenge": challenge_code, "code_challenge_method": "S256",
                     "resource": resource}
            if scope:
                query["scope"] = scope
            endpoint = meta["authorization_endpoint"]
            link = endpoint + ("&" if "?" in endpoint else "?") + urlencode(query)
            nc.out(f"  {nc.DIM}⎿  log in to {name} in your browser (waiting up to 5 minutes){nc.RESET}\n"
                   f"  {nc.DIM}   not opened? go to: {link}{nc.RESET}")
            try:
                (open_browser or webbrowser.open)(link)
            except Exception as e:
                nc.log_error(e)
            try:
                auth_code = await asyncio.wait_for(code, LOGIN_TIMEOUT)
            except asyncio.TimeoutError as e:
                raise nc.ToolError(f"no login within {LOGIN_TIMEOUT // 60} minutes") from e
        finally:
            server.close()
        form = {"grant_type": "authorization_code", "code": auth_code, "redirect_uri": redirect,
                "client_id": client["client_id"], "code_verifier": verifier, "resource": resource}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        resp = await http.post(meta["token_endpoint"], data=form, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise nc.ToolError(f"{name}: getting the token failed (HTTP {resp.status_code}) {resp.text[:200]}")
        store_token(url, {"client_id": client["client_id"], "client_secret": client.get("client_secret"),
                          "token_endpoint": meta["token_endpoint"], "resource": resource}, resp.json())
    return f"logged in to {name}"


def forget_token(url):
    data = load_tokens()
    if data.pop(url, None) is None:
        return False
    save_tokens(data)
    return True


async def reconnect(name):
    """Starts a server again now (after a login), not at the next retry."""
    client = nc.st.mcp.pop(name, None)
    if client:
        await client.close()
    nc.st.mcp_loaded = None
    nc.st.mcp_errors.pop(name, None)


@command("/mcp", "MCP servers: /mcp add <name> <command or URL> · login <name> · import · tools · on|off")
async def mcp_command(arg):
    word, _, rest = arg.partition(" ")
    rest = rest.strip()
    servers = dict(nc.mcp_servers())
    if word == "add" and re.match(r"\S+\s+https?://", rest):
        name, url, *flags = rest.split()
        servers[name] = {"url": url, **({"type": "sse"} if "--sse" in flags else {})}
        nc.st.config["mcp_servers"] = servers
        nc.save_config()
        nc.out(f"{nc.GREEN}⏺ Added remote MCP server {name}{nc.RESET} {nc.DIM}connecting…{nc.RESET}")
        await nc.ensure_mcp()
        if "/mcp login" in nc.st.mcp_errors.get(name, ""):
            await mcp_command(f"login {name}")
            return
        await nc.cmd_mcp("")
    elif word in ("login", "logout"):
        spec = servers.get(rest)
        if not spec or not spec.get("url"):
            nc.out(f"{nc.RED}⏺ no remote MCP server '{rest}'{nc.RESET} {nc.DIM}(see /mcp){nc.RESET}\n")
            return
        if word == "logout":
            forgot = forget_token(spec["url"])
            await reconnect(rest)
            nc.out(f"{nc.GREEN}⏺ Logged out of {rest}{nc.RESET}\n" if forgot else f"{nc.DIM}not logged in to {rest}{nc.RESET}\n")
            return
        try:
            message = await login(rest, spec)
        except (nc.ToolError, httpx.HTTPError, ValueError, KeyError, OSError) as e:
            nc.out(f"{nc.RED}⏺ couldn't log in to {rest}:{nc.RESET} {e}\n")
            return
        nc.out(f"{nc.GREEN}⏺ {message[0].upper() + message[1:]}{nc.RESET}")
        await reconnect(rest)
        await nc.cmd_mcp("")
    else:
        await nc.cmd_mcp(arg)
