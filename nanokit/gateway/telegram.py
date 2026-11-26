"""Telegram, through the Bot API: long polling (getUpdates), so no public URL is needed.

Private chats always get answers; in groups, the bot answers when it's mentioned (@name), replied to, or sent
a /command. Photos go to the model as images, voice messages are transcribed (nanokit/voice.py).
"""
from __future__ import annotations

import asyncio
import re

import httpx

from nanokit.gateway import GatewayError, Incoming, Platform, say

TELEGRAM_RETRIES = 3  # tries of a call that's rate limited (HTTP 429)

TELEGRAM_API = "https://api.telegram.org"
SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
INLINE = re.compile(r"`([^`\n]+)`|\*\*(.+?)\*\*|\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


class TelegramError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def escape(text):
    return SPECIAL.sub(r"\\\1", text)


def inline_v2(text):
    out, pos = [], 0
    for m in INLINE.finditer(text):
        out.append(escape(text[pos:m.start()]))
        code, bold, label, url = m.groups()
        if code is not None:
            out.append("`" + code.replace("\\", "\\\\").replace("`", "\\`") + "`")
        elif bold is not None:
            out.append("*" + escape(bold) + "*")
        else:
            out.append(f"[{escape(label)}]({url.replace(chr(92), chr(92) * 2).replace(')', chr(92) + ')')})")
        pos = m.end()
    out.append(escape(text[pos:]))
    return "".join(out)


def to_markdown_v2(markdown):
    """Markdown from the model in Telegram's MarkdownV2: code blocks, inline code, bold, links and headings
    kept; everything else escaped, so Telegram never rejects it."""
    parts = re.split(r"(```[^\n`]*\n.*?```)", markdown, flags=re.S)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            m = re.match(r"```([^\n`]*)\n(.*?)```", part, re.S)
            lang = re.sub(r"[^\w+-]", "", m.group(1))
            out.append(f"```{lang}\n" + m.group(2).replace("\\", "\\\\").replace("`", "\\`") + "```")
            continue
        part = re.sub(r"^#{1,6}\s+(.+?)\s*#*$", r"**\1**", part, flags=re.M)
        part = re.sub(r"^(\s*)[-*+]\s+", "\\1• ", part, flags=re.M)
        out.append(inline_v2(part))
    return "".join(out)


class Telegram(Platform):
    name = "telegram"
    limit = 4096
    SECRETS = [("token", "Bot token")]
    USER_HINT = "Telegram user ids"
    SETUP_HELP = ("Make a bot: in Telegram, message @BotFather, send /newbot and copy the token it gives you.\n"
                  "  Your user id: message @userinfobot (or leave the list empty and approve yourself with a code).")

    def __init__(self, cfg, gateway=None, http=None):
        super().__init__(cfg, gateway, http)
        self.base = (cfg.get("api_url") or TELEGRAM_API).rstrip("/")
        self.me = {}

    @staticmethod
    def configured(cfg):
        return bool((cfg or {}).get("token"))

    @staticmethod
    async def check(cfg):
        bot = Telegram(cfg)
        try:
            me = await bot.call("getMe")
        finally:
            await bot.close()
        return f"connected as @{me.get('username')}"

    def hide(self, text):
        return str(text).replace(self.cfg.get("token") or "\0", "<token>")

    async def call(self, method, timeout=30, **params):
        for attempt in range(TELEGRAM_RETRIES):
            try:
                resp = await self.client().post(f"{self.base}/bot{self.cfg['token']}/{method}", json=params,
                                                timeout=timeout)
            except httpx.HTTPError as e:        # its message can hold the URL, and so the token
                raise TelegramError(self.hide(f"{type(e).__name__}: {e}"))
            try:
                data = resp.json()
            except ValueError:
                raise TelegramError(f"HTTP {resp.status_code}", resp.status_code)
            if data.get("ok"):
                return data.get("result")
            if resp.status_code == 429 and attempt < TELEGRAM_RETRIES - 1:   # rate limited: wait as told, again
                try:
                    wait = float((data.get("parameters") or {}).get("retry_after", 5))
                except (TypeError, ValueError):
                    wait = 5.0
                await asyncio.sleep(min(30.0, max(wait, 0.0)))
                continue
            raise TelegramError(data.get("description") or f"HTTP {resp.status_code}", resp.status_code)

    async def run(self):
        delay = 1
        while not self.me:
            try:
                self.me = await self.call("getMe")
            except TelegramError as e:
                if e.status in (401, 404):
                    raise GatewayError("Telegram refused the bot token: nanocode gateway setup")
                say(f"telegram: {e}; retrying in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        say(f"telegram: connected as @{self.me.get('username')}")
        offset, delay = None, 1
        while True:
            try:
                updates = await self.poll(offset)
                delay = 1
                for update in updates or []:
                    offset = update["update_id"] + 1
                    if update.get("message"):
                        asyncio.ensure_future(self.receive(update["message"]))
            except TelegramError as e:
                if e.status in (401, 404):
                    raise GatewayError("Telegram refused the bot token: nanocode gateway setup")
                if e.status == 409:
                    say("telegram: another program is reading this bot's messages (is the gateway running twice?)")
                else:
                    say(f"telegram: {e}; retrying in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            except (asyncio.CancelledError, GatewayError):
                raise
            except Exception as e:              # anything else: wait and go on, never stop for good
                say(f"telegram: {type(e).__name__}: {self.hide(e)}; retrying in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def poll(self, offset):
        params = {"timeout": 50, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = await self.client().post(f"{self.base}/bot{self.cfg['token']}/getUpdates", json=params, timeout=70)
        except httpx.HTTPError as e:
            raise TelegramError(self.hide(f"{type(e).__name__}: {e}"))
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"HTTP {resp.status_code}", resp.status_code)
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {resp.status_code}", resp.status_code)
        return data.get("result") or []

    def for_me(self, message, text):
        """In a group, only messages meant for the bot."""
        if (message.get("chat") or {}).get("type") == "private":
            return True
        name = self.me.get("username") or ""
        replied = ((message.get("reply_to_message") or {}).get("from") or {}).get("id")
        return (name and f"@{name}".lower() in text.lower()) or replied == self.me.get("id") or text.startswith("/")

    async def receive(self, message):
        try:
            user = message.get("from") or {}
            if user.get("is_bot"):
                return
            text = message.get("text") or message.get("caption") or ""
            if not self.for_me(message, text):
                return
            name = self.me.get("username")
            if name:
                text = re.sub(rf"@{re.escape(name)}\b", "", text, flags=re.I).strip()
            images, audio = [], None
            if not self.may_download(user.get("id")):   # a stranger: no downloads, just the approval code
                message = {k: v for k, v in message.items() if k not in ("photo", "document", "voice", "audio")}
            if message.get("photo"):
                biggest = max(message["photo"], key=lambda p: p.get("file_size") or p.get("width", 0))
                images.append(("image/jpeg", await self.file(biggest["file_id"])))
            document = message.get("document") or {}
            if str(document.get("mime_type", "")).startswith("image/"):
                data = await self.file(document["file_id"])
                images.append((document["mime_type"], data))
            voice = message.get("voice") or message.get("audio")
            if voice:
                suffix = ".ogg" if message.get("voice") else "." + (voice.get("file_name") or "a.mp3").rsplit(".", 1)[-1]
                audio = (await self.file(voice["file_id"]), suffix)
            who = user.get("username") or " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
            await self.gateway.handle(Incoming("telegram", message["chat"]["id"], user.get("id"), text, name=who,
                                               images=images, audio=audio))
        except (TelegramError, GatewayError, httpx.HTTPError) as e:
            say(f"telegram: couldn't read a message: {self.hide(e)}")

    async def file(self, file_id):
        info = await self.call("getFile", file_id=file_id)
        try:
            return await self.download(f"{self.base}/file/bot{self.cfg['token']}/{info['file_path']}")
        except httpx.HTTPError as e:            # its message can hold the URL, and so the token
            raise TelegramError(self.hide(f"download failed: {type(e).__name__}"))

    async def post(self, chat, text, thread=None):
        formatted = to_markdown_v2(text)
        if len(formatted) <= self.limit:
            try:
                await self.call("sendMessage", chat_id=chat, text=formatted, parse_mode="MarkdownV2",
                                link_preview_options={"is_disabled": True})
                return
            except TelegramError as e:
                if e.status != 400:
                    raise
        await self.call("sendMessage", chat_id=chat, text=text, link_preview_options={"is_disabled": True})

    async def typing(self, chat):
        await self.call("sendChatAction", chat_id=chat, action="typing")


PLATFORM = Telegram
