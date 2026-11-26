"""nanocode's HTTP API: `nanocode serve` makes nanocode an OpenAI-compatible endpoint, like hermes-agent's.

Any OpenAI client (curl, the openai SDK, Open WebUI, LobeChat...) can use nanocode as a "model" with all its
tools. One turn runs at a time (nanocode has one active chat); other requests wait their turn. Each API
conversation is a normal saved nanocode session, so you can also /resume it in the app.

The running nanocode module is passed in (`serve(nanocode_module, ...)`), so this shares its state.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.parse

nc = None  # the nanocode module, set by serve()

API_DEFAULT_PORT = 8765
API_MAX_BODY = 40 * 1024 * 1024  # room for a few base64 images
API_MAX_IMAGE = 20 * 1024 * 1024
API_MAX_STORED = 100  # responses kept for previous_response_id
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
API_MAX_QUEUED = 20  # turns waiting for the one before them; more get 429
API_HEADER_TIMEOUT = 30  # seconds to send the request line and headers
API_BODY_TIMEOUT = 120  # seconds to send the body (room for big images on a slow link)
API_MAX_HEADERS = 100
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
HTTP_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                405: "Method Not Allowed", 408: "Request Timeout", 413: "Payload Too Large",
                429: "Too Many Requests", 431: "Request Header Fields Too Large", 500: "Internal Server Error"}


class ApiError(Exception):
    def __init__(self, status, message, code="invalid_request_error"):
        super().__init__(message)
        self.status, self.code = status, code


class ApiRun:
    """One agent turn requested over the API: its events, so clients can stream, poll or attach later."""

    def __init__(self, prompt, session_id=None, images=None, command=False, confirmed=False, mode=None):
        self.images = images or []  # [(mime, bytes)] sent with the prompt
        self.command, self.confirmed, self.mode = command, confirmed, mode   # a slash command (see web_command)
        self.id = "run_" + os.urandom(8).hex()
        self.prompt, self.session_id = prompt, session_id
        self.status = "queued"  # queued, running, completed, failed, cancelled
        self.events, self.listeners = [], []
        self.texts, self.tools = [""], []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0.0}
        self.error, self.task, self.created, self.started = None, None, int(time.time()), False

    @property
    def text(self):
        return "\n\n".join(t.strip() for t in self.texts if t.strip())

    @property
    def done(self):
        return self.status in ("completed", "failed", "cancelled")

    def emit(self, kind, data):
        if kind == "delta":
            self.texts[-1] += data["text"]
        elif kind == "text_end":
            self.texts.append("")
            return
        elif kind == "tool.started":
            self.tools.append({**data, "output": None, "is_error": False})
            self.texts.append("")
        elif kind == "tool.completed":
            for t in self.tools:
                if t["id"] == data["id"]:
                    t.update(output=data["output"], is_error=data["is_error"])
        elif kind == "usage":
            self.usage["input_tokens"] += data["input"]
            self.usage["output_tokens"] += data["output"]
            self.usage["total_tokens"] = self.usage["input_tokens"] + self.usage["output_tokens"]
            self.usage["cost"] += data["cost"]
        event = {"event": kind, "run_id": self.id, "time": time.time(), **data}
        self.events.append(event)
        for queue in self.listeners:
            queue.put_nowait(event)

    def finish(self, status, error=None):
        self.status, self.error = status, error
        self.emit(f"run.{status}", {"session_id": self.session_id, "output": self.text, "usage": self.usage,
                                    **({"error": error} if error else {})})
        for queue in self.listeners:
            queue.put_nowait(None)

    async def follow(self):
        """Every event so far, then the new ones as they happen, until the run ends."""
        queue = asyncio.Queue()
        backlog = list(self.events)
        if not self.done:
            self.listeners.append(queue)
        try:
            for event in backlog:
                yield event
            while not self.done or not queue.empty():
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            if queue in self.listeners:
                self.listeners.remove(queue)

    def info(self):
        return {"object": "nanocode.run", "run_id": self.id, "status": self.status, "session_id": self.session_id,
                "model": nc.st.model, "created": self.created, "output": self.text, "usage": self.usage,
                "error": self.error}


class ApiServer:
    def __init__(self, key):
        self.key = key
        self.lock = asyncio.Lock()
        self.runs = {}
        self.responses, self.conversations = self.load()
        self.fork_notes = {}  # forked Claude subscription chat -> the earlier chat, sent with its first message

    # Stored responses (previous_response_id) survive restarts: .nanocode/api.json in the project
    def store_path(self):
        return nc.project_dir() / "api.json"

    def load(self):
        try:
            data = json.loads(self.store_path().read_text(encoding="utf-8"))
            return dict(data.get("responses") or {}), dict(data.get("conversations") or {})
        except (OSError, json.JSONDecodeError):
            return {}, {}

    def save(self):
        while len(self.responses) > API_MAX_STORED:
            self.responses.pop(next(iter(self.responses)))
        try:
            nc.ensure_project_dir()
            tmp = self.store_path().with_suffix(".tmp")
            tmp.write_text(json.dumps({"responses": self.responses, "conversations": self.conversations}),
                           encoding="utf-8")
            tmp.replace(self.store_path())
        except OSError as e:
            nc.log_error(e)

    # --- running turns ---

    def start(self, prompt, session_id=None, instructions=None, images=None, command=False, confirmed=False,
              mode=None):
        if sum(r.status == "queued" for r in self.runs.values()) >= API_MAX_QUEUED:
            raise ApiError(429, f"{API_MAX_QUEUED} turns are already waiting; try again later", "rate_limit_error")
        run = ApiRun(prompt, session_id, images, command, confirmed, mode)
        self.runs[run.id] = run
        while len(self.runs) > 200:  # forget the oldest finished runs
            old = next((r for r in self.runs.values() if r.done), None)
            if old is None:
                break
            self.runs.pop(old.id)
        run.task = asyncio.ensure_future(self.execute(run, instructions))
        return run

    async def execute(self, run, instructions):
        # Listing commands (/memory, /stats...) only print, so they don't wait for a reply that's running.
        lock_free = run.command and run.prompt.strip() in LOCK_FREE_COMMANDS
        try:
            async with contextlib.nullcontext() if lock_free else self.lock:
                run.status, run.started = "running", True
                if not run.command or (run.session_id and not lock_free):
                    await api_switch_session(run.session_id)
                if run.mode and not lock_free:
                    from nanokit import modes
                    modes.set_mode(run.mode)
                run.session_id = nc.st.session["id"]
                run.emit("run.started", {"session_id": run.session_id, "model": nc.st.model})
                text = run.prompt if not instructions else \
                    f"Extra instructions from the app calling nanocode's API:\n{instructions}\n\n---\n\n{run.prompt}"
                note = None if run.command else self.fork_notes.pop(run.session_id, None)
                if note:
                    text = ("This chat continues an earlier conversation (a fork of it). This is what was on screen "
                            f"there, most recent last:\n\n<earlier_chat>\n{note}\n</earlier_chat>\n\n---\n\n{text}")
                token = nc.EVENTS.set(run.emit)
                try:
                    nc.st.last_error = None
                    nc.out(f"{nc.DIM}API {run.id} · session {run.session_id}{nc.RESET}")
                    if run.command:
                        tokens = nc.COMMAND_OUTPUT.set(True), nc.CONFIRMED.set(run.confirmed)
                        try:
                            await nc.run_input(run.prompt)
                        finally:
                            nc.COMMAND_OUTPUT.reset(tokens[0])
                            nc.CONFIRMED.reset(tokens[1])
                        run.session_id = nc.st.session["id"]
                    else:
                        images = [nc.save_image(data, mime) for mime, data in run.images]
                        display = run.prompt + (f"\n[{len(images)} image{'s' * (len(images) > 1)}]" if images else "")
                        await nc.send(nc.expand_mentions(text, images), display=display, images=images)
                finally:
                    nc.EVENTS.reset(token)
                    if not lock_free:
                        nc.stream_end()
            run.finish("completed")
        except asyncio.CancelledError:
            nc.stream_end()
            run.finish("cancelled")
        except Exception as e:
            nc.log_error(e)
            run.finish("failed", str(e))

    async def stop(self, run):
        if run.done:
            return
        if run.started and nc.st.backend == "sdk" and nc.st.client:
            try:
                await nc.st.client.interrupt()
                return
            except Exception as e:
                nc.log_error(e)
        run.task.cancel()

    # --- HTTP ---

    async def stop_on_disconnect(self, reader, run):
        """A client that gives up (closes the connection, or times out and retries) shouldn't leave its turn
        running: its edits and commands would happen twice."""
        try:
            while await reader.read(65536):
                pass
        except (ConnectionError, OSError):
            pass
        if not run.done:
            nc.out(f"{nc.DIM}API {run.id}: the client disconnected; stopping it{nc.RESET}")
            await self.stop(run)

    async def streaming(self, reader, run, stream):
        watcher = asyncio.ensure_future(self.stop_on_disconnect(reader, run)) if reader else None
        try:
            await stream
        except (ConnectionError, OSError):
            await self.stop(run)                     # the client went away mid-stream
            raise
        finally:
            if watcher:
                watcher.cancel()

    async def wait_for_run(self, reader, run):
        watcher = asyncio.ensure_future(self.stop_on_disconnect(reader, run))
        try:
            await asyncio.shield(run.task)
        finally:
            watcher.cancel()

    async def handle(self, reader, writer):
        try:
            method, path, headers, body = await read_http_request(reader)
            await self.route(method, path, headers, body, writer, reader)
        except ApiError as e:
            await write_json(writer, e.status, {"error": {"message": str(e), "type": e.code}})
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception as e:
            nc.log_error(e)
            try:
                await write_json(writer, 500, {"error": {"message": str(e), "type": "server_error"}})
            except ConnectionError:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def check_auth(self, headers):
        sent = headers.get("authorization", "")
        token = sent[7:].strip() if sent.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token, self.key):
            raise ApiError(401, "missing or wrong API key (send: Authorization: Bearer <key>)", "authentication_error")

    async def route(self, method, path, headers, body, writer, reader=None):
        path, _, query = path.partition("?")
        path = path.rstrip("/") or "/"
        query = dict(urllib.parse.parse_qsl(query))
        if path in ("/health", "/v1/health"):
            return await write_json(writer, 200, {"status": "ok", "version": nc.__version__})
        page = web_page(path) if method == "GET" else None
        if page:                                 # the browser chat page (nanokit/web): no data in it, so no key
            content_type, body, extra = page
            writer.write(http_head(200, content_type, extra, len(body)) + body)
            return await writer.drain()
        self.check_auth(headers)
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/v1/models":
            return await write_json(writer, 200, {"object": "list", "data": [
                {"id": "nanocode", "object": "model", "created": int(time.time()), "owned_by": "nanocode",
                 "backend_model": nc.st.model, "provider": nc.st.provider}]})
        if method == "GET" and path == "/v1/capabilities":
            return await write_json(writer, 200, api_capabilities())
        if method == "GET" and path == "/v1/skills":
            return await write_json(writer, 200, [{"name": n, "description": s["description"]}
                                                  for n, s in nc.list_skills().items()])
        if method == "GET" and path == "/v1/tools":
            return await write_json(writer, 200, [{"name": t["name"], "description": t["description"],
                                                   "read_only": nc.tool_is_read_only(t)} for t in nc.active_tools()])
        if method == "GET" and path == "/v1/sessions":
            chats = await asyncio.to_thread(nc.list_sessions, 200)    # reads up to 200 files: not on the loop
            return await write_json(writer, 200, {"object": "list", "data": [
                {"id": c["id"], "title": c.get("title"), "created": c.get("created"), "updated": c.get("updated"),
                 "model": c.get("model")} for c in chats]})
        if method == "GET" and parts[:2] == ["v1", "sessions"] and len(parts) == 3:
            return await write_json(writer, 200, api_session(parts[2]))
        if method == "GET" and parts[:2] == ["v1", "sessions"] and parts[3:] == ["export"]:
            return await write_json(writer, 200, api_export(parts[2], query.get("format", "md")))
        if method == "POST" and parts[:2] == ["v1", "sessions"] and parts[3:] == ["fork"]:
            async with self.lock:                    # not while a turn is writing that chat
                fork = api_fork(parts[2], self.fork_notes)
            return await write_json(writer, 200, fork)
        if method == "GET" and path == "/v1/search":
            return await write_json(writer, 200, await api_search(query.get("q", "")))
        if method == "GET" and path == "/v1/workspace":
            return await write_json(writer, 200, api_workspace())
        if method == "POST" and path == "/v1/chat/completions":
            return await self.chat_completions(parse_json(body), headers, writer, reader)
        if method == "POST" and path == "/v1/responses":
            return await self.create_response(parse_json(body), writer, reader)
        if parts[:2] == ["v1", "responses"] and len(parts) == 3:
            stored = self.responses.get(parts[2])
            if stored is None:
                raise ApiError(404, f"no stored response '{parts[2]}'", "not_found")
            if method == "GET":
                return await write_json(writer, 200, stored["response"])
            if method == "DELETE":
                self.responses.pop(parts[2])
                self.save()
                return await write_json(writer, 200, {"id": parts[2], "object": "response", "deleted": True})
        if method == "POST" and path == "/v1/runs":
            data = parse_json(body)
            session_id = api_session_id(data.get("session_id") or data.get("conversation"))
            mode = data.get("mode")
            if mode not in (None, "plan", "build"):
                raise ApiError(400, "mode is plan or build")
            if data.get("command"):                  # a slash command, like typing it in the terminal
                text = web_command(data.get("input"))
                run = self.start(text, session_id, command=True, confirmed=bool(data.get("confirmed")), mode=mode)
                return await write_json(writer, 202, {"run_id": run.id, "status": "started"})
            prompt, history, instructions, images = api_prompt(data.get("input"), data.get("instructions"))
            run = self.start(api_with_history(prompt, history), session_id, instructions, images, mode=mode)
            return await write_json(writer, 202, {"run_id": run.id, "status": "started"})
        if parts[:2] == ["v1", "runs"] and len(parts) >= 3:
            run = self.runs.get(parts[2])
            if run is None:
                raise ApiError(404, f"no run '{parts[2]}'", "not_found")
            if method == "GET" and len(parts) == 3:
                return await write_json(writer, 200, run.info())
            if method == "GET" and parts[3:] == ["events"]:
                await start_sse(writer)
                async for event in run.follow():
                    await write_sse(writer, event, event["event"])
                return
            if method == "POST" and parts[3:] == ["stop"]:
                await self.stop(run)
                return await write_json(writer, 200, {"run_id": run.id, "status": "stopping"})
        raise ApiError(404, f"{method} {path} isn't an endpoint (GET /v1/capabilities lists them)", "not_found")

    # --- POST /v1/chat/completions ---

    async def chat_completions(self, data, headers, writer, reader=None):
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ApiError(400, "'messages' must be a non-empty list")
        system = "\n\n".join(message_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer"))
        turns = [m for m in messages if m.get("role") in ("user", "assistant")]
        if not turns or turns[-1].get("role") != "user":
            raise ApiError(400, "the last message must be from the user")
        session_id = api_session_id(headers.get("x-nanocode-session-id"))
        prompt, images = content_parts(turns[-1].get("content"))
        if session_id is None:  # stateless, like OpenAI: the history comes with every request
            prompt = api_with_history(prompt, [(m["role"], message_text(m.get("content"))) for m in turns[:-1]])
        run = self.start(prompt, session_id, system or None, images)
        cid, created = "chatcmpl-" + run.id[4:], int(time.time())

        def chunk(delta, finish=None, **extra):
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "nanocode",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}

        if data.get("stream"):
            await self.streaming(reader, run, self.stream_chat(writer, run, chunk))
            return
        await self.wait_for_run(reader, run) if reader else await asyncio.shield(run.task)
        if run.status == "failed":
            raise ApiError(500, run.error or "the turn failed", "server_error")
        await write_json(writer, 200, {
            "id": cid, "object": "chat.completion", "created": created, "model": "nanocode",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": run.text},
                         "finish_reason": "stop" if run.status == "completed" else "cancelled"}],
            "usage": openai_usage(run.usage), "session_id": run.session_id},
            {"X-Nanocode-Session-Id": run.session_id or ""})

    async def stream_chat(self, writer, run, chunk):
        await start_sse(writer, {"X-Nanocode-Run-Id": run.id})
        await write_sse(writer, chunk({"role": "assistant", "content": ""}))
        async for event in run.follow():
            if event["event"] == "run.started":
                await write_sse(writer, {"run_id": run.id, "session_id": event["session_id"]}, "nanocode.session")
            elif event["event"] == "delta":
                await write_sse(writer, chunk({"content": event["text"]}))
            elif event["event"] == "text_end" or event["event"] == "tool.started":
                if event["event"] == "tool.started":
                    await write_sse(writer, {"tool": event["name"], "arguments": event["arguments"],
                                             "call_id": event["id"]}, "nanocode.tool.progress")
                    await write_sse(writer, chunk({"content": "\n\n"}))
        if run.status == "failed":
            await write_sse(writer, {"error": {"message": run.error, "type": "server_error"}})
        else:
            await write_sse(writer, chunk({}, "stop", usage=openai_usage(run.usage)))
        await write_sse(writer, "[DONE]")

    # --- POST /v1/responses ---

    async def create_response(self, data, writer, reader=None):
        prompt, history, instructions, images = api_prompt(data.get("input"), data.get("instructions"))
        previous, conversation = data.get("previous_response_id"), data.get("conversation")
        if isinstance(conversation, dict):
            conversation = conversation.get("id")
        if conversation is not None and not SESSION_ID_RE.match(str(conversation)):
            raise ApiError(400, "'conversation' must be 1-64 letters, digits, '.', '_' or '-'")
        if previous is None and conversation:
            previous = self.conversations.get(conversation)
        session_id = None
        if previous:
            stored = self.responses.get(previous)
            if stored is None:
                raise ApiError(404, f"no stored response '{previous}' to continue from", "not_found")
            session_id = stored["session_id"]
        elif history:
            prompt = api_with_history(prompt, history)
        run = self.start(prompt, session_id, instructions, images)
        rid = "resp_" + run.id[4:]

        def response(status):
            output = []
            for t in run.tools:
                output.append({"type": "function_call", "id": "fc_" + t["id"][5:], "call_id": t["id"], "name": t["name"],
                               "arguments": json.dumps(t["arguments"]), "status": "completed"})
                if t["output"] is not None:
                    output.append({"type": "function_call_output", "call_id": t["id"], "output": t["output"],
                                   "status": "completed"})
            if run.text or status == "completed":
                output.append({"type": "message", "id": "msg_" + run.id[4:], "role": "assistant", "status": status,
                               "content": [{"type": "output_text", "text": run.text, "annotations": []}]})
            usage = {"input_tokens": run.usage["input_tokens"], "output_tokens": run.usage["output_tokens"],
                     "total_tokens": run.usage["total_tokens"]}
            return {"id": rid, "object": "response", "created_at": run.created, "status": status, "model": "nanocode",
                    "output": output, "output_text": run.text, "usage": usage, "previous_response_id": previous,
                    "conversation": conversation, "session_id": run.session_id,
                    **({"error": {"message": run.error}} if run.error else {})}

        def remember(final):
            if data.get("store", True) and final["status"] == "completed":
                self.responses[rid] = {"session_id": run.session_id, "response": final}
                if conversation:
                    self.conversations[conversation] = rid
                self.save()

        async def stream():
            await start_sse(writer)
            await write_sse(writer, {"type": "response.created", "response": response("in_progress")}, "response.created")
            async for event in run.follow():
                kind = event["event"]
                if kind == "delta":
                    await write_sse(writer, {"type": "response.output_text.delta", "item_id": "msg_" + run.id[4:],
                                             "delta": event["text"]}, "response.output_text.delta")
                elif kind == "tool.started":
                    await write_sse(writer, {"type": "response.output_item.added", "item": {
                        "type": "function_call", "call_id": event["id"], "name": event["name"],
                        "arguments": json.dumps(event["arguments"]), "status": "in_progress"}},
                        "response.output_item.added")
                elif kind == "tool.completed":
                    await write_sse(writer, {"type": "response.output_item.done", "item": {
                        "type": "function_call_output", "call_id": event["id"], "output": event["output"],
                        "status": "completed"}}, "response.output_item.done")
            status = "completed" if run.status == "completed" else "failed" if run.status == "failed" else "cancelled"
            final = response(status)
            remember(final)
            await write_sse(writer, {"type": f"response.{status}", "response": final}, f"response.{status}")

        if data.get("stream"):
            await self.streaming(reader, run, stream())
            return
        await self.wait_for_run(reader, run) if reader else await asyncio.shield(run.task)
        status = "completed" if run.status == "completed" else "failed" if run.status == "failed" else "cancelled"
        final = response(status)
        remember(final)
        await write_json(writer, 500 if status == "failed" else 200, final)


async def api_switch_session(session_id):
    """Makes the API conversation's nanocode session the current chat: a saved one, or a new one."""
    if session_id and session_id == nc.st.session.get("id"):
        return
    nc.leave_current_chat()
    match = None
    if session_id and nc.sessions_dir().is_dir():
        path = nc.sessions_dir() / f"{session_id}.json"
        try:
            match = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, json.JSONDecodeError):
            match = None
        if match and match.get("backend") != nc.st.backend:
            raise ApiError(400, f"session '{session_id}' was made with another provider ({match.get('backend')})")
    if match:
        await nc.open_session(match)
    else:
        await nc.new_chat(announce=False)
        if session_id:
            nc.st.session["id"] = session_id  # the client named it: later requests continue it


