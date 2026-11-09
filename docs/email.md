# Email

nanocode can read, summarize, organize and draft your email over IMAP and SMTP. It needs no extra packages.

## Setup

```sh
nanocode email
```

It asks for your address, then fills in the servers for Gmail, iCloud, Outlook, Yahoo and Fastmail (or asks for them), then the password. Gmail and iCloud need an [app password](https://support.google.com/accounts/answer/185833). The settings are stored under `"email"` in `~/.nanocode/config.json`, readable only by you.

## Use

- **`/email`**: what's new, sorted into needs a reply, FYI and likely spam, with suggested tags and moves.
- **`/email <task>`**, or just ask in the chat:
  - "draft a reply to Ana saying Friday works"
  - "move the newsletters to archive"
  - "tag everything from my bank as finance"

## Drafts in your style

Before writing, nanocode reads a few of your sent emails to match your tone, greeting and sign-off. Drafts go to your **Drafts** folder, where you can edit and send them yourself.

## Safety

- **It can't send by itself.** Sending only happens when you ask, and you confirm every send, even in Auto mode.
- **Email is untrusted.** nanocode treats what's in an email as information, never as instructions. "Ignore previous instructions" in an email does nothing.
- **Reading doesn't mark as read.**

The email tools are listed in [Tools](tools.md#email).
