"""LSP: type errors and lint from a language server, right after the model edits a file.

A small Language Server Protocol client: JSON-RPC over the server's stdin/stdout, each message framed with a
Content-Length header. The right server is started the first time a file of its language is written or edited
(pyright or pylsp for Python, typescript-language-server, gopls, rust-analyzer, clangd), only if it's
installed. After a write or edit the file is sent to it and its diagnostics are added to the tool result, so
the model sees "app.py:12:5 error: ..." and fixes it in the same turn. The `diagnostics` tool asks on demand.

Everything here has a time limit: a turn waits at most a few seconds for a server, a server that crashes (or
stops reading what we send, or sends something that isn't JSON) is stopped and restarted once, then left off.
Each server has its own reader and writer threads, so it doesn't depend on the event loop and a stuck pipe
can't hang a turn. Servers that run the project's own code (rust-analyzer runs build scripts and proc macros;
typescript-language-server loads the project's TypeScript and its plugins) start only in a project you trusted
(asked once per project, remembered in "trusted_projects"). Config: "lsp": false turns it off;
{"python": "pylsp"} picks the command for a language.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import nanocode as nc
from nanokit import on

WAIT = 3.0            # seconds an edit waits for diagnostics (including a server's start)
WRITE_TIMEOUT = 5.0   # seconds a message may take to go into the server's stdin before the server is killed
TOOL_WAIT = 10.0      # seconds the diagnostics tool waits
MAX_ERRORS, MAX_WARNINGS = 20, 5

# language -> (file extensions, server commands to try in order)
LANGUAGES = {
    "python": ({".py", ".pyi"}, [["pyright-langserver", "--stdio"], ["basedpyright-langserver", "--stdio"],
                                 ["pylsp"]]),
    "typescript": ({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"},
                   [["typescript-language-server", "--stdio"]]),
    "go": ({".go"}, [["gopls"]]),
    "rust": ({".rs"}, [["rust-analyzer"]]),
    "c": ({".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"}, [["clangd"]]),
}
LANGUAGE_IDS = {".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "typescriptreact",
                ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascriptreact",
                ".c": "c", ".h": "c", ".py": "python", ".pyi": "python", ".go": "go", ".rs": "rust"}
INSTALL = {"python": "npm i -g pyright, or pip install python-lsp-server",
           "typescript": "npm i -g typescript typescript-language-server", "go": "go install golang.org/x/tools/gopls@latest",
           "rust": "rustup component add rust-analyzer", "c": "install clangd (LLVM)"}
SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
RUNS_PROJECT_CODE = {"rust", "typescript"}   # servers that execute code from the repository

servers = {}   # language -> Server
broken = {}    # language -> why it's off for this session (crashed twice, couldn't start)
_lock = threading.Lock()
DECLINED = set()   # projects whose tools you said no to, this session


def project_trusted(root=None):
    value = nc.st.config.get("trusted_projects")
    return isinstance(value, list) and os.path.realpath(root or os.getcwd()) in value


async def trust_project(why):
    """True when this project's own code may run (formatters from its node_modules or .venv, language servers
    that run build scripts): trusted before, or you say so now (asked once per project, even with /auto, and
    remembered in "trusted_projects"). Without anyone to ask (headless), only a trusted project."""
    root = os.path.realpath(os.getcwd())
    if project_trusted(root):
        return True
    if root in DECLINED or nc.st.headless:
        return False
    if nc.st.permission_lock is None:
        nc.st.permission_lock = asyncio.Lock()
    async with nc.st.permission_lock:
        if project_trusted(root) or root in DECLINED:
            return project_trusted(root)
        if not await nc.ask_permission("project_tools", f"trust {nc.tilde(root)}? {why}", False):
            DECLINED.add(root)
            return False
        value = nc.st.config.get("trusted_projects")
        nc.st.config["trusted_projects"] = (list(value) if isinstance(value, list) else []) + [root]
        try:
            nc.save_config()
        except OSError as e:
            nc.log_error(e)
    return True


def needs_trust(language):
    return language in RUNS_PROJECT_CODE and not project_trusted()


def language_of(path):
    ext = Path(path).suffix.lower()
    return next((lang for lang, (exts, _) in LANGUAGES.items() if ext in exts), None)


def command_for(language):
    """The server command for a language: the user's choice, or the first one installed."""
    setting = nc.st.config.get("lsp", True)
    if setting is False:
        return None
    if isinstance(setting, dict) and language in setting:
        custom = setting[language]
        if not custom:
            return None
        argv = custom if isinstance(custom, list) else shlex.split(str(custom), posix=os.name != "nt")
        found = shutil.which(argv[0]) if argv else None
        return [found] + [str(a) for a in argv[1:]] if found else None
    for argv in LANGUAGES[language][1]:
        found = shutil.which(argv[0])
        if found:
            return [found] + argv[1:]
    return None


