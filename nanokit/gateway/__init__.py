"""The messaging gateway: talk to nanocode from Telegram, Discord, Slack, WhatsApp or Matrix (like Hermes Agent's).

`nanocode gateway run` runs nanocode headless in a folder and answers messages there. Each platform
conversation is its own nanocode chat (saved, so you can /resume it in the app), in its own tab, so several
conversations can work at once; within one conversation, messages wait their turn. Only people on the allow
list can talk to it; anyone else gets a code the owner can approve (`nanocode gateway approve <code>`, or
/approve <code> from an allowed account).

Each platform is a module here (telegram.py, discord.py, slack.py, whatsapp.py) with a Platform subclass.
The scheduler of nanokit/cron.py also runs inside the gateway.
"""
from __future__ import annotations

import asyncio
import getpass
import importlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

import nanocode as nc
from nanokit import cli

PLATFORMS = ("telegram", "discord", "slack", "whatsapp", "matrix")
GATEWAY_MAX_CHATS = 20  # conversations kept open (each may hold a Claude Code process); idle ones are closed
GATEWAY_MAX_QUEUED = 10  # messages waiting in one conversation
GATEWAY_TYPING_EVERY = 4  # seconds
GATEWAY_REFUSE_EVERY = 600  # tell an unknown user about the approval code at most this often
GATEWAY_LOG_MAX = 5_000_000
GATEWAY_CODE_TTL = 3600  # an approval code works for an hour
GATEWAY_MAX_PENDING = 20  # approval codes waiting at once (strangers can't fill the state file)
GATEWAY_MAX_DOWNLOAD = 20 * 1024 * 1024  # bytes per photo or voice message
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
GATEWAY_HELP = ("I'm nanocode, working in {folder}.\nSend a message to ask or give me a task. Commands:\n"
                "/new start a new chat\n/stop stop what I'm doing\n/status what I'm working on\n"
                "/cron scheduled tasks (/cron add every day at 8:00 | <prompt>)\n"
                "/approve <code> let someone talk to me\n(On Slack, start commands with ! instead of /.)")