def web_page(path):
    """The browser chat page's files (nanokit/web), or None without nanokit."""
    try:
        from nanokit import web
    except ImportError:
        return None
    return web.static(path)


def api_session(session_id):
    """A saved chat of this folder: its title and turns (user and assistant text, tool calls and results)."""
    data = read_session_file(session_id)
    try:
        from nanokit.web import session_turns
        turns = session_turns(data)
    except ImportError:
        turns = [{"role": m["role"], "text": message_text(m.get("content"))} for m in data.get("messages") or []
                 if m.get("role") in ("user", "assistant") and m.get("content")]
    return {"object": "nanocode.session", "id": data.get("id"), "title": data.get("title"),
            "created": data.get("created"), "updated": data.get("updated"), "model": data.get("model"),
            "backend": data.get("backend"), "turns": turns}


def api_session_id(value):
    if value in (None, ""):
        return None
    if not SESSION_ID_RE.match(str(value)):
        raise ApiError(400, "a session id must be 1-64 letters, digits, '.', '_' or '-'")
    return str(value)


# --- What the web page (and other apps) can do besides chatting ---

# Slash commands an API client may run. The rest need a terminal (/tab, /editor, /terminal-setup...), change the
# server itself (/provider, /update, /auto), or the page does them its own way (new chat, search, fork, export).
API_COMMANDS = {"/help", "/model", "/think", "/plan", "/build", "/undo", "/redo", "/rollback", "/compact", "/init",
                "/learn", "/recap", "/memory", "/skills", "/stats", "/cost", "/cron", "/permissions", "/agents",
                "/commands", "/hooks", "/plugins", "/mcp", "/env", "/keybinds"}
