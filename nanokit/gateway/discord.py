"""Discord: the Gateway websocket to receive messages, the REST API to answer.

The bot answers direct messages, and messages that @mention it in a server (only in the channels listed in
"channels", when there's a list). It needs the MESSAGE CONTENT intent, turned on in the Developer Portal.
"""
from __future__ import annotations

import asyncio
import json
import random
import re

import httpx

from nanokit.gateway import GatewayError, Incoming, Platform, say

DISCORD_API = "https://discord.com/api/v10"
DISCORD_GATEWAY = "wss://gateway.discord.gg"
# GUILDS, GUILD_MESSAGES, DIRECT_MESSAGES, MESSAGE_CONTENT
DISCORD_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)
FATAL_CLOSE = {4004: "Discord refused the bot token: nanocode gateway setup",
               4010: "invalid shard", 4011: "sharding required", 4012: "invalid API version",
               4013: "invalid intents",
               4014: "the bot isn't allowed the MESSAGE CONTENT intent: turn it on in the Discord Developer Portal "
                     "(your app → Bot → Privileged Gateway Intents)"}
INVITE_PERMISSIONS = 1024 | 2048 | 65536  # view channels, send messages, read message history


def connector():
    """websockets' asyncio client (the module moved it in version 14)."""
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        from websockets import connect
    return connect