def say(text):
    """A line in the gateway's log (stderr: the terminal, or gateway.log for the daemon)."""
    nc.headless_write(sys.stderr, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def settings():
    return nc.st.config.get("gateway") or {}


def platform_config(name):
    return settings().get(name) or {}


def platform_class(name):
    if name not in PLATFORMS:
        raise ValueError(f"unknown platform '{name}' (choose: {', '.join(PLATFORMS)})")
    module = importlib.import_module(f"{__name__}.{name}")
    return module.PLATFORM


def enabled_platforms():
    names = []
    for name in PLATFORMS:
        cfg = platform_config(name)
        if cfg and cfg.get("enabled", True) and platform_class(name).configured(cfg):
            names.append(name)
    return names


def gateway_dir():
    return nc.home_dir() / "gateway"


def pid_file():
    return gateway_dir() / "gateway.pid"


def log_path():
    return gateway_dir() / "gateway.log"


def state_file():
    return gateway_dir() / "state.json"


# --- Messages ---

class Incoming:
    """A message from a platform, in the same shape for all of them."""

    def __init__(self, platform, chat, user, text="", name=None, images=None, audio=None, thread=None):
        self.platform, self.chat, self.user = platform, str(chat), str(user)
        self.text, self.name = text or "", name or str(user)
        self.images = images or []  # [(mime, bytes)]
        self.audio = audio  # (bytes, ".ogg") for a voice message
        self.thread = thread  # Slack: replies go into this thread

    @property
    def key(self):
        """The conversation: a chat (or one thread of a channel)."""
        return f"{self.platform}:{self.chat}" + (f":{self.thread}" if self.thread else "")


def split_message(text, limit):
    """Splits a long reply into messages of at most `limit` characters, at blank lines or line ends when it
    can, closing and reopening a ``` code block that's cut in two."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    room = limit - 8                           # for a closing ``` added at a cut
    pieces = []
    for line in text.split("\n"):
        while len(line) > room:
            cut = line.rfind(" ", 0, room)
            cut = cut if cut > room // 2 else room
            pieces.append(line[:cut])
            line = line[cut:]
        pieces.append(line)
    chunks, lines, size, fence = [], [], 0, None

    def flush():
        body = "\n".join(lines).strip("\n")
        if body.strip():
            chunks.append(body + ("\n```" if fence else ""))
        return ([fence] if fence else []), (len(fence) + 1 if fence else 0)

    for line in pieces:
        if lines and (size + len(line) + 1 > room or (not line.strip() and not fence and size > limit * 0.75)):
            lines, size = flush()
        lines.append(line)
        size += len(line) + 1
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = None if fence else stripped
    fence = None
    flush()
    return chunks


# --- Platforms ---

class Platform:
    """One messaging service. A subclass receives messages in run() and passes them to gateway.handle()."""
    name = ""
    limit = 4000  # characters per message

    def __init__(self, cfg, gateway=None, http=None):
        self.cfg, self.gateway, self.http = cfg, gateway, http

    @staticmethod
    def configured(cfg):
        return False

    def client(self):
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=httpx.Timeout(30, read=70))
        return self.http

    async def close(self):
        if self.http is not None:
            await self.http.aclose()

    async def run(self):
        """Receives messages until cancelled; reconnects by itself. Raises GatewayError when it can't go on."""
        raise NotImplementedError

    async def post(self, chat, text, thread=None):
        """Sends one message (already split and formatted)."""
        raise NotImplementedError

    def format(self, text):
        return text

    async def send_text(self, chat, text, thread=None):
        for chunk in split_message(text, self.limit):
            await self.post(chat, chunk, thread)

    async def typing(self, chat):
        pass

    def may_download(self, user):
        """Attachments are downloaded only for people on the allow list (a stranger's 1 GB video isn't)."""
        allowed = getattr(self.gateway, "allowed", None)
        return allowed is None or allowed(self.name, user)

    async def download(self, url, headers=None, limit=GATEWAY_MAX_DOWNLOAD):
        """A file's bytes, streamed and stopped at `limit` (the size it says is checked first)."""
        async with self.client().stream("GET", url, headers=headers or {}, follow_redirects=True,
                                        timeout=120) as resp:
            if resp.status_code != 200:
                raise GatewayError(f"download failed: HTTP {resp.status_code}")
            try:
                size = int(resp.headers.get("content-length") or 0)
            except ValueError:
                size = 0
            if size > limit:
                raise GatewayError(f"the file is too big ({size // 1_000_000} MB)")
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > limit:
                    raise GatewayError("the file is too big")
        return bytes(data)


class GatewayError(Exception):
    pass


async def deliver(platform, chat, text):
    """Sends a message to a chat without the gateway running (scheduled tasks use it)."""
    cls = platform_class(platform)
    cfg = platform_config(platform)
    if not cls.configured(cfg):
        raise GatewayError(f"{platform} isn't set up: nanocode gateway setup")
    target = cls(cfg)
    try:
        await target.send_text(chat, text)
    finally:
        await target.close()


# --- The gateway ---

class Reply:
    """Collects a turn's events (nc.EVENTS): finished text goes out as it's written, tool calls are counted."""

    def __init__(self, sink):
        self.sink, self.current, self.tools, self.sent = sink, "", 0, []

    def emit(self, kind, data):
        if kind == "delta":
            self.current += data["text"]
        elif kind in ("text_end", "tool.started"):
            self.tools += kind == "tool.started"
            self.flush()

    def flush(self):
        text = nc.clean_reply(self.current).strip()
        self.current = ""
        if text:
            self.sent.append(asyncio.ensure_future(self.sink(text)))


class Conversation:
    def __init__(self, key, platform, chat, thread=None):
        self.key, self.platform, self.chat, self.thread = key, platform, chat, thread
        self.tab = nc.Tab(model=nc.st.active_tab.model)
        self.queue = []  # (text, images) waiting
        self.task = None  # the running turn
        self.worker = None
        self.opened = False
        self.send_lock = asyncio.Lock()
        self.used = self.started = time.time()

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    @property
    def target(self):
        """Where a scheduled task made in this chat delivers."""
        return f"{self.platform}:{self.chat}"


class Gateway:
    def __init__(self, platforms):
        self.platforms = {p.name: p for p in platforms}
        self.conversations = {}
        self.state = load_state()
        self.config_seen = config_mtime()

    # --- who may talk ---

    def allowed(self, platform, user):
        if str(user) in map(str, platform_config(platform).get("allowed_users") or []):
            return True
        mtime = config_mtime()
        if mtime != self.config_seen:          # changed on disk (approved from the command line meanwhile)
            self.config_seen = mtime
            disk = ((nc.read_config_file().get("gateway") or {}).get(platform) or {}).get("allowed_users") or []
            if str(user) in map(str, disk):
                nc.st.config = nc.load_config()
                return True
        return False

    def owner(self, platform, user):
        """Owners: the ids given in setup (`owners`), not people approved later with a code."""
        return str(user) in map(str, platform_config(platform).get("owners") or [])

    async def refuse(self, msg):
        pending = self.state.setdefault("pending", {})
        prune_pending(self.state)
        code = next((c for c, p in pending.items() if p["platform"] == msg.platform and p["user"] == msg.user), None)
        if code is None:
            if len(pending) >= GATEWAY_MAX_PENDING:
                say(f"{msg.platform} user {msg.user} ({msg.name}) asked to talk, but too many people are waiting")
                return
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            pending[code] = {"platform": msg.platform, "user": msg.user, "name": msg.name, "chat": msg.chat,
                             "time": time.time(), "told": 0}
        entry = pending[code]
        if time.time() - entry.get("told", 0) < GATEWAY_REFUSE_EVERY:
            return
        entry["told"] = time.time()
        save_state(self.state)
        say(f"{msg.platform} user {msg.user} ({msg.name}) asked to talk: approve with `nanocode gateway approve {code}`")
        platform = self.platforms.get(msg.platform)   # straight back: strangers don't get a chat of their own
        try:
            await platform.send_text(msg.chat, "Hi! I only talk to people my owner has approved. If you're one of "
                                               f"them, ask them to approve this code: {code}", msg.thread)
        except Exception as e:
            nc.log_error(e)

    async def approve(self, code):
        """Adds the person behind a code to the allow list and tells them. Returns what happened."""
        result = approve_code(code, self.state)
        if result.startswith("error"):
            return result
        entry = self.state.pop("approved_now", None)
        platform = self.platforms.get(entry["platform"]) if entry else None
        if platform:
            try:
                await platform.send_text(entry["chat"], "You're approved: I'm listening. Send /help to see what I can do.")
            except Exception as e:
                nc.log_error(e)
        return result

    # --- messages ---

    async def handle(self, msg):
        """A message from a platform."""
        try:
            await self._handle(msg)
        except Exception as e:
            nc.log_error(e)
            say(f"{msg.key}: {type(e).__name__}: {e}")

    async def _handle(self, msg):
        if not self.allowed(msg.platform, msg.user):
            await self.refuse(msg)
            return
        text = msg.text.strip()
        word, _, arg = text.partition(" ")
        word = word.split("@")[0].lower()
        if word[:1] == "!":
            word = "/" + word[1:]
        conv = self.conversation(msg)
        if word in ("/new", "/reset", "/clear"):
            await self.stop(conv)
            await self.in_tab(conv, nc.new_chat(announce=False))
            conv.opened = True
            self.remember(conv)
            return await self.reply(msg, "New chat started.")
        if word in ("/stop", "/cancel"):
            stopped = await self.stop(conv)
            return await self.reply(msg, "Stopped." if stopped else "Nothing is running.")
        if word == "/status":
            return await self.reply(msg, self.status(conv))
        if word in ("/help", "/start"):
            return await self.reply(msg, GATEWAY_HELP.format(folder=nc.tilde(Path.cwd())))
        if word == "/cron":
            from nanokit import cron
            owner, auto = self.owner(msg.platform, msg.user), settings().get("permissions") == "auto"
            return await self.reply(msg, await cron.cron_command(
                arg, deliver=conv.target, log=say, permissions="auto" if auto else "read-only",
                can_change=auto or owner, targets=None if owner else [conv.target, "file"]))
        if word == "/approve":
            if not self.owner(msg.platform, msg.user) and platform_config(msg.platform).get("owners"):
                return await self.reply(msg, "Only the owner can approve new people.")
            return await self.reply(msg, await self.approve(arg.strip()) if arg.strip() else pending_text(self.state))
        if msg.audio:
            heard = await self.hear(msg)
            if heard is None:
                return
            text = (f"(voice message) {heard}\n\n{text}" if text else f"(voice message) {heard}").strip()
        if not text and not msg.images:
            return
        if len(conv.queue) >= GATEWAY_MAX_QUEUED:
            return await self.reply(msg, "I have too many messages waiting; send /stop or try again later.")
        conv.queue.append((text or "What's in this image?", msg.images))
        if conv.busy:
            await self.reply(msg, f"(queued: I'll get to it after the current task, {len(conv.queue)} waiting)")
        if conv.worker is None or conv.worker.done():
            conv.worker = asyncio.ensure_future(self.work(conv))

    async def hear(self, msg):
        from nanokit import voice
        data, suffix = msg.audio
        folder = Path(tempfile.mkdtemp(prefix="nanocode-voice-"))
        path = folder / f"voice{suffix or '.ogg'}"
        try:
            path.write_bytes(data)
            return await voice.transcribe(path)
        except voice.VoiceError as e:
            await self.reply(msg, f"I couldn't understand the voice message: {e}")
            return None
        finally:
            try:
                path.unlink()
                folder.rmdir()
            except OSError:
                pass

    def conversation(self, msg):
        conv = self.conversations.get(msg.key)
        if conv is None:
            conv = Conversation(msg.key, msg.platform, msg.chat, msg.thread)
            self.conversations[msg.key] = conv
            self.forget_idle()
        conv.used = time.time()
        return conv

    def forget_idle(self):
        """Closes the least recently used idle conversations beyond GATEWAY_MAX_CHATS (they stay saved)."""
        idle = sorted((c for c in self.conversations.values() if not c.busy and not c.queue), key=lambda c: c.used)
        while len(self.conversations) > GATEWAY_MAX_CHATS and idle:
            conv = idle.pop(0)
            self.conversations.pop(conv.key, None)
            if conv.tab.client is not None:
                asyncio.ensure_future(conv.tab.client.disconnect())

    def in_tab(self, conv, coro):
        return nc.in_tab(conv.tab, coro)

    async def work(self, conv):
        while conv.queue:
            text, images = conv.queue.pop(0)
            await self.turn(conv, text, images)

    async def turn(self, conv, text, images):
        """Runs one message in the conversation's chat and sends the answer back."""
        from nanokit import cron
        reply = Reply(lambda part: self.say(conv, part))

        async def run():
            token, target = nc.EVENTS.set(reply.emit), cron.DELIVER_TO.set(conv.target)
            try:
                await self.open_chat(conv)
                nc.st.last_error = None
                names = [nc.save_image(data, mime) for mime, data in images if mime in nc.IMAGE_TYPES]
                note = f" [+{len(names)} image{'s' * (len(names) > 1)}]" if names else ""
                say(f"{conv.key}: {' '.join(text.split())[:80]}{note}")
                await nc.send(nc.expand_mentions(text, names), display=text, images=names)
            finally:
                nc.EVENTS.reset(token)
                cron.DELIVER_TO.reset(target)
                nc.stream_end()

        conv.started = time.time()
        conv.task = self.in_tab(conv, run())
        typing = asyncio.ensure_future(self.keep_typing(conv))
        try:
            await asyncio.wait([conv.task])
        finally:
            typing.cancel()
        reply.flush()
        if reply.sent:
            await asyncio.wait(reply.sent)     # what was written goes out before any note below
        if conv.task.cancelled():
            return
        error = conv.task.exception()
        if error is not None:
            nc.log_error(error)
            say(f"{conv.key}: error: {error}")
            await self.say(conv, f"Something went wrong: {error}")
        elif not reply.sent:
            await self.say(conv, "(done, nothing to add)" if reply.tools else "(no answer)")
        self.remember(conv)

    async def open_chat(self, conv):
        """The first turn of a conversation continues its saved chat (or starts one)."""
        if conv.opened:
            return
        from api import server
        server.nc = server.nc or nc
        session_id = self.state.get("chats", {}).get(conv.key)
        try:
            await server.api_switch_session(session_id)
        except server.ApiError:                # made with another provider: start over
            await server.api_switch_session(None)
        conv.opened = True

    def remember(self, conv):
        session_id = (conv.tab.session or {}).get("id")
        if session_id and self.state.setdefault("chats", {}).get(conv.key) != session_id:
            self.state["chats"][conv.key] = session_id
            save_state(self.state)

    async def keep_typing(self, conv):
        platform = self.platforms.get(conv.platform)
        while platform is not None:
            try:
                await platform.typing(conv.chat)
            except Exception:
                pass
            await asyncio.sleep(GATEWAY_TYPING_EVERY)

    async def say(self, conv, text):
        async with conv.send_lock:             # in order, one at a time
            platform = self.platforms.get(conv.platform)
            if platform is None:
                return
            try:
                await platform.send_text(conv.chat, text, conv.thread)
            except Exception as e:
                nc.log_error(e)
                say(f"{conv.key}: couldn't send the reply: {e}")

    async def reply(self, msg, text):
        await self.say(self.conversation(msg), text)

    async def stop(self, conv):
        """Stops the running turn and drops the waiting messages. True if something was running."""
        conv.queue.clear()
        if not conv.busy:
            return False
        task = conv.task
        if nc.st.backend == "sdk" and conv.tab.client is not None:
            try:
                await conv.tab.client.interrupt()
                await asyncio.wait([task], timeout=4)
            except Exception as e:
                nc.log_error(e)
        if not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=5)
        return True

    def status(self, conv):
        tab = conv.tab
        usage = tab.usage
        cost = f", ${usage['cost']:.4f}" if usage.get("cost") else ""
        state = f"working ({int(time.time() - conv.started)}s)" if conv.busy else "idle"
        from nanokit import cron
        jobs = cron.load_jobs()
        return (f"nanocode {nc.__version__} · {nc.pretty_model(tab.model or nc.st.model)} ({nc.provider_label()})\n"
                f"folder: {nc.tilde(Path.cwd())}\n"
                f"edits and commands: {'allowed' if nc.st.auto_approve else 'not allowed (read-only)'}\n"
                f"chat: {(tab.session or {}).get('title') or 'new'} · {state}"
                f"{f', {len(conv.queue)} waiting' if conv.queue else ''}\n"
                f"tokens: {nc.fmt_tokens(usage['input'])} in, {nc.fmt_tokens(usage['output'])} out{cost}\n"
                f"scheduled tasks: {len(jobs)} (/cron list)")

    # --- running ---

    async def run(self):
        from nanokit import cron
        tasks = {asyncio.ensure_future(p.run()): p for p in self.platforms.values()}
        scheduler = asyncio.ensure_future(cron.scheduler(log=say))
        try:
            while tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    platform = tasks.pop(task)
                    error = task.exception() if not task.cancelled() else None
                    say(f"{platform.name}: stopped{f': {error}' if error else ''}")
                    if error and not isinstance(error, GatewayError):
                        nc.log_error(error)
            say("no platform is running any more")
        finally:
            scheduler.cancel()
            for task in tasks:
                task.cancel()
            for conv in self.conversations.values():
                if conv.task and not conv.task.done():
                    conv.task.cancel()
            for platform in self.platforms.values():
                await platform.close()
            for conv in self.conversations.values():
                if conv.tab.client is not None:
                    try:
                        await conv.tab.client.disconnect()
                    except Exception:
                        pass