# Read-only API: these change nothing (or only the model, how hard it thinks, the mode).
READ_ONLY_COMMANDS = {"/help", "/model", "/think", "/plan", "/build", "/compact", "/recap", "/memory", "/skills",
                      "/stats", "/cost", "/cron", "/permissions", "/agents", "/commands", "/hooks", "/plugins",
                      "/mcp", "/env", "/keybinds", "/rollback"}
READ_ONLY_ARGS = {"/model": None, "/think": None, "/plan": None, "/build": None, "/stats": None,
                  "/skills": ("hub",), "/cron": ("list",), "/mcp": ("tools",)}  # None: any argument
LOCK_FREE_COMMANDS = {"/help", "/memory", "/skills", "/stats", "/stats all", "/cost", "/cron", "/cron list",
                      "/permissions", "/agents", "/commands", "/hooks", "/plugins", "/mcp", "/env", "/keybinds",
                      "/rollback", "/think", "/model"}


def custom_prompt_command(word):
    """A skill or a custom command (.nanocode/commands/*.md): both are a prompt for the model, like a message."""
    import nanokit
    if word[1:] in nc.list_skills() and word not in nc.COMMANDS and word not in nanokit.COMMANDS:
        return True
    from nanokit import commands
    return word in commands.OURS          # their !`shell` lines ask, like the bash tool


