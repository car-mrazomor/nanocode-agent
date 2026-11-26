"""`nanocode acp`: nanocode as an Agent Client Protocol agent, for editors like Zed.

ACP (https://agentclientprotocol.com) is JSON-RPC 2.0 over stdio, one JSON message per line. The editor starts
`nanocode acp`, opens sessions and sends prompts; nanocode answers with session/update notifications: the reply
as it's written, each tool call and its result, and the plan (the todo tool). When nanocode would ask before an
edit or a command, it asks the editor instead (session/request_permission).

Each ACP session is a nanocode chat in its own tab (not shown anywhere): saved like any other chat, so it can be
loaded again (session/load) or opened in the app with /resume.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

import nanocode as nc
from nanokit import cli, on

PROTOCOL_VERSION = 1
SERVER = None  # the running AcpServer, if any

TOOL_KINDS = {"read": "read", "write": "edit", "edit": "edit", "bash": "execute", "shell": "execute",
              "glob": "search", "grep": "search", "web_search": "search", "session_search": "search",
              "web_fetch": "fetch", "todo": "think", "agent": "think"}
PLAN_STATUS = {"pending": "pending", "in_progress": "in_progress", "done": "completed"}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AcpSession:
    def __init__(self, sid, cwd):
        self.id, self.cwd = sid, cwd
        self.tab = nc.Tab(model=nc.st.model)
        self.task = None
        self.running = {}  # tool call id -> name, of calls not finished yet
        self.permissions = set()  # futures of permission questions waiting for the editor
        self.cancelled = False  # session/cancel stopped the current prompt


def file_uri_path(uri):
    """A local path from a file:// URI (Windows drive letters included), or None."""
    parsed = urlparse(uri or "")
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def prompt_text(blocks):
    """(text, images) from ACP content blocks: text, files (as @mentions or inline), images."""
    parts, images = [], []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "resource_link":
            path = file_uri_path(block.get("uri"))
            parts.append(nc.mention(path) if path else block.get("uri") or block.get("name") or "")
        elif kind == "resource":
            res = block.get("resource") or {}
            path = file_uri_path(res.get("uri")) or res.get("uri") or "context"
            if "text" in res:
                parts.append(f'\n<file path="{path}">\n{nc.cap(res.get("text") or "", 100_000)}\n</file>\n')
            else:
                parts.append(nc.mention(path) if file_uri_path(res.get("uri")) else "")
        elif kind == "image" and block.get("data"):
            mime = block.get("mimeType") or "image/png"
            if mime in nc.IMAGE_TYPES:
                try:
                    images.append(nc.save_image(base64.b64decode(block["data"]), mime))
                except (binascii.Error, ValueError, OSError):
                    pass
    text = " ".join(p for p in parts if p and not p.startswith("\n")).strip()
    inline = "".join(p for p in parts if p.startswith("\n"))
    return text + inline, images


def tool_title(name, args):
    key = nc.TOOL_PREVIEW.get(name)
    preview = " ".join(str(args.get(key, "")).split()) if key and isinstance(args, dict) else ""
    return f"{name} {preview[:80]}".strip()


def tool_locations(args):
    path = args.get("path") if isinstance(args, dict) else None
    return [{"path": str(nc.resolve(path))}] if isinstance(path, str) and path else []