# --- State: chat → session, approval codes (~/.nanocode/gateway/state.json) ---

def load_state():
    try:
        data = json.loads(state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        gateway_dir().mkdir(parents=True, exist_ok=True)
        tmp = state_file().with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in state.items() if k != "approved_now"}, f, indent=1)
        tmp.replace(state_file())
    except OSError as e:
        nc.log_error(e)


def prune_pending(state):
    """Drops approval codes older than GATEWAY_CODE_TTL."""
    pending = state.get("pending") or {}
    for code in [c for c, p in pending.items() if time.time() - (p.get("time") or 0) > GATEWAY_CODE_TTL]:
        pending.pop(code)


def config_mtime():
    try:
        info = nc.config_file().stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return None


def approve_code(code, state=None):
    """Moves the person behind an approval code onto their platform's allow list (in the config)."""
    own = state is None
    if own:
        state = load_state()
    else:                                      # codes made by the running gateway are on disk too
        for c, p in load_state().get("pending", {}).items():
            state.setdefault("pending", {}).setdefault(c, p)
    prune_pending(state)
    entry = state.get("pending", {}).pop(code.strip().upper(), None)
    if entry is None:
        return f"error: no request with the code '{code}' (codes work for an hour)"
    nc.st.config = nc.load_config()            # the newest, so another nanocode's changes aren't lost
    gateway = dict(nc.st.config.get("gateway") or {})
    platform = dict(gateway.get(entry["platform"]) or {})
    users = [str(u) for u in platform.get("allowed_users") or []]
    if not users and not platform.get("owners"):   # the first person approved (you, after an empty setup list)
        platform["owners"] = [entry["user"]]
    if entry["user"] not in users:
        users.append(entry["user"])
    platform["allowed_users"] = users
    gateway[entry["platform"]] = platform
    nc.st.config["gateway"] = gateway
    nc.save_config()
    state["approved_now"] = entry
    save_state(state)
    return f"Approved {entry['name']} ({entry['platform']} user {entry['user']})."