def web_command(value, read_only=None):
    """The command, if an API client may run it; else ApiError (400) saying why."""
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "input must be a command, like /memory")
    text = value.strip()
    read_only = (not nc.st.auto_approve) if read_only is None else read_only
    if text.startswith("#"):
        if read_only:
            raise ApiError(400, "saving to memory needs edits allowed (\"api\": {\"permissions\": \"auto\"})")
        return text
    word, _, arg = text.partition(" ")
    word, arg = nc.ALIASES.get(word, word), arg.strip()
    if word in ("/plan", "/build") and arg[:1] in ("/", "#"):
        web_command(arg, read_only)               # what runs after switching has to be allowed too
        return text
    if custom_prompt_command(word):
        return text
    if word not in API_COMMANDS:
        raise ApiError(400, f"{word} isn't available here (it needs the terminal)")
    if read_only:
        allowed = READ_ONLY_ARGS.get(word, ())
        if word not in READ_ONLY_COMMANDS or (arg and allowed is not None and arg.split()[0] not in allowed):
            raise ApiError(400, f"{text} changes things, and this API is read-only "
                                "(\"api\": {\"permissions\": \"auto\"} allows it)")
    return text


def api_commands():
    """Commands the client may run, with what they do (for the page's / menu)."""
    import nanokit
    out = []
    for name, description in {**nc.COMMANDS, **{n: c["description"] for n, c in nanokit.COMMANDS.items()}}.items():
        try:
            web_command(name)
        except ApiError:
            continue
        out.append({"name": name, "description": description})
    for name, skill in nc.list_skills().items():
        if not any(c["name"] == "/" + name for c in out):
            out.append({"name": "/" + name, "description": skill["description"], "skill": True})
    return sorted(out, key=lambda c: c["name"])


