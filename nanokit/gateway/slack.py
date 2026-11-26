"""Slack, in Socket Mode: a websocket opened with an app-level token, so no public URL is needed.

The bot answers direct messages (message.im) and @mentions in channels (app_mention). A mention starts a
thread: the answer goes into it, and the thread is its own nanocode chat. Slack keeps "/" for its own slash
commands, so the chat commands start with "!" here (!new, !stop, !status, !cron).
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from nanokit.gateway import GatewayError, Incoming, Platform, say
from nanokit.gateway.discord import connector

SLACK_API = "https://slack.com/api/"
SLACK_RETRIES = 3  # tries of a call that's rate limited (HTTP 429)
SLACK_FATAL = ("invalid_auth", "not_authed", "account_inactive", "token_revoked", "not_allowed_token_type",
               "missing_scope")


class SlackError(Exception):
    pass


def to_mrkdwn(markdown):
    """Markdown in Slack's own format: *bold*, <url|label>, headings as bold lines; code stays as it is."""
    parts = re.split(r"(```.*?```)", markdown, flags=re.S)
    for i in range(0, len(parts), 2):
        part = parts[i]
        part = part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        part = re.sub(r"^#{1,6}\s+(.+?)\s*#*$", r"*\1*", part, flags=re.M)
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", part)
        part = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", part, flags=re.M)
        parts[i] = part
    return "".join(parts)


class Slack(Platform):
    name = "slack"
    limit = 3900
    SECRETS = [("app_token", "App-level token (xapp-…)"), ("bot_token", "Bot token (xoxb-…)")]
    USER_HINT = "Slack member ids (U…)"
    SETUP_HELP = ("Make an app at https://api.slack.com/apps: turn on Socket Mode (it makes the xapp- token, with\n"
                  "  connections:write); under OAuth add the bot scopes app_mentions:read, chat:write, im:history,\n"
                  "  files:read; under Event Subscriptions subscribe to app_mention and message.im; under App Home\n"
                  "  allow messages; install it to your workspace and copy the xoxb- bot token. Your member id:\n"
                  "  your profile → ⋮ → Copy member ID.")

    def __init__(self, cfg, gateway=None, http=None, connect=None):
        super().__init__(cfg, gateway, http)
        self.connect = connect
        self.user_id = None
        self.seen = []

    @staticmethod
    def configured(cfg):
        return bool((cfg or {}).get("app_token") and (cfg or {}).get("bot_token"))

    @staticmethod
    async def check(cfg):
        bot = Slack(cfg)
        try:
            me = await bot.api("auth.test")
            await bot.api("apps.connections.open", token=cfg["app_token"])
        finally:
            await bot.close()
        return f"connected as {me.get('user')} in {me.get('team')}"

    async def api(self, method, token=None, **params):
        for attempt in range(SLACK_RETRIES):
            try:
                resp = await self.client().post(SLACK_API + method, json=params or None,
                                                headers={"Authorization": f"Bearer {token or self.cfg['bot_token']}"})
            except httpx.HTTPError as e:
                raise SlackError(f"{type(e).__name__}: {e}")
            if resp.status_code != 429:
                break
            if attempt == SLACK_RETRIES - 1:
                raise SlackError("ratelimited")
            try:                                # rate limited: wait as told, then again
                wait = float(resp.headers.get("retry-after") or 5)
            except ValueError:
                wait = 5.0
            await asyncio.sleep(min(30.0, max(wait, 0.0)))
        try:
            data = resp.json()
        except ValueError:
            raise SlackError(f"HTTP {resp.status_code}")
        if not data.get("ok"):
            error = data.get("error") or f"HTTP {resp.status_code}"
            if error in SLACK_FATAL:
                raise GatewayError(f"Slack: {error} (check the tokens and scopes: nanocode gateway setup)")
            raise SlackError(error)
        return data

    async def run(self):
        delay = 1
        while True:
            try:
                if self.user_id is None:
                    me = await self.api("auth.test")
                    self.user_id = me.get("user_id")
                    say(f"slack: connected as {me.get('user')} in {me.get('team')}")
                url = (await self.api("apps.connections.open", token=self.cfg["app_token"]))["url"]
                await self.session(url)
                delay = 1
            except GatewayError:
                raise
            except Exception as e:
                say(f"slack: {type(e).__name__}: {e}; reconnecting in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def session(self, url):
        connect = self.connect or connector()
        async with connect(url, max_size=None) as ws:
            async for raw in ws:
                envelope = json.loads(raw)
                if envelope.get("envelope_id"):  # acknowledge at once, or Slack sends it again
                    await ws.send(json.dumps({"envelope_id": envelope["envelope_id"]}))
                kind = envelope.get("type")
                if kind == "disconnect":         # Slack asks to reconnect (it refreshes connections)
                    return
                if kind == "events_api":
                    payload = envelope.get("payload") or {}
                    event_id = payload.get("event_id")
                    if event_id and event_id in self.seen:
                        continue
                    self.seen = (self.seen + [event_id])[-200:]
                    asyncio.ensure_future(self.receive(payload.get("event") or {}))

    async def receive(self, event):
        try:
            kind, user = event.get("type"), event.get("user")
            if event.get("bot_id") or not user or user == self.user_id or \
                    event.get("subtype") not in (None, "file_share", "thread_broadcast"):
                return
            text = event.get("text") or ""
            if kind == "app_mention":
                thread = event.get("thread_ts") or event.get("ts")
            elif kind == "message" and event.get("channel_type") == "im":
                thread = event.get("thread_ts")
            else:
                return
            if self.user_id:
                text = re.sub(rf"<@{self.user_id}(\|[^>]*)?>", "", text).strip()
            text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            images, audio = [], None
            files = event.get("files") if self.may_download(user) else []
            for item in files or []:
                kind = str(item.get("mimetype") or "")
                url = item.get("url_private_download") or item.get("url_private")
                if not url:
                    continue
                auth = {"Authorization": f"Bearer {self.cfg['bot_token']}"}
                if kind in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    images.append((kind, await self.download(url, auth)))
                elif (kind.startswith("audio/") or item.get("subtype") == "slack_audio") and audio is None:
                    audio = (await self.download(url, auth), "." + str(item.get("filetype") or "m4a"))
            await self.gateway.handle(Incoming("slack", event["channel"], user, text, images=images, audio=audio,
                                               thread=thread))
        except (GatewayError, SlackError, httpx.HTTPError) as e:
            say(f"slack: couldn't read a message: {e}")

    def format(self, text):
        return to_mrkdwn(text)

    async def post(self, chat, text, thread=None):
        params = {"channel": chat, "text": self.format(text), "unfurl_links": False}
        if thread:
            params["thread_ts"] = thread
        await self.api("chat.postMessage", **params)


PLATFORM = Slack