def pending_text(state):
    prune_pending(state)
    pending = state.get("pending") or {}
    if not pending:
        return "Nobody is waiting for approval."
    return "Waiting for approval (/approve <code>):\n" + "\n".join(
        f"{code}  {p['platform']} {p['name']} ({p['user']})" for code, p in pending.items())


# --- The daemon: `nanocode gateway start|stop|status` ---

def read_pid():
    try:
        return int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259                          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_file():
    return gateway_dir() / "gateway.lock"


def try_lock(f):
    """A non-blocking exclusive lock on an open file. True if we got it."""
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, ImportError):
        return False


HELD = {"lock": None}  # the running gateway's open, locked gateway.lock


def hold_lock():
    """The running gateway holds gateway.lock for as long as it runs. False if another one holds it."""
    gateway_dir().mkdir(parents=True, exist_ok=True)
    f = open(lock_file(), "a+")
    if not try_lock(f):
        f.close()
        return False
    HELD["lock"] = f
    return True


def drop_lock():
    f, HELD["lock"] = HELD["lock"], None
    if f is not None:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()


def lock_held():
    """Is a gateway holding gateway.lock (so it's running)?"""
    if HELD["lock"] is not None:
        return True
    try:
        f = open(lock_file(), "a+")
    except OSError:
        return False
    try:
        if not try_lock(f):
            return True
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    except OSError:
        return False
    finally:
        f.close()