def uri(path):
    return Path(os.path.abspath(path)).as_uri()


def _key(path):
    """How a file is known in `diagnostics`: servers may send its URI spelled differently (C%3A, /private/tmp)."""
    return os.path.normcase(os.path.realpath(path))


def from_uri(text):
    parsed = urlparse(text)
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]                                    # /C:/x -> C:/x
    return path


class Server:
    """One running language server, spoken to over stdio from a reader thread."""

    def __init__(self, language, argv, root):
        self.language, self.argv, self.root = language, argv, root
        self.name = Path(argv[0]).stem.replace("-langserver", "").replace("-language-server", "")
        self.proc = None
        self.ready = threading.Event()
        self.changed = threading.Condition()
        self.next_id = 0
        self.pending = {}             # request id -> [Event, result]
        self.diagnostics = {}         # _key(path) -> list of diagnostics
        self.published = {}           # _key(path) -> how many times diagnostics came for it
        self.versions = {}            # _key(path) -> version we sent
        self.write_lock = threading.Lock()
        self.outbox = queue.Queue()   # (bytes, Event) for the writer thread
        self.closed = None            # why the connection is over (the reader stopped), or None
        self.error = None

    # --- wire ---

    def start(self):
        """Starts the process and the initialize handshake in the background; `ready` is set when it's done."""
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=self.root,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, daemon=True, name=f"lsp-{self.name}").start()
        threading.Thread(target=self._write, daemon=True, name=f"lsp-{self.name}-writer").start()
        threading.Thread(target=self._initialize, daemon=True).start()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def kill(self, why):
        """Ends a server that stopped talking properly; whoever waits on it is woken (server_for restarts it)."""
        self.error = self.error or why
        self._close(why)
        try:
            self.proc.kill()
            self.proc.wait(2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _close(self, why):
        if self.closed is None:
            self.closed = why
        self.outbox.put(None)
        with self.changed:
            self.changed.notify_all()
        for waiter in list(self.pending.values()):
            waiter[2] = waiter[2] or why
            waiter[0].set()

    def _write(self):
        """The writer thread: a server that stops reading blocks this thread, never a turn."""
        while True:
            item = self.outbox.get()
            if item is None:
                return
            data, done = item
            try:
                if self.closed is None:
                    self.proc.stdin.write(data)
                    self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                self.error = self.error or f"writing to {self.name}: {e}"
                self._close(self.error)
            finally:
                done.set()

    def _send(self, message, wait=True):
        """Queues a message for the server. With `wait`, waits (a few seconds at most) until it's written, and
        kills a server that doesn't take it in time."""
        if self.closed is not None:
            raise OSError(f"{self.name} is gone: {self.closed}")
        body = json.dumps(dict(message, jsonrpc="2.0")).encode()
        done = threading.Event()
        self.outbox.put((b"Content-Length: %d\r\n\r\n" % len(body) + body, done))
        if not wait:
            return
        if not done.wait(WRITE_TIMEOUT):
            self.kill(f"{self.name} stopped reading its input")
            raise TimeoutError(f"{self.name} stopped reading its input")
        if self.closed is not None:
            raise OSError(f"{self.name} is gone: {self.closed}")

    def request(self, method, params, timeout):
        with self.write_lock:
            self.next_id += 1
            rid = self.next_id
        waiter = self.pending[rid] = [threading.Event(), None, None]   # done, result, error
        try:
            if self.closed is not None:
                raise OSError(f"{self.name} is gone: {self.closed}")
            self._send({"id": rid, "method": method, "params": params})
            if not waiter[0].wait(timeout):
                raise TimeoutError(f"{self.name} didn't answer {method} in {timeout:g}s")
        finally:
            self.pending.pop(rid, None)
        if waiter[2]:
            raise OSError(f"{self.name} didn't answer {method}: {waiter[2]}")
        return waiter[1]

    def notify(self, method, params):
        self._send({"method": method, "params": params})

    def _read(self):
        stream = self.proc.stdout
        why = "the server exited"
        try:
            while True:
                length = None
                while True:
                    line = stream.readline()
                    if not line:
                        return                                     # the server exited
                    line = line.strip()
                    if not line:
                        break
                    key, _, value = line.decode("ascii", "replace").partition(":")
                    if key.lower() == "content-length":
                        length = int(value.strip())
                if length is None:
                    continue
                msg = json.loads(stream.read(length))
                if not isinstance(msg, dict):
                    raise ValueError(f"not a JSON-RPC message: {str(msg)[:80]}")
                self._handle(msg)
        except Exception as e:                             # bad JSON or framing: we can't follow it any more
            why = self.error = f"{self.name} sent something unreadable ({type(e).__name__}: {e})"
        finally:
            self._close(why)
            if self.alive():                              # it's still running but we stopped listening
                try:
                    self.proc.kill()
                except OSError:
                    pass

    def _handle(self, msg):
        if "id" in msg and "method" in msg:                      # a request from the server: answer simply
            method = msg["method"]
            result = [None] * len((msg.get("params") or {}).get("items") or []) \
                if method == "workspace/configuration" else None
            self._send({"id": msg["id"], "result": result}, wait=False)   # the reader mustn't wait on the writer
        elif "id" in msg:
            waiter = self.pending.get(msg["id"])
            if waiter:
                waiter[1] = msg.get("result")
                waiter[0].set()
        elif msg.get("method") == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            key = _key(from_uri(params.get("uri", "")))
            with self.changed:
                self.diagnostics[key] = params.get("diagnostics") or []
                self.published[key] = self.published.get(key, 0) + 1
                self.changed.notify_all()

    def _initialize(self):
        root = Path(self.root)
        try:
            self.request("initialize", {
                "processId": os.getpid(), "rootUri": root.as_uri(), "rootPath": str(root),
                "workspaceFolders": [{"uri": root.as_uri(), "name": root.name or str(root)}],
                "capabilities": {"textDocument": {"synchronization": {"didSave": True},
                                                  "publishDiagnostics": {"relatedInformation": False}},
                                 "workspace": {"configuration": True, "workspaceFolders": True}},
                "clientInfo": {"name": "nanocode", "version": nc.__version__}}, timeout=30)
            self.notify("initialized", {})
            self.ready.set()
        except Exception as e:
            self.error = str(e)

    # --- documents ---

    def check(self, path, timeout):
        """Sends the file's current text and waits for its diagnostics. Returns them, or None on timeout."""
        deadline = time.monotonic() + timeout
        while not self.ready.is_set() and self.closed is None and time.monotonic() < deadline:
            self.ready.wait(min(0.1, max(0.0, deadline - time.monotonic())))
        if not self.ready.is_set() or not self.alive() or self.closed is not None:
            return None
        key, address = _key(path), uri(path)
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        with self.changed:
            seen = self.published.get(key, 0)
        version = self.versions.get(key, 0) + 1
        self.versions[key] = version
        language_id = LANGUAGE_IDS.get(Path(path).suffix.lower(), "cpp" if self.language == "c" else self.language)
        try:
            if version == 1:
                self.notify("textDocument/didOpen", {"textDocument": {"uri": address, "languageId": language_id,
                                                                      "version": version, "text": text}})
            else:
                self.notify("textDocument/didChange", {"textDocument": {"uri": address, "version": version},
                                                       "contentChanges": [{"text": text}]})
            self.notify("textDocument/didSave", {"textDocument": {"uri": address}, "text": text})
        except (OSError, TimeoutError):
            return None                                   # it was killed: _check restarts it once
        with self.changed:
            while self.published.get(key, 0) == seen and self.alive() and self.closed is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.changed.wait(min(left, 0.2))
            if self.published.get(key, 0) == seen:
                return None
            got = self.published[key]
        # A server may publish twice (a quick pass, then a full one): take a short moment for a second one.
        with self.changed:
            self.changed.wait_for(lambda: self.published.get(key, 0) > got or not self.alive(),
                                  min(0.3, max(0.0, deadline - time.monotonic())))
            return list(self.diagnostics.get(key, []))

    def stop(self):
        if not self.alive():
            self._close("stopped")
            return
        try:
            if self.ready.is_set():
                self.request("shutdown", None, timeout=1)
                self.notify("exit", None)
        except Exception:
            pass
        try:
            self.proc.wait(1)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._close("stopped")


def server_for(path):
    """The running server for this file's language, started (or restarted once) if needed; None if there's
    none, with the reason in `broken`."""
    language = language_of(path)
    if language is None or language in broken or needs_trust(language):
        return None                                      # never starts a server that runs untrusted code
    with _lock:
        server = servers.get(language)
        if server is not None and server.root != os.getcwd():    # another project now: start fresh there
            server.stop()
            servers.pop(language, None)
            server = None
        if server is not None and server.alive() and server.closed is None:
            return server
        if server is not None and server.alive():        # killed for not talking properly, still exiting
            server.kill(server.closed or "restarting")
        argv = command_for(language)
        if argv is None:
            return None
        if server is not None:                           # it crashed: once more, then leave it off
            if getattr(server, "restarted", False):
                broken[language] = f"{server.name} keeps crashing"
                nc.log_error(RuntimeError(f"LSP {server.name} crashed twice; off for this session"))
                return None
        new = Server(language, argv, os.getcwd())
        new.restarted = server is not None
        try:
            new.start()
        except OSError as e:
            broken[language] = f"couldn't start {argv[0]}: {e}"
            return None
        if not servers:
            atexit.register(stop_all)
        servers[language] = new
        return new


def describe(path, diagnostics, server):
    """'LSP (pyright): 2 errors' and one line per problem, errors first; None when there's nothing to fix."""
    shown = {1: [], 2: []}
    for d in diagnostics:
        severity = d.get("severity") or 1
        if severity in shown:
            shown[severity].append(d)
    if not shown[1] and not shown[2]:
        return None
    name = nc.display_path(path)
    real_cwd = os.path.realpath(os.getcwd())
    if os.path.isabs(name) and name.startswith(real_cwd + os.sep):
        name = os.path.relpath(name, real_cwd)
    counts = [f"{len(v)} {SEVERITY[k]}{'s' if len(v) != 1 else ''}" for k, v in shown.items() if v]
    lines = [f"LSP ({server.name}): {', '.join(counts)}"]
    for severity, limit in ((1, MAX_ERRORS), (2, MAX_WARNINGS)):
        items = sorted(shown[severity], key=lambda d: ((d.get("range") or {}).get("start") or {}).get("line", 0))
        for d in items[:limit]:
            start = (d.get("range") or {}).get("start") or {}
            source = f" ({d['source']})" if d.get("source") and d.get("source") != server.name else ""
            message = " ".join(str(d.get("message", "")).split())[:300]
            lines.append(f"  {name}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1} "
                         f"{SEVERITY[severity]}: {message}{source}")
        if len(items) > limit:
            lines.append(f"  … and {len(items) - limit} more {SEVERITY[severity]}s")
    return "\n".join(lines)


def _check(path, timeout):
    server = server_for(path)
    if server is None:
        return None, None
    diagnostics = server.check(path, timeout)
    if diagnostics is None and (server.closed is not None or not server.alive()):   # it crashed: try once more
        server = server_for(path)
        diagnostics = server.check(path, timeout) if server else None
    return server, diagnostics


@on("after_tool")
async def after_edit(name, args, result, is_error):
    if name not in ("write", "edit") or is_error or not args.get("path") or nc.st.config.get("lsp") is False:
        return None
    path = os.path.abspath(nc.resolve(args["path"]))
    language = language_of(path)
    if language is None or not os.path.isfile(path) or language in broken or command_for(language) is None:
        return None
    if needs_trust(language) and not await trust_project(f"its {language} language server runs code from "
                                                         f"the project (build scripts, plugins)"):
        return None
    server, diagnostics = await asyncio.to_thread(_check, path, WAIT)
    text = describe(path, diagnostics or [], server) if server else None
    if text:
        nc.out(f"     {nc.fg('warning')}{text.splitlines()[0]}{nc.RESET}")
    return text


@nc.tool("diagnostics", "Errors and warnings from the language server (type checker, linter) for a file, or for "
         "every file checked so far when no path is given. Edits already report new problems; use this to check "
         "a file you haven't edited.",
         {"path": {"type": "string", "description": "file to check (optional)"}})
async def diagnostics_tool(args):
    if nc.st.config.get("lsp") is False:
        return "the language servers are off (\"lsp\": false in the config)"
    if args.get("path"):
        path = os.path.abspath(nc.resolve(args["path"]))
        if not os.path.isfile(path):
            raise nc.ToolError(f"{args['path']} not found")
        language = language_of(path)
        if language is None:
            return f"no language server for {Path(path).suffix or 'this kind of'} files"
        if language in broken:
            return f"the {language} language server is off: {broken[language]}"
        if command_for(language) is None:
            return f"no {language} language server is installed (install one: {INSTALL[language]})"
        if needs_trust(language) and not await trust_project(f"its {language} language server runs code from "
                                                             f"the project (build scripts, plugins)"):
            return (f"the {language} language server runs code from this project, which isn't trusted "
                    f"(\"trusted_projects\" in the config)")
        server, found = await asyncio.to_thread(_check, path, TOOL_WAIT)
        if found is None:
            return f"{server.name if server else 'the language server'} didn't report in time; try again shortly"
        return describe(path, found, server) or f"no errors or warnings in {nc.display_path(path)}"
    reports = []
    for server in list(servers.values()):
        for key, found in list(server.diagnostics.items()):
            text = describe(key, found, server)
            if text:
                reports.append(text)
    if not servers:
        return "no language server is running yet: pass a path to check a file"
    return "\n\n".join(reports) or "no errors or warnings in the files checked so far"


def stop_all():
    for server in list(servers.values()):
        server.stop()
    servers.clear()


@on("shutdown")
async def shutdown():
    await asyncio.to_thread(stop_all)


nc.READ_ONLY_TOOLS.add("diagnostics")
nc.AGENT_READ_ONLY.add("diagnostics")
nc.TOOL_PREVIEW["diagnostics"] = "path"
nc.TOOL_ICONS["diagnostics"] = "✱"