class AcpServer:
    def __init__(self, write):
        self.write = write  # sends one message (a dict)
        self.sessions = {}
        self.waiting = {}  # id of our request -> future of its answer
        self.next_id = 0
        self.tasks = set()

    # --- JSON-RPC plumbing ---

    def notify(self, method, params):
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    def update(self, session, update):
        self.notify("session/update", {"sessionId": session.id, "update": update})

    async def request(self, method, params):
        self.next_id += 1
        rid = self.next_id                     # other requests may be sent while this one waits
        future = asyncio.get_running_loop().create_future()
        self.waiting[rid] = future
        self.write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await future
        finally:
            self.waiting.pop(rid, None)

    def receive(self, message):
        """One message from the editor. Requests run as tasks, so answers to ours keep arriving meanwhile."""
        if not isinstance(message, dict):
            return
        if "method" not in message:            # an answer to one of our requests
            future = self.waiting.pop(message.get("id"), None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(RpcError(-32000, str((message["error"] or {}).get("message"))))
                else:
                    future.set_result(message.get("result"))
            return
        task = asyncio.ensure_future(self.dispatch(message))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def dispatch(self, message):
        mid, method, params = message.get("id"), message["method"], message.get("params") or {}
        handler = getattr(self, "on_" + method.replace("/", "_"), None)
        try:
            if handler is None:
                raise RpcError(-32601, f"method not found: {method}")
            result = await handler(params)
        except RpcError as e:
            if mid is not None:
                self.write({"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e)}})
            return
        except Exception as e:
            nc.log_error(e)
            if mid is not None:
                self.write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})
            return
        if mid is not None:
            self.write({"jsonrpc": "2.0", "id": mid, "result": result})

    def session(self, params):
        session = self.sessions.get(params.get("sessionId"))
        if session is None:
            raise RpcError(-32602, f"no session {params.get('sessionId')!r}")
        return session

    def session_for(self, tab):
        return next((s for s in self.sessions.values() if s.tab is tab), None)

    # --- methods ---

    async def on_initialize(self, params):
        return {"protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": True,
                                      "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True}},
                "agentInfo": {"name": "nanocode", "title": "nanocode", "version": nc.__version__},
                "authMethods": []}

    async def on_authenticate(self, params):
        return {}

    def enter(self, cwd):
        """Goes to a session's folder: nanocode works in the current folder (chats, memory, tools), which is
        process-wide, so it only moves while no session's turn is running."""
        if cwd and Path(cwd).is_dir() and Path(cwd).resolve() != Path.cwd().resolve():
            busy = [s for s in self.sessions.values() if s.task and not s.task.done()]
            if busy:
                raise RpcError(-32000, f"another session is working in {Path.cwd()}; nanocode works in one folder "
                                       f"at a time, so try again when its turn ends")
            os.chdir(cwd)

    async def on_session_new(self, params):
        self.enter(params.get("cwd"))
        session = AcpSession(None, str(Path.cwd()))
        with nc.for_tab(session.tab):
            nc.new_session()
            sid = session.id = nc.st.session["id"]
        self.sessions[sid] = session
        return {"sessionId": sid}

    async def on_session_load(self, params):
        sid = str(params.get("sessionId") or "")
        self.enter(params.get("cwd"))
        path = nc.sessions_dir() / f"{Path(sid).name}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RpcError(-32602, f"no saved chat {sid!r} in {Path.cwd()}")
        if data.get("backend") != nc.st.backend:
            raise RpcError(-32602, f"that chat was made with another provider ({data.get('backend')})")
        session = self.sessions.get(sid) or AcpSession(sid, str(Path.cwd()))
        with nc.for_tab(session.tab):
            await nc.open_session(data)
        self.sessions[sid] = session
        self.replay(session, data)
        return None

    def replay(self, session, data):
        """Sends a loaded chat's history to the editor."""
        messages = data.get("messages") or []
        if not messages and data.get("screen"):
            from nanokit.sessions import screen_text
            self.update(session, {"sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text", "text": screen_text(data["screen"])}})
        for m in messages:
            text = m.get("content") if isinstance(m.get("content"), str) else ""
            if m.get("role") == "user" and text:
                self.update(session, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}})
            elif m.get("role") == "assistant":
                if text:
                    self.update(session, {"sessionUpdate": "agent_message_chunk",
                                          "content": {"type": "text", "text": text}})
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    self.update(session, {"sessionUpdate": "tool_call", "toolCallId": call.get("id") or "call",
                                          "title": tool_title(fn.get("name"), args),
                                          "kind": TOOL_KINDS.get(fn.get("name"), "other"), "status": "completed",
                                          "rawInput": args})

    async def on_session_prompt(self, params):
        session = self.session(params)
        if session.task and not session.task.done():
            raise RpcError(-32000, "this session is already answering a prompt")
        self.enter(session.cwd)
        text, images = prompt_text(params.get("prompt"))
        if not text.strip() and not images:
            return {"stopReason": "end_turn"}
        session.task = nc.in_tab(session.tab, self.turn(session, text, images))
        try:
            await asyncio.shield(session.task)
        except asyncio.CancelledError:
            if not session.task.cancelled():
                raise
            return {"stopReason": "cancelled"}
        error = session.tab.last_error
        if error:
            raise RpcError(-32000, error)
        return {"stopReason": "cancelled" if session.cancelled else "end_turn"}

    async def turn(self, session, text, images):
        session.cancelled = False

        def sink(kind, data):
            self.event(session, kind, data)
        token = nc.EVENTS.set(sink)
        nc.st.last_error = None
        try:
            await nc.send(nc.expand_mentions(text, images), display=text, images=images)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            nc.log_error(e)
            nc.st.last_error = str(e)
        finally:
            nc.EVENTS.reset(token)
            nc.stream_end()

    def event(self, session, kind, data):
        """nanocode's events of a turn (see nc.emit) as ACP session updates."""
        if kind == "delta":
            self.update(session, {"sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text", "text": data["text"]}})
        elif kind == "tool.started":
            session.running[data["id"]] = data["name"]
            self.update(session, {"sessionUpdate": "tool_call", "toolCallId": data["id"],
                                  "title": tool_title(data["name"], data["arguments"]),
                                  "kind": TOOL_KINDS.get(data["name"], "other"), "status": "in_progress",
                                  "rawInput": data["arguments"], "locations": tool_locations(data["arguments"])})
        elif kind == "tool.completed":
            session.running.pop(data["id"], None)
            self.update(session, {"sessionUpdate": "tool_call_update", "toolCallId": data["id"],
                                  "status": "failed" if data["is_error"] else "completed",
                                  "content": [{"type": "content",
                                               "content": {"type": "text", "text": data["output"]}}],
                                  "rawOutput": {"output": data["output"]}})

    async def on_session_cancel(self, params):
        session = self.sessions.get(params.get("sessionId"))
        if session is None:
            return None
        for future in list(session.permissions):
            if not future.done():
                future.set_result({"outcome": {"outcome": "cancelled"}})
        if session.task and not session.task.done():
            session.cancelled = True
            if nc.st.backend == "sdk" and session.tab.client and session.tab.sdk_streaming:
                try:
                    await session.tab.client.interrupt()
                    return None
                except Exception as e:
                    nc.log_error(e)
            session.task.cancel()
        return None

    # --- asking the editor ---

    async def ask(self, session, tool, preview):
        """Asks the editor whether a tool may run: 'allow' or 'deny'."""
        call_id = next((cid for cid, name in reversed(list(session.running.items())) if name == tool), None)
        call = {"toolCallId": call_id or "perm_" + os.urandom(6).hex(), "title": f"{tool} {preview[:200]}".strip(),
                "kind": TOOL_KINDS.get(tool, "other"), "status": "pending"}
        options = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                   {"optionId": "always", "name": f"Always allow {tool}", "kind": "allow_always"},
                   {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]
        future = asyncio.ensure_future(self.request("session/request_permission",
                                                    {"sessionId": session.id, "toolCall": call, "options": options}))
        session.permissions.add(future)
        try:
            answer = await future
        except RpcError:
            return "deny"
        finally:
            session.permissions.discard(future)
        outcome = (answer or {}).get("outcome") or {}
        choice = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
        if choice == "always":
            nc.st.always_allow.add(tool)
        return "allow" if choice in ("allow", "always") else "deny"


