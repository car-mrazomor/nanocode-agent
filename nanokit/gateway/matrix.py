"""Matrix, through the client-server API: long polling (/sync), so no public URL is needed, and any homeserver
works (matrix.org, your own Synapse, Conduit, Dendrite…).

nanocode uses an ordinary Matrix account (a bot account is best). It joins rooms it's invited to; in a direct
chat it answers everything, in a bigger room only when it's mentioned or sent a /command. Replies to a thread
stay in that thread. Images go to the model, voice and audio messages are transcribed (nanokit/voice.py).

Encrypted rooms need Olm/Megolm, which nanocode doesn't have: in one, it says so once and asks for an
unencrypted room (start a chat with encryption off, which Element allows for new rooms).
"""
from __future__ import annotations

import asyncio
import time
import urllib.parse

import httpx

from nanokit.gateway import GatewayError, Incoming, Platform, say

MATRIX_RETRIES = 3  # tries of a request that's rate limited (M_LIMIT_EXCEEDED / HTTP 429)
MATRIX_SYNC_TIMEOUT = 30_000  # ms the server holds a /sync open when nothing happens
API = "/_matrix/client/v3"


class MatrixError(Exception):
    def __init__(self, message, status=0, code=""):
        super().__init__(message)
        self.status, self.code = status, code


def quote(part):
    return urllib.parse.quote(str(part), safe="")