class Discord(Platform):
    name = "discord"
    limit = 2000
    SECRETS = [("token", "Bot token")]
    SETTINGS = [("channels", "Channel ids where it answers @mentions (comma separated; empty: every channel)", "")]
    USER_HINT = "Discord user ids"
    SETUP_HELP = ("Make an app at https://discord.com/developers/applications → Bot: copy the token and turn on\n"
                  "  MESSAGE CONTENT INTENT. Your user id: Settings → Advanced → Developer Mode, then right-click "
                  "yourself → Copy User ID.")

    def __init__(self, cfg, gateway=None, http=None, connect=None):
        super().__init__(cfg, gateway, http)
        self.connect = connect
        self.session_id = self.seq = self.resume_url = self.user_id = None
        self.acked = True
        self.seen = []

    @staticmethod
    def configured(cfg):
        return bool((cfg or {}).get("token"))

    @staticmethod
    async def check(cfg):
        bot = Discord(cfg)
        try:
            me = await bot.rest("GET", "/users/@me")
        finally:
            await bot.close()
        invite = (f"https://discord.com/oauth2/authorize?client_id={me['id']}&scope=bot"
                  f"&permissions={INVITE_PERMISSIONS}")
        return f"connected as {me.get('username')} · add it to a server: {invite}"

    def channels(self):
        value = self.cfg.get("channels") or []
        return [str(c) for c in (re.split(r"[\s,]+", value) if isinstance(value, str) else value) if str(c)]

    async def rest(self, method, path, body=None):
        for _ in range(3):
            resp = await self.client().request(method, DISCORD_API + path, json=body,
                                               headers={"Authorization": f"Bot {self.cfg['token']}"})
            if resp.status_code == 429:         # rate limited: wait as told, then again
                try:
                    wait = float(resp.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                await asyncio.sleep(min(wait, 30))
                continue
            if resp.status_code == 401:
                raise GatewayError("Discord refused the bot token: nanocode gateway setup")
            if resp.status_code >= 400:
                raise GatewayError(f"Discord: HTTP {resp.status_code} {resp.text[:200]}")
            return resp.json() if resp.content else {}
        raise GatewayError("Discord: rate limited")

    async def run(self):
        try:
            url = (await self.rest("GET", "/gateway/bot")).get("url") or DISCORD_GATEWAY
        except GatewayError as e:
            if "refused" in str(e):
                raise
            url = DISCORD_GATEWAY
        except Exception:                       # network trouble: the usual address works too
            url = DISCORD_GATEWAY
        delay = 1
        while True:
            try:
                await self.session(url)
                delay = 1
            except GatewayError:
                raise
            except Exception as e:              # network trouble, a closed socket: connect again and resume
                say(f"discord: {type(e).__name__}: {e}; reconnecting in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def session(self, url):
        target = (self.resume_url if self.session_id and self.resume_url else url).rstrip("/") + "/?v=10&encoding=json"
        connect = self.connect or connector()
        async with connect(target, max_size=None) as ws:
            hello = json.loads(await ws.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            self.acked = True
            heart = asyncio.ensure_future(self.heartbeat(ws, interval))
            try:
                if self.session_id:
                    await ws.send(json.dumps({"op": 6, "d": {"token": self.cfg["token"],
                                                             "session_id": self.session_id, "seq": self.seq}}))
                else:
                    await ws.send(json.dumps({"op": 2, "d": {
                        "token": self.cfg["token"], "intents": DISCORD_INTENTS,
                        "properties": {"os": "nanocode", "browser": "nanocode", "device": "nanocode"}}}))
                async for raw in ws:
                    if await self.event(ws, json.loads(raw)) == "reconnect":
                        return
            except Exception as e:              # closed with an error code: see which below
                closed = e
            else:
                closed = None
            finally:
                heart.cancel()
            code = getattr(ws, "close_code", None)
            if code in FATAL_CLOSE:
                raise GatewayError(FATAL_CLOSE[code])
            if code in (4007, 4009):            # the session can't be resumed: start a new one
                self.session_id = self.seq = None
            if closed is not None:
                raise closed

    async def event(self, ws, event):
        if event.get("s") is not None:
            self.seq = event["s"]
        op, data = event.get("op"), event.get("d")
        if op == 0:
            kind = event.get("t")
            if kind == "READY":
                self.session_id, self.resume_url = data["session_id"], data.get("resume_gateway_url")
                self.user_id = data["user"]["id"]
                say(f"discord: connected as {data['user'].get('username')}")
            elif kind == "MESSAGE_CREATE":
                asyncio.ensure_future(self.receive(data))
        elif op == 1:
            await ws.send(json.dumps({"op": 1, "d": self.seq}))
        elif op == 7:
            return "reconnect"
        elif op == 9:
            if not data:
                self.session_id = self.seq = None
            await asyncio.sleep(random.uniform(1, 5))
            return "reconnect"
        elif op == 11:
            self.acked = True
        return None

    async def heartbeat(self, ws, interval):
        await asyncio.sleep(interval * random.random())
        while True:
            if not self.acked:                  # no answer to the last one: the connection is dead
                await ws.close(4000)
                return
            self.acked = False
            await ws.send(json.dumps({"op": 1, "d": self.seq}))
            await asyncio.sleep(interval)

    async def receive(self, message):
        try:
            author = message.get("author") or {}
            if author.get("bot") or message.get("id") in self.seen:
                return
            self.seen = (self.seen + [message.get("id")])[-200:]
            text = message.get("content") or ""
            if message.get("guild_id"):          # in a server: only when mentioned, in the allowed channels
                if not any(m.get("id") == self.user_id for m in message.get("mentions") or []):
                    return
                if self.channels() and str(message["channel_id"]) not in self.channels():
                    return
                text = re.sub(rf"<@!?{self.user_id}>", "", text).strip()
            images, audio = [], None
            attachments = message.get("attachments") if self.may_download(author.get("id")) else []
            for item in attachments or []:
                kind = str(item.get("content_type") or "")
                if kind.split(";")[0] in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    images.append((kind.split(";")[0], await self.download(item["url"])))
                elif kind.startswith("audio/") and audio is None:
                    suffix = "." + str(item.get("filename") or "voice.ogg").rsplit(".", 1)[-1]
                    audio = (await self.download(item["url"]), suffix)
            await self.gateway.handle(Incoming("discord", message["channel_id"], author.get("id"), text,
                                               name=author.get("username"), images=images, audio=audio))
        except (GatewayError, httpx.HTTPError) as e:
            say(f"discord: couldn't read a message: {e}")

    async def post(self, chat, text, thread=None):
        await self.rest("POST", f"/channels/{chat}/messages", {"content": text, "allowed_mentions": {"parse": []}})

    async def typing(self, chat):
        await self.rest("POST", f"/channels/{chat}/typing")


PLATFORM = Discord
