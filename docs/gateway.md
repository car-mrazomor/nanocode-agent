# Messaging gateway

The gateway lets you talk to nanocode from **Telegram, Discord, Slack or WhatsApp**, like [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s. It runs nanocode in the background, in one folder, and answers your messages there: ask about the code, start a task, get a scheduled report on your phone.

The code is in [`nanokit/gateway/`](../nanokit/gateway/): one file per platform.

## Set it up

```sh
cd your-project
nanocode gateway setup      # asks for each platform's tokens and who may talk to it
nanocode gateway start      # runs in the background
```

Setup checks each token, and saves everything in `~/.nanocode/config.json` under `"gateway"` (only you can read that file). The gateway works in the folder you ran setup in (`"workdir"`).

| Command | What it does |
|---|---|
| `nanocode gateway setup` | Set up platforms, the allow list and permissions |
| `nanocode gateway run` | Run in this terminal (Ctrl+C stops) |
| `nanocode gateway start` / `stop` / `restart` | Run in the background, or stop it |
| `nanocode gateway status` | Is it running, which platforms, who's waiting for approval, the last log lines |
| `nanocode gateway approve <code>` | Let someone talk to it |
| `nanocode gateway logs` | The last 50 lines of the log |

The background gateway keeps its pid file, log and lock in `~/.nanocode/gateway/`. While it runs it holds `gateway.lock`, so `stop` and `status` never mistake another program that got the same pid for it (and never stop that program).

## Who can talk to it

Only people on the allow list (user ids, per platform). Anyone else gets a polite answer with a code, and you see the request in `nanocode gateway status` and the log. To let them in:

```sh
nanocode gateway approve 7AFM5J
```

or send `/approve 7AFM5J` from an account that's already allowed. The easiest way to add yourself: leave the list empty in setup, message the bot, and approve your own code. A code works for an hour, and at most 20 wait at once (more requests are only logged). Photos and voice messages from people who aren't allowed are never downloaded, and any download stops at 20 MB.

**Owners** are the ids you gave in setup (`"owners"`), or the first person approved when there were none. In a read-only gateway only they can add or change [scheduled tasks](cron.md#from-a-gateway-chat).

## What it may do

By default the gateway is **read-only**: it can read code, search and answer, but edits and commands are denied. To allow them, answer yes in setup, or set `"gateway": {"permissions": "auto"}`. Dangerous commands, sending email and the `cron` tool are always denied, as in [`nanocode serve`](api.md) (use `/cron` instead).

## Chats

Each platform conversation (a direct chat, a channel, or a Slack thread) is its own nanocode chat. It's saved like any other chat, so you can `/resume` it in the app, and it continues after a restart. Messages in one conversation run one at a time (the next ones wait); different conversations can work at once.

Photos are passed to the model as images. Voice messages are transcribed (see [voice](voice.md#speech-to-text)).

Commands, in any chat:

| Command | What it does |
|---|---|
| `/new` | Start a new chat |
| `/stop` | Stop what it's doing, and drop the waiting messages |
| `/status` | Model, folder, permissions, what it's working on |
| `/cron ...` | [Scheduled tasks](cron.md); their answers come back to this chat |
| `/approve <code>` | Let someone talk to it (`/approve` alone lists the requests) |
| `/help` | The commands |

On Slack, `/` is for Slack's own commands: use `!new`, `!stop`, `!status`, `!cron` instead (they work everywhere).

## Telegram

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Run `nanocode gateway setup` and paste it.

It uses long polling, so no public address is needed. In private chats it answers everything; in groups, only when it's mentioned (`@yourbot`), replied to, or sent a command. Your user id: message **@userinfobot**.

## Discord

1. At <https://discord.com/developers/applications>, make an application, open **Bot**, copy the token, and turn on **Message Content Intent**.
2. Run setup and paste the token. It prints a link to add the bot to your server.

It answers direct messages, and messages that @mention it in a server. To limit that to some channels, list their ids in setup (`"channels"`). Your user id: turn on Developer Mode (Settings → Advanced), then right-click your name → Copy User ID.

## Slack

1. At <https://api.slack.com/apps>, make an app. Turn on **Socket Mode**: it gives you an app-level token (`xapp-…`, scope `connections:write`).
2. Under **OAuth & Permissions**, add the bot scopes `app_mentions:read`, `chat:write`, `im:history`, `files:read`.
3. Under **Event Subscriptions**, subscribe to the bot events `app_mention` and `message.im`. Under **App Home**, allow users to send messages.
4. Install the app to your workspace, copy the bot token (`xoxb-…`), and run setup.

Socket Mode needs no public address. It answers direct messages, and @mentions in channels, in a thread: each thread is its own chat (mention it again to continue there). Your member id: your profile → ⋮ → Copy member ID.

## WhatsApp

This uses Meta's WhatsApp Cloud API. Meta **sends messages to your computer**, so the gateway runs a small web server (port 8770) that must be reachable from the internet, through a tunnel:

```sh
cloudflared tunnel --url http://localhost:8770     # or: ngrok http 8770
```

1. At <https://developers.facebook.com>, make an app (type Business) and add **WhatsApp**.
2. From **API Setup**, copy the access token and the phone number id; from **App settings → Basic**, the app secret.
3. Run setup. Choose a verify token (any text).
4. In **WhatsApp → Configuration**, set the webhook URL to `<your tunnel URL>/webhook` with your verify token, and subscribe to `messages`.

Every message Meta sends is checked against the app secret, so nobody else can post to the webhook. The allow list holds phone numbers (international format, digits only). Meta's temporary access tokens expire after a day: make a permanent one (a system user token) for everyday use.

## Settings

All under `"gateway"` in `~/.nanocode/config.json`:

```json
{
  "gateway": {
    "workdir": "/home/me/project",
    "permissions": "read-only",
    "telegram": {"token": "123:ABC…", "allowed_users": ["123456789"], "owners": ["123456789"]},
    "discord": {"token": "…", "allowed_users": ["…"], "channels": ["…"]},
    "slack": {"app_token": "xapp-…", "bot_token": "xoxb-…", "allowed_users": ["U…"]},
    "whatsapp": {"access_token": "…", "app_secret": "…", "phone_number_id": "…", "verify_token": "…",
                 "port": 8770, "host": "127.0.0.1", "allowed_users": ["4477…"]}
  }
}
```

Rate limits (HTTP 429) are waited out and retried, a few times. Set `"enabled": false` in a platform to turn it off without deleting its tokens. Which chat is which nanocode chat, and the waiting approval codes, are kept in `~/.nanocode/gateway/state.json`.