def looks_like_gateway(pid):
    """Without the lock (a gateway from an older nanocode): its command line says `gateway run`."""
    if os.name == "nt":
        return False
    try:
        line = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "nanocode" in line and "gateway run" in line


def running_pid():
    """The running gateway's pid, or None. A pid file left behind whose pid now belongs to another program
    doesn't count: the gateway holds gateway.lock while it runs."""
    pid = read_pid()
    if not pid or pid == os.getpid() or not pid_alive(pid):
        return None
    return pid if lock_held() or looks_like_gateway(pid) else None


def ask_folder(current):
    """The folder the gateway works in: asked until it's one that exists; saved as a full path."""
    default = current if current and os.path.isdir(os.path.expanduser(current)) else os.getcwd()
    while True:
        answer = nc.ask("Folder it works in (a path; Enter keeps the one shown)", default).strip()
        folder = os.path.abspath(os.path.expanduser(answer or default))
        if os.path.isdir(folder):
            return folder
        print(f"  {nc.YELLOW}{folder} isn't a folder; type the path of your project (or press Enter for "
              f"{nc.tilde(Path(default))}){nc.RESET}")


def workdir():
    folder = settings().get("workdir")
    return str(Path(folder).expanduser()) if folder else os.getcwd()


def start_daemon():
    pid = running_pid()
    if pid:
        print(f"  the gateway is already running (pid {pid}) · nanocode gateway status")
        return 0
    if not enabled_platforms():
        print(f"  {nc.RED}no platform is set up yet:{nc.RESET} nanocode gateway setup")
        return 1
    gateway_dir().mkdir(parents=True, exist_ok=True)
    log = log_path()
    if log.is_file() and log.stat().st_size > GATEWAY_LOG_MAX:
        log.replace(log.with_suffix(".log.old"))
    folder = workdir()
    if not os.path.isdir(folder):
        print(f"  {nc.RED}the gateway's folder doesn't exist:{nc.RESET} {folder}\n"
              f"  {nc.DIM}set the right one with: nanocode gateway setup (the last question){nc.RESET}")
        return 1
    argv = [sys.executable, str(Path(nc.__file__).resolve()), "gateway", "run"]
    options = {"start_new_session": True} if os.name != "nt" else \
        {"creationflags": 0x00000008 | 0x00000200 | 0x08000000}   # detached, own group, no window
    with open(log, "a", encoding="utf-8") as f:
        proc = subprocess.Popen(argv, cwd=folder, stdin=subprocess.DEVNULL, stdout=f, stderr=f,
                                env={**os.environ, "NANOCODE_NO_UPDATE_CHECK": "1"}, **options)
    for _ in range(50):
        if proc.poll() is not None or read_pid() == proc.pid:
            break
        time.sleep(0.1)
    if proc.poll() is not None:
        print(f"  {nc.RED}the gateway stopped right away:{nc.RESET}")
        print("\n".join("    " + line for line in tail(log, 8)))
        return 1
    print(f"  {nc.GREEN}⏺ gateway started{nc.RESET} (pid {proc.pid}) in {nc.tilde(Path(folder))} · "
          f"{', '.join(enabled_platforms())}\n  {nc.DIM}log: {nc.tilde(log)} · stop: nanocode gateway stop{nc.RESET}")
    return 0