class Matrix(Platform):
    name = "matrix"
    limit = 16_000  # characters per message (an event can be 64 KB)
    SECRETS = [("access_token", "Access token (Element: Settings → Help & About → Access Token)")]
    SETTINGS = [("homeserver", "Homeserver URL", "https://matrix.org")]
    USER_HINT = "Matrix user ids (like @you:matrix.org)"
    SETUP_HELP = ("Use a separate Matrix account for nanocode (a bot account): log in to it in Element, copy its\n"
                  "  access token, then invite it to a direct chat with encryption off. Its rooms must be unencrypted.")

    def __init__(self, cfg, gateway=None, http=None):
        super().__init__(cfg, gateway, http)
        self.base = (cfg.get("homeserver") or "https://matrix.org").rstrip("/")
        self.me = ""          # our user id
        self.names = set()    # what a mention of us looks like: the user id, its localpart, the display name
        self.members = {}     # room -> joined members, to tell direct chats from bigger rooms
        self.encrypted = set()  # rooms where encryption is on
        self.told = set()       # encrypted rooms we've already said we can't read
        self.txn = int(time.time() * 1000)

    @staticmethod
    def configured(cfg):
        return bool((cfg or {}).get("access_token") and (cfg or {}).get("homeserver"))

    @staticmethod
    async def check(cfg):
        bot = Matrix(cfg)
        try:
            me = await bot.call("GET", "/account/whoami")
        finally:
            await bot.close()
        return f"connected as {me.get('user_id')}"

    def hide(self, text):
        return str(text).replace(self.cfg.get("access_token") or "\0", "<token>")

    async def call(self, method, path, body=None, params=None, timeout=30, prefix=API):
        """One request; a rate limit is waited out (as the server says) and tried again."""
        headers = {"Authorization": f"Bearer {self.cfg['access_token']}"}
        for attempt in range(MATRIX_RETRIES):
            try:
                resp = await self.client().request(method, self.base + prefix + path, json=body, params=params,
                                                   headers=headers, timeout=timeout)
            except httpx.HTTPError as e:
                raise MatrixError(self.hide(f"{type(e).__name__}: {e}"))
            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                data = {}
            if resp.status_code == 200:
                return data
            code = data.get("errcode", "") if isinstance(data, dict) else ""
            if (resp.status_code == 429 or code == "M_LIMIT_EXCEEDED") and attempt < MATRIX_RETRIES - 1:
                wait = (data.get("retry_after_ms") or 5000) / 1000 if isinstance(data, dict) else 5
                await asyncio.sleep(min(30.0, max(float(wait), 0.0)))
                continue
            message = (data.get("error") if isinstance(data, dict) else None) or f"HTTP {resp.status_code}"
            raise MatrixError(f"{code}: {message}" if code else message, resp.status_code, code)

    async def login(self):
        me = await self.call("GET", "/account/whoami")
        self.me = me["user_id"]
        localpart = self.me[1:].split(":", 1)[0]
        self.names = {self.me.lower(), localpart.lower()}
        try:
            profile = await self.call("GET", f"/profile/{quote(self.me)}/displayname")
            if profile.get("displayname"):
                self.names.add(profile["displayname"].lower())
        except MatrixError:
            pass

    async def run(self):
        delay = 1
        while not self.me:
            try:
                await self.login()
            except MatrixError as e:
                if e.status == 401 or e.code in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"):
                    raise GatewayError("the Matrix homeserver refused the access token: nanocode gateway setup")
                say(f"matrix: {e}; retrying in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        say(f"matrix: connected as {self.me} on {self.base}")
        since, delay = None, 1
        while True:
            try:
                first = since is None
                data = await self.sync(since)
                since, delay = data.get("next_batch") or since, 1
                await self.handle_sync(data, catch_up=first)
            except MatrixError as e:
                if e.status == 401 or e.code in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"):
                    raise GatewayError("the Matrix homeserver refused the access token: nanocode gateway setup")
                say(f"matrix: {e}; retrying in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            except (asyncio.CancelledError, GatewayError):
                raise
            except Exception as e:              # anything else: wait and go on, never stop for good
                say(f"matrix: {type(e).__name__}: {self.hide(e)}; retrying in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def sync(self, since):
        """The first sync only learns where "now" is (old messages aren't answered); later ones wait for news."""
        params = {"timeout": "0" if since is None else str(MATRIX_SYNC_TIMEOUT)}
        if since is None:
            params["filter"] = '{"room": {"timeline": {"limit": 1}}}'
        else:
            params["since"] = since
        return await self.call("GET", "/sync", params=params, timeout=MATRIX_SYNC_TIMEOUT / 1000 + 30)

    async def handle_sync(self, data, catch_up=False):
        rooms = data.get("rooms") or {}
        for room, info in (rooms.get("invite") or {}).items():
            await self.invited(room, info)
        for room, info in (rooms.get("join") or {}).items():
            summary = info.get("summary") or {}
            if "m.joined_member_count" in summary:
                self.members[room] = summary["m.joined_member_count"]
            for event in (info.get("state") or {}).get("events", []) + (info.get("timeline") or {}).get("events", []):
                if event.get("type") == "m.room.encryption":
                    self.encrypted.add(room)
            if catch_up:
                continue
            for event in (info.get("timeline") or {}).get("events", []):
                if event.get("sender") == self.me:
                    continue
                if event.get("type") == "m.room.encrypted":
                    await self.tell_encrypted(room)
                elif event.get("type") == "m.room.message":
                    asyncio.ensure_future(self.receive(room, event))

    async def invited(self, room, info):
        """Joins rooms it's invited to (strangers then get the approval code there, like on other platforms)."""
        try:
            await self.call("POST", f"/rooms/{quote(room)}/join", {})
        except MatrixError as e:
            say(f"matrix: couldn't join {room}: {e}")
            return
        events = (info.get("invite_state") or {}).get("events", [])
        if any(event.get("type") == "m.room.encryption" for event in events):
            await self.tell_encrypted(room)

    async def tell_encrypted(self, room):
        self.encrypted.add(room)
        if room in self.told:
            return
        self.told.add(room)
        try:
            await self.post(room, "This room is encrypted, and I can't read encrypted messages. Please start a "
                                  "chat with me with encryption off.")
        except MatrixError as e:
            say(f"matrix: {e}")

    def for_me(self, room, text):
        """In a direct chat everything; in a bigger room only a mention of nanocode or a /command."""
        if self.members.get(room, 2) <= 2:
            return True
        lowered = text.lower()
        return text.startswith(("/", "!")) or any(name and name in lowered for name in self.names)

    def strip_mention(self, text):
        for name in sorted(self.names, key=len, reverse=True):
            index = text.lower().find(name)
            if index >= 0:
                text = (text[:index] + text[index + len(name):]).lstrip(" :,")
                break
        return text.strip()

    async def receive(self, room, event):
        try:
            content = event.get("content") or {}
            relates = content.get("m.relates_to") or {}
            if relates.get("rel_type") == "m.replace":      # an edit of an earlier message
                return
            kind = content.get("msgtype")
            text = content.get("body") or ""
            if kind in ("m.text", "m.notice", "m.emote") and not self.for_me(room, text):
                return
            if kind in ("m.text", "m.notice", "m.emote") and self.members.get(room, 2) > 2:
                text = self.strip_mention(text)
            images, audio = [], None
            sender = event.get("sender")
            if kind in ("m.image", "m.audio", "m.file") and not self.may_download(sender):
                kind = "m.text"                         # a stranger: no downloads, just the approval code
            if kind == "m.image" and content.get("url"):
                mime = (content.get("info") or {}).get("mimetype") or "image/png"
                images.append((mime, await self.media(content["url"])))
                text = ""
            elif kind == "m.audio" and content.get("url"):
                mime = (content.get("info") or {}).get("mimetype") or "audio/ogg"
                audio = (await self.media(content["url"]), "." + (mime.split("/")[-1].split(";")[0] or "ogg"))
                text = ""
            thread = relates.get("event_id") if relates.get("rel_type") == "m.thread" else None
            await self.gateway.handle(Incoming("matrix", room, sender, text, name=sender, images=images,
                                               audio=audio, thread=thread))
        except (MatrixError, GatewayError, httpx.HTTPError) as e:
            say(f"matrix: couldn't read a message: {self.hide(e)}")

    async def media(self, mxc):
        """Bytes of an mxc:// file: the authenticated media API, else the older public one."""
        if not str(mxc).startswith("mxc://"):
            raise MatrixError("not a Matrix media link")
        server, _, media_id = mxc[len("mxc://"):].partition("/")
        auth = {"Authorization": f"Bearer {self.cfg['access_token']}"}
        try:
            return await self.download(f"{self.base}/_matrix/client/v1/media/download/{quote(server)}/{quote(media_id)}",
                                       headers=auth)
        except GatewayError as e:
            if "too big" in str(e):
                raise
            return await self.download(f"{self.base}/_matrix/media/v3/download/{quote(server)}/{quote(media_id)}",
                                       headers=auth)

    async def post(self, chat, text, thread=None):
        self.txn += 1
        content = {"msgtype": "m.text", "body": text}
        if thread:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread, "is_falling_back": True,
                                       "m.in_reply_to": {"event_id": thread}}
        await self.call("PUT", f"/rooms/{quote(chat)}/send/m.room.message/nanocode{self.txn}", content)

    async def typing(self, chat):
        if self.me:
            await self.call("PUT", f"/rooms/{quote(chat)}/typing/{quote(self.me)}", {"typing": True, "timeout": 8000})


PLATFORM = Matrix