@on("permission")
async def permission(tool, preview):
    server = SERVER
    session = server.session_for(nc.st.current_tab()) if server else None
    if session is None:
        return None
    dangerous = (tool in ("bash", "shell") and bool(nc.DANGEROUS.search(preview))) or tool in nc.ALWAYS_ASK
    if dangerous or nc.st.auto_approve or tool in nc.st.always_allow:
        return None                            # nanocode decides: allowed, or (dangerous) always denied here
    return await server.ask(session, tool, preview)


@on("after_tool")
def send_plan(name, args, result, is_error):
    server = SERVER
    session = server.session_for(nc.st.current_tab()) if server and name == "todo" else None
    if session is not None:
        server.update(session, {"sessionUpdate": "plan", "entries": [
            {"content": t["text"], "priority": "medium", "status": PLAN_STATUS.get(t["status"], "pending")}
            for t in nc.st.todos]})
    return None


# --- stdio ---

async def serve(stdin=None, stdout=None):
    """Runs the agent on stdin/stdout until the editor closes stdin."""
    global SERVER
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()

    def reader():                              # a thread: reading stdin this way works everywhere (Windows too)
        while True:
            try:
                line = stdin.readline()
            except (OSError, ValueError):
                line = b""
            loop.call_soon_threadsafe(lines.put_nowait, line)
            if not line:
                return

    def write(message):
        stdout.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        stdout.flush()

    nc.st.headless = nc.st.serving = True
    nc.new_session()
    SERVER = server = AcpServer(write)
    threading.Thread(target=reader, daemon=True).start()
    try:
        while True:
            line = await lines.get()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            server.receive(message)
    finally:
        if server.tasks:                       # requests already received get a moment to answer (a quick one
            await asyncio.wait(list(server.tasks), timeout=2)   # like initialize must not be lost); turns stop
        for task in list(server.tasks):
            task.cancel()
        for session in server.sessions.values():
            if session.task and not session.task.done():
                session.task.cancel()
            with nc.for_tab(session.tab):
                nc.save_session()
                if session.tab.client:
                    try:
                        await session.tab.client.disconnect()
                    except Exception:
                        pass
        await nc.stop_all_shells()
        await nc.close_mcp()
        SERVER = None
    return 0


@cli("acp", "run as an Agent Client Protocol agent over stdio (Zed and other editors)")
def acp_cli(argv):
    parser = argparse.ArgumentParser(prog="nanocode acp", description="Agent Client Protocol server over stdio.")
    parser.add_argument("-m", "--model")
    parser.add_argument("--provider", choices=list(nc.PROVIDERS))
    parser.add_argument("-y", "--yes", action="store_true", help="allow edits and commands without asking")
    args = parser.parse_args(argv)
    if args.model:
        os.environ["MODEL"] = args.model
    error = nc.apply_config(args.provider)
    if error:
        print(f"nanocode acp: {error}", file=sys.stderr)
        return 1
    if args.yes:
        nc.st.auto_approve = True
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr                    # stdout is the protocol's: anything printed goes to stderr
    try:
        return asyncio.run(serve(sys.stdin.buffer, protocol_out))
    except KeyboardInterrupt:
        return 0