def stop_daemon():
    pid = running_pid()
    if not pid:
        print("  the gateway isn't running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"  {nc.RED}couldn't stop it (pid {pid}): {e}{nc.RESET}")
        return 1
    for _ in range(100):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    try:
        pid_file().unlink()
    except OSError:
        pass
    print(f"  {nc.GREEN}⏺ gateway stopped{nc.RESET}")
    return 0


def tail(path, lines=10):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def show_status():
    pid = running_pid()
    state = f"{nc.GREEN}running{nc.RESET} (pid {pid})" if pid else f"{nc.DIM}not running{nc.RESET}"
    names = enabled_platforms()
    print(f"  gateway: {state}\n  folder: {nc.tilde(Path(workdir()))}\n"
          f"  platforms: {', '.join(names) or 'none (nanocode gateway setup)'}\n"
          f"  edits and commands: {'allowed' if settings().get('permissions') == 'auto' else 'read-only'}")
    for name in names:
        users = platform_config(name).get("allowed_users") or []
        print(f"  {name}: {len(users)} allowed user{'s' * (len(users) != 1)}")
    state = load_state()
    prune_pending(state)
    for code, p in (state.get("pending") or {}).items():
        print(f"  {nc.YELLOW}waiting for approval:{nc.RESET} {p['platform']} {p['name']} ({p['user']}) · "
              f"nanocode gateway approve {code}")
    if log_path().is_file():
        print(f"  log: {nc.tilde(log_path())}")
        for line in tail(log_path(), 5):
            print(f"    {nc.DIM}{line}{nc.RESET}")
    return 0


def run_foreground():
    folder = workdir()
    try:
        os.chdir(folder)
    except OSError as e:
        print(f"{nc.RED}⏺ can't work in {folder}: {e}{nc.RESET}", file=sys.stderr)
        return 1
    error = nc.apply_config()
    if error:
        print(f"{nc.RED}⏺ {error}{nc.RESET}", file=sys.stderr)
        return 1
    names = enabled_platforms()
    if not names:
        print(f"{nc.RED}⏺ no platform is set up yet: nanocode gateway setup{nc.RESET}", file=sys.stderr)
        return 1
    other = running_pid()
    if other:
        print(f"{nc.RED}⏺ the gateway is already running (pid {other}): nanocode gateway stop{nc.RESET}",
              file=sys.stderr)
        return 1
    if not hold_lock():
        print(f"{nc.RED}⏺ the gateway is already running: nanocode gateway stop{nc.RESET}", file=sys.stderr)
        return 1
    pid_file().write_text(str(os.getpid()))
    nc.st.headless = nc.st.serving = True
    nc.SURFACE = "gateway"
    nc.st.auto_approve = settings().get("permissions", "read-only") == "auto"
    say(f"nanocode gateway {nc.__version__} · {', '.join(names)} · {nc.pretty_model(nc.st.model)} · "
        f"{nc.provider_label()} · {folder} · edits and commands "
        f"{'allowed' if nc.st.auto_approve else 'denied (read-only)'} · Ctrl+C stops")

    def stop(*_):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, stop)
    except (OSError, ValueError):
        pass
    try:
        asyncio.run(serve(names))
    except KeyboardInterrupt:
        pass
    finally:
        if read_pid() == os.getpid():
            try:
                pid_file().unlink()
            except OSError:
                pass
        drop_lock()
        say("gateway stopped")
    return 0


