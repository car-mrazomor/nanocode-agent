"""WhatsApp, through Meta's WhatsApp Cloud API.

Meta sends messages to a webhook, so this runs a small HTTP server (port 8770 by default) that must be
reachable from the internet: put a tunnel in front of it (cloudflared, ngrok) and give Meta that URL. Every
POST is checked against the app secret (X-Hub-Signature-256), so only Meta can send messages. Answers go out
through the Graph API.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx

import nanocode as nc
from nanokit.gateway import GatewayError, Incoming, Platform, say

GRAPH_API = "https://graph.facebook.com/v21.0"
WHATSAPP_PORT = 8770
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 413: "Payload Too Large"}


def to_whatsapp(markdown):
    """Markdown in WhatsApp's format: *bold*, headings as bold lines, links as "label (url)"."""
    parts = re.split(r"(```.*?```)", markdown, flags=re.S)
    for i in range(0, len(parts), 2):
        part = re.sub(r"^#{1,6}\s+(.+?)\s*#*$", r"*\1*", parts[i], flags=re.M)
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", part)
        parts[i] = re.sub(r"^(\s*)[-+]\s+", r"\1• ", part, flags=re.M)
    return "".join(parts)


def signature_ok(secret, body, header):
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(header) and hmac.compare_digest(expected, header.strip())


class WhatsApp(Platform):
    name = "whatsapp"
    limit = 4096
    SECRETS = [("access_token", "Access token"), ("app_secret", "App secret (App settings → Basic)")]
    SETTINGS = [("phone_number_id", "Phone number id (WhatsApp → API Setup)", ""),
                ("verify_token", "Webhook verify token (any text; you'll paste it in Meta's webhook setup)",
                 secrets.token_hex(8)),
                ("port", "Port for the webhook server", WHATSAPP_PORT)]
    USER_HINT = "WhatsApp numbers (international, digits only)"
    SETUP_HELP = ("Make an app at https://developers.facebook.com (type Business) and add WhatsApp. Copy the\n"
                  "  access token and phone number id from API Setup, and the app secret. The webhook needs a public\n"
                  "  URL: run a tunnel (cloudflared tunnel --url http://localhost:8770, or ngrok http 8770) and set\n"
                  "  <tunnel URL>/webhook with your verify token in WhatsApp → Configuration; subscribe to messages.")

    def __init__(self, cfg, gateway=None, http=None):
        super().__init__(cfg, gateway, http)
        self.seen = []
        self.server = None

    @staticmethod
    def configured(cfg):
        cfg = cfg or {}
        return all(cfg.get(k) for k in ("access_token", "app_secret", "phone_number_id", "verify_token"))

    @staticmethod
    async def check(cfg):
        bot = WhatsApp(cfg)
        try:
            info = await bot.graph("GET", f"/{cfg['phone_number_id']}")
        finally:
            await bot.close()
        return f"connected to {info.get('display_phone_number') or info.get('verified_name') or 'the number'}"

    def auth(self):
        return {"Authorization": f"Bearer {self.cfg['access_token']}"}

    async def graph(self, method, path, body=None):
        resp = await self.client().request(method, GRAPH_API + path, json=body, headers=self.auth())
        if resp.status_code == 401:
            raise GatewayError("WhatsApp refused the access token (they expire: make a permanent one)")
        if resp.status_code >= 400:
            raise GatewayError(f"WhatsApp: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def run(self):
        host = self.cfg.get("host") or "127.0.0.1"
        port = int(self.cfg.get("port") or WHATSAPP_PORT)
        try:
            self.server = await asyncio.start_server(self.handle_http, host, port)
        except OSError as e:
            raise GatewayError(f"WhatsApp: can't listen on {host}:{port}: {e}")
        say(f"whatsapp: webhook on http://{host}:{port}/webhook (reach it from the internet with a tunnel: "
            f"cloudflared tunnel --url http://localhost:{port})")
        async with self.server:
            await self.server.serve_forever()

    async def handle_http(self, reader, writer):
        from api import server
        try:
            method, path, headers, body = await server.read_http_request(reader)
            status, text = await self.route(method, path, headers, body)
        except server.ApiError as e:
            status, text = e.status, str(e)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            status, text = None, ""
        except Exception as e:
            nc.log_error(e)
            status, text = 500, "error"
        try:
            if status:
                data = text.encode()
                writer.write((f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\nContent-Type: text/plain\r\n"
                              f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
                await writer.drain()
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def route(self, method, path, headers, body):
        url = urlsplit(path)
        if method == "GET":                      # Meta checks the webhook when you set it up
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if query.get("hub.mode") == "subscribe" and hmac.compare_digest(
                    query.get("hub.verify_token", ""), str(self.cfg.get("verify_token"))):
                say("whatsapp: webhook verified by Meta")
                return 200, query.get("hub.challenge", "")
            return 403, "wrong verify token"
        if method != "POST":
            return 405, "only GET and POST"
        if not signature_ok(str(self.cfg["app_secret"]), body, headers.get("x-hub-signature-256", "")):
            say("whatsapp: refused a POST with a wrong signature (is app_secret right?)")
            return 401, "bad signature"
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, "not JSON"
        for entry in data.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
                for message in value.get("messages") or []:
                    asyncio.ensure_future(self.receive(message, names))
        return 200, "ok"

    async def media(self, media_id):
        info = await self.graph("GET", f"/{media_id}")
        return await self.download(info["url"], self.auth()), str(info.get("mime_type") or "")

    async def receive(self, message, names):
        try:
            if message.get("id") in self.seen:   # Meta retries deliveries
                return
            self.seen = (self.seen + [message.get("id")])[-200:]
            kind, user = message.get("type"), str(message.get("from"))
            text, images, audio = "", [], None
            if kind in ("image", "audio") and not self.may_download(user):
                kind, message = "text", {"text": {"body": (message.get("image") or {}).get("caption") or ""}}
            if kind == "text":
                text = (message.get("text") or {}).get("body", "")
            elif kind == "image":
                data, mime = await self.media(message["image"]["id"])
                images.append((mime.split(";")[0] or "image/jpeg", data))
                text = message["image"].get("caption") or ""
            elif kind == "audio":
                data, mime = await self.media(message["audio"]["id"])
                audio = (data, ".mp3" if "mpeg" in mime else ".ogg")
            else:
                return
            await self.gateway.handle(Incoming("whatsapp", user, user, text, name=names.get(user), images=images,
                                               audio=audio))
        except (GatewayError, httpx.HTTPError, KeyError) as e:
            say(f"whatsapp: couldn't read a message: {e}")

    def format(self, text):
        return to_whatsapp(text)

    async def post(self, chat, text, thread=None):
        await self.graph("POST", f"/{self.cfg['phone_number_id']}/messages", {
            "messaging_product": "whatsapp", "to": chat, "type": "text",
            "text": {"body": self.format(text), "preview_url": False}})


PLATFORM = WhatsApp