def api_workspace():
    """Everything the page shows around the chat: model and choices, mode, thinking, themes, commands."""
    from nanokit import modes
    theme = nc.st.config.get("theme")
    theme = theme if isinstance(theme, str) and theme in nc.THEMES else "opencode"
    return {"object": "nanocode.workspace", "version": nc.__version__, "folder": nc.tilde(os.getcwd()),
            "provider": nc.st.provider, "provider_label": nc.provider_label(), "model": nc.st.model,
            "models": [{"id": k, "description": v} for k, v in nc.model_choices().items()],
            "think": nc.think_level(), "think_levels": list(nc.THINK_LEVELS), "mode": modes.current_mode(),
            "permissions": "auto" if nc.st.auto_approve else "read-only", "theme": theme, "themes": nc.THEMES,
            "commands": api_commands()}


def read_session_file(session_id):
    if not SESSION_ID_RE.match(session_id):
        raise ApiError(400, "a session id must be 1-64 letters, digits, '.', '_' or '-'")
    try:
        return json.loads((nc.sessions_dir() / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ApiError(404, f"no saved session '{session_id}'", "not_found")


def api_export(session_id, kind):
    """A chat as Markdown or as its JSON file, for the page to download."""
    from nanokit import share
    if kind not in ("md", "json"):
        raise ApiError(400, "format is md or json")
    data = read_session_file(session_id)
    content = json.dumps(data, indent=1, ensure_ascii=False) if kind == "json" else share.to_markdown(data)
    return {"filename": f"{share.slug(data.get('title'))}.{kind}", "content": content}


def api_fork(session_id, fork_notes):
    """A copy of a chat under a new id, to try another direction (like /fork)."""
    from nanokit import sessions
    data = read_session_file(session_id)
    title = data.get("title") or "chat"
    now = time.time()
    fork = {**data, "id": nc.make_session_id(), "title": nc.short_title(f"fork of {title}"), "created": now,
            "updated": now, "sdk_session_id": None, "usage": {"input": 0, "output": 0, "cost": 0.0}}
    path = nc.sessions_dir() / f"{fork['id']}.json"
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(fork), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        raise ApiError(500, f"can't save the fork: {e}", "server_error")
    if data.get("backend") == "sdk":   # Claude Code's conversation can't be copied: its next message gets the screen
        fork_notes[fork["id"]] = sessions.screen_text(data.get("screen") or "")[-sessions.FORK_NOTE_LIMIT:]
    return {"object": "nanocode.session", "id": fork["id"], "title": fork["title"]}


async def api_search(query):
    """This folder's chats that mention the words, best first."""
    from nanokit import sessions
    if not query.strip():
        return {"object": "list", "data": []}
    here = str(os.getcwd())

    def find():
        sessions.update_index(everywhere=False)
        return sessions.search(query, 30, project=here)
    try:
        found = await asyncio.to_thread(find)
    except Exception as e:                 # sqlite3.Error, OSError: the search index can't be read
        raise ApiError(500, f"search failed: {e}", "server_error")
    return {"object": "list", "data": [{"id": r["id"], "title": r["title"], "updated": r["updated"],
                                        "snippet": r["snippet"]} for r in found]}


IMAGE_PARTS = ("image_url", "input_image", "image")
IMAGE_MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"),
               (b"GIF89a", "image/gif")]


def content_parts(content):
    """(text, images) of an OpenAI message content: a string, or a list of text and image parts.
    Images are [(mime type, bytes)]."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise ApiError(400, "message content must be a string or a list of text and image parts")
    texts, images = [], []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
            texts.append(part.get("text") or "")
        elif isinstance(part, dict) and part.get("type") in IMAGE_PARTS:
            images.append(decode_image(part))
        elif isinstance(part, dict):
            raise ApiError(400, f"'{part.get('type')}' content isn't supported: send text and images",
                           "unsupported_content_type")
    return "\n".join(texts), images


def message_text(content):
    """Text of a message from earlier in the chat; its images become "[image]"."""
    if isinstance(content, list):
        content = [{"type": "text", "text": "[image]"} if isinstance(p, dict) and p.get("type") in IMAGE_PARTS else p
                   for p in content]
    return content_parts(content)[0]


def decode_image(part):
    """(mime, bytes) from an image part: {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
    the Responses API's {"type": "input_image", "image_url": "data:..."}, or Anthropic's base64 "source"."""
    source = part.get("source")
    if isinstance(source, dict):
        url = source.get("data") or source.get("url") or ""
    else:
        url = part.get("image_url") or part.get("url") or ""
        if isinstance(url, dict):
            url = url.get("url") or ""
    if not isinstance(url, str) or not url:
        raise ApiError(400, "an image part needs its data: {\"url\": \"data:image/png;base64,...\"}")
    if url.startswith(("http://", "https://")):
        raise ApiError(400, "send images as base64 (data:image/png;base64,...); image URLs aren't downloaded",
                       "unsupported_content_type")
    if url.startswith("data:"):
        header, _, url = url.partition(",")
        if ";base64" not in header:
            raise ApiError(400, "image data URLs must be base64: data:image/png;base64,...")
    try:
        data = base64.b64decode("".join(url.split()), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "the image isn't valid base64")
    if len(data) > API_MAX_IMAGE:
        raise ApiError(413, f"an image can be at most {API_MAX_IMAGE // 1024 // 1024} MB")
    mime = image_type(data)
    if mime is None:
        raise ApiError(400, "images must be PNG, JPEG, WebP or GIF", "unsupported_content_type")
    return mime, data


def image_type(data):
    """The real type, from the bytes: clients often say image/png for any image."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return next((mime for magic, mime in IMAGE_MAGIC if data.startswith(magic)), None)


def api_prompt(value, instructions=None):
    """(prompt, earlier turns, system instructions, images) from a Responses/Runs `input`: a string or a list
    of messages; the images are the last user message's."""
    if isinstance(value, str) and value.strip():
        return value, [], instructions, []
    if isinstance(value, list) and value:
        messages = [m for m in value if isinstance(m, dict) and m.get("type", "message") == "message"]
        system = [message_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")]
        turns = [m for m in messages if m.get("role") in ("user", "assistant")]
        if turns and turns[-1].get("role") == "user":
            instructions = "\n\n".join(filter(None, [instructions, *system])) or None
            prompt, images = content_parts(turns[-1].get("content"))
            history = [(m.get("role"), message_text(m.get("content"))) for m in turns[:-1]]
            return prompt, history, instructions, images
    raise ApiError(400, "'input' must be a non-empty string, or a list of messages ending with one from the user")


def api_with_history(prompt, history):
    if not history:
        return prompt
    lines = [f"{'User' if role == 'user' else 'You (assistant)'}: {text}" for role, text in history if text]
    return "Our conversation so far:\n\n" + "\n\n".join(lines) + "\n\n---\n\nMy new message:\n" + prompt


def openai_usage(usage):
    return {"prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"]}


def api_capabilities():
    return {"object": "nanocode.api.capabilities", "platform": "nanocode", "version": nc.__version__, "model": "nanocode",
            "backend_model": nc.st.model, "provider": nc.st.provider,
            "auth": {"type": "bearer", "required": True},
            "permissions": "auto" if nc.st.auto_approve else "read-only",
            "features": {"chat_completions": True, "chat_completions_streaming": True, "responses_api": True,
                         "responses_streaming": True, "previous_response_id": True, "conversations": True,
                         "run_submission": True, "run_status": True, "run_events_sse": True, "run_stop": True,
                         "skills": True, "tools": True, "images": True, "concurrent_runs": 1,
                         "sessions": True, "web_ui": True, "commands": True, "search": True, "fork": True,
                         "export": True, "workspace": True},
            "session_header": "X-Nanocode-Session-Id",
            "endpoints": {"chat_completions": "POST /v1/chat/completions", "responses": "POST /v1/responses",
                          "response": "GET|DELETE /v1/responses/{id}", "runs": "POST /v1/runs",
                          "run": "GET /v1/runs/{id}", "run_events": "GET /v1/runs/{id}/events",
                          "run_stop": "POST /v1/runs/{id}/stop", "models": "GET /v1/models",
                          "skills": "GET /v1/skills", "tools": "GET /v1/tools", "health": "GET /health",
                          "sessions": "GET /v1/sessions", "session": "GET /v1/sessions/{id}",
                          "export": "GET /v1/sessions/{id}/export?format=md|json",
                          "fork": "POST /v1/sessions/{id}/fork", "search": "GET /v1/search?q=",
                          "workspace": "GET /v1/workspace", "web": "GET /"}}


def parse_json(body):
    try:
        data = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "the body must be JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "the body must be a JSON object")
    return data


async def read_http_request(reader):
    """Reads one request; a client that sends too slowly or too many headers is cut off."""
    method, path, headers = await asyncio.wait_for(read_http_head(reader), API_HEADER_TIMEOUT)
    length = int(headers.get("content-length") or 0)
    if length > API_MAX_BODY:
        raise ApiError(413, "request body too large")
    body = await asyncio.wait_for(reader.readexactly(length), API_BODY_TIMEOUT) if length else b""
    return method.upper(), path, headers, body


async def read_http_head(reader):
    line = await reader.readline()
    if not line:
        raise ConnectionError("closed")
    try:
        method, path, _ = line.decode("latin-1").split(" ", 2)
    except ValueError:
        raise ApiError(400, "bad request line")
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
        if len(headers) > API_MAX_HEADERS:
            raise ApiError(431, "too many headers")
    try:
        int(headers.get("content-length") or 0)
    except ValueError:
        raise ApiError(400, "bad Content-Length")
    return method, path, headers


def http_head(status, content_type, extra=None, length=None):
    lines = [f"HTTP/1.1 {status} {HTTP_REASONS.get(status, 'OK')}", f"Content-Type: {content_type}",
             "Connection: close", "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer"]
    if length is not None:
        lines.append(f"Content-Length: {length}")
    lines += [f"{k}: {v}" for k, v in (extra or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def write_json(writer, status, obj, extra=None):
    body = json.dumps(obj).encode()
    writer.write(http_head(status, "application/json", extra, len(body)) + body)
    await writer.drain()


async def start_sse(writer, extra=None):
    writer.write(http_head(200, "text/event-stream", {"Cache-Control": "no-cache", **(extra or {})}))
    await writer.drain()


async def write_sse(writer, data, event=None):
    payload = data if isinstance(data, str) else json.dumps(data)
    writer.write(((f"event: {event}\n" if event else "") + f"data: {payload}\n\n").encode())
    await writer.drain()


def api_settings(host=None, port=None):
    """(host, port, key): flags, then NANOCODE_API_* env vars, then "api" in the config. Makes a key if there's none."""
    cfg = nc.st.config.get("api") or {}
    host = host or os.environ.get("NANOCODE_API_HOST") or cfg.get("host") or "127.0.0.1"
    port = int(port or os.environ.get("NANOCODE_API_PORT") or cfg.get("port") or API_DEFAULT_PORT)
    key = os.environ.get("NANOCODE_API_KEY") or cfg.get("key")
    if not key:
        key = secrets.token_hex(24)
        nc.st.config["api"] = {**cfg, "key": key}
        nc.save_config()
    return host, port, key


def say(text):
    """A line for the person running the server (no colors when it goes to a log file)."""
    nc.headless_write(sys.stderr, text + "\n")


async def serve(module, host=None, port=None):
    global nc
    nc = module
    host, port, key = api_settings(host, port)
    if len(key) < 8:
        say(f"{nc.RED}⏺ the API key must be at least 8 characters{nc.RESET}")
        return 1
    nc.st.headless = nc.st.serving = True
    # Edits and commands are allowed by default only on this machine; reachable from others, they have to be
    # turned on explicitly with "permissions": "auto".
    default = "auto" if host in LOCAL_HOSTS else "read-only"
    nc.st.auto_approve = (nc.st.config.get("api") or {}).get("permissions", default) == "auto"
    nc.new_session()
    if nc.st.backend == "sdk":
        nc.st.client = await nc.make_client()
    api = ApiServer(key)
    try:
        server = await asyncio.start_server(api.handle, host, port)
    except OSError as e:
        say(f"{nc.RED}⏺ can't listen on {host}:{port}: {e}{nc.RESET}")
        return 1
    shown = key if (os.environ.get("NANOCODE_API_KEY") is None and sys.stderr.isatty()) else key[:4] + "…"
    say(f"{nc.GREEN}⏺ nanocode API{nc.RESET} on http://{host}:{port}/v1  {nc.DIM}·  {nc.pretty_model(nc.st.model)} · "
          f"{nc.provider_label()} · {os.getcwd()}{nc.RESET}\n"
          f"  {nc.DIM}key:{nc.RESET} {shown}  {nc.DIM}(Authorization: Bearer <key>; in ~/.nanocode/config.json under \"api\"){nc.RESET}\n"
          f"  {nc.DIM}edits and commands: {'allowed' if nc.st.auto_approve else 'denied (read-only)'}; "
          f"dangerous commands and sending email are always denied · Ctrl+C stops{nc.RESET}")
    if host not in LOCAL_HOSTS:
        say(f"  {nc.YELLOW}listening beyond this machine over plain HTTP: put it behind HTTPS"
            f"{'; anyone with the key can run commands here' if nc.st.auto_approve else ''}{nc.RESET}")
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await nc.stop_all_shells()
        if nc.st.rea:
            await nc.st.rea.close()
        await nc.close_mcp()
        if nc.st.client:
            await nc.st.client.disconnect()
    return 0