async def serve(names):
    nc.new_session()
    platforms = [platform_class(name)(platform_config(name)) for name in names]
    gateway = Gateway(platforms)
    for platform in platforms:
        platform.gateway = gateway
    try:
        await gateway.run()
    finally:
        await nc.stop_all_shells()
        if nc.st.rea:
            await nc.st.rea.close()
        await nc.close_mcp()


# --- Setup ---

def ask_secret(label, current=None):
    keep = f" {nc.DIM}[Enter keeps {nc.redact(current, 'token')}]{nc.RESET}" if current else ""
    try:
        value = getpass.getpass(f"  {label}{keep}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)
    return value or current or ""


def ask_list(label, current):
    shown = ", ".join(map(str, current or []))
    value = nc.ask(label, shown)
    return [v for v in re.split(r"[\s,]+", value) if v]


def yes(question, default=False):
    answer = nc.ask(f"{question} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y")


def setup():
    """Asks for each platform's tokens and who may talk to nanocode, and saves them in the config."""
    print(f"\n  {nc.BOLD}nanocode gateway setup{nc.RESET} {nc.DIM}· talk to nanocode from your phone. Tokens are "
          f"saved in {nc.tilde(nc.config_file())} (only you can read it).{nc.RESET}")
    gateway = dict(settings())
    for name in PLATFORMS:
        cfg = dict(gateway.get(name) or {})
        cls = platform_class(name)
        done = cls.configured(cfg)
        if not yes(f"\n  {nc.BOLD}{name.capitalize()}{nc.RESET}{' (set up)' if done else ''}: set up now?", False):
            continue
        print(f"  {nc.DIM}{cls.SETUP_HELP}{nc.RESET}")
        for key, label in cls.SECRETS:
            cfg[key] = ask_secret(label, cfg.get(key))
        for key, label, default in getattr(cls, "SETTINGS", ()):
            cfg[key] = nc.ask(label, str(cfg.get(key) or default))
        if not cls.configured(cfg):
            print(f"  {nc.YELLOW}not all of it was given: skipped{nc.RESET}")
            continue
        try:
            note = asyncio.run(cls.check(cfg))
            print(f"  {nc.GREEN}✓{nc.RESET} {note}")
        except Exception as e:
            print(f"  {nc.RED}✗ {e}{nc.RESET}")
            if not yes("save it anyway?", False):
                continue
        cfg["allowed_users"] = ask_list(f"{cls.USER_HINT} allowed to talk to it (comma separated; empty: approve "
                                        "people with a code later)", cfg.get("allowed_users"))
        cfg["owners"] = list(cfg["allowed_users"])   # owners may manage scheduled tasks even when read-only
        cfg["enabled"] = True
        gateway[name] = cfg
    auto = yes("\n  Allow edits and commands from chats? (no: read-only, it can only look and answer)",
               gateway.get("permissions") == "auto")
    gateway["permissions"] = "auto" if auto else "read-only"
    gateway["workdir"] = ask_folder(gateway.get("workdir"))
    nc.st.config["gateway"] = gateway
    nc.save_config()
    names = [n for n in PLATFORMS if (gateway.get(n) or {}).get("enabled") and
             platform_class(n).configured(gateway.get(n))]
    print(f"\n  {nc.GREEN}⏺ saved{nc.RESET} · platforms: {', '.join(names) or 'none'}\n"
          f"  {nc.DIM}start it: nanocode gateway start (background) or nanocode gateway run (this terminal).\n"
          f"  Message your bot: if you're not on the allow list yet, it answers with a code; approve it with\n"
          f"  nanocode gateway approve <code>.{nc.RESET}\n")
    return 0


GATEWAY_USAGE = """usage: nanocode gateway <command>
  setup              set up Telegram, Discord, Slack, WhatsApp or Matrix (tokens, who may talk to it)
  run                run in this terminal (Ctrl+C stops)
  start | stop       run in the background, or stop it
  status             is it running, platforms, people waiting for approval, last log lines
  approve <code>     let the person who got this code talk to nanocode
  logs               the last 50 lines of the log"""


@cli("gateway", "chat with nanocode from Telegram, Discord, Slack, WhatsApp or Matrix")
def gateway_cli(argv):
    command = argv[0] if argv else "help"
    if command == "setup":
        return setup()
    if command == "run":
        return run_foreground()
    if command == "start":
        return start_daemon()
    if command == "stop":
        return stop_daemon()
    if command == "restart":
        stop_daemon()
        return start_daemon()
    if command == "status":
        return show_status()
    if command == "approve":
        if len(argv) < 2:
            print(pending_text(load_state()))
            return 0
        result = approve_code(argv[1])
        print(f"  {result}" + ("" if result.startswith("error") else
                               "\n  (they'll be able to talk to it now; the running gateway picks this up by itself)"))
        return 1 if result.startswith("error") else 0
    if command == "logs":
        print("\n".join(tail(log_path(), 50)) or "no log yet")
        return 0
    print(GATEWAY_USAGE)
    return 0 if command in ("help", "-h", "--help") else 2
