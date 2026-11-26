"""Email: read, search, triage, draft and send over IMAP/SMTP (standard library only), set up with `nanocode email` in a terminal so the password is never typed into the chat."""
from __future__ import annotations

import asyncio
import getpass
import re
import time

import nanocode as nc

__all__ = ['EMAIL_PRESETS', 'ALWAYS_ASK', 'email_config', 'email_preset', 'is_gmail', 'Mailbox', 'email_rows', 'uid_list', 'tool_email_list', 'tool_email_search', 'tool_email_read', 'tool_email_folders', 'compose', 'COMPOSE_ARGS', 'original', 'tool_email_draft', 'tool_email_send', 'tool_email_tag', 'EMAIL_TRIAGE', 'cmd_email', 'email_setup']


# --- Email: read, search, triage, draft and send over IMAP/SMTP (standard library only). Set up with
#     `nanocode email` in a terminal, so the password is never typed into the chat. ---

EMAIL_PRESETS = {
    ("gmail.com", "googlemail.com"): ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    ("icloud.com", "me.com", "mac.com"): ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    ("outlook.com", "hotmail.com", "live.com", "msn.com"): ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587),
    ("yahoo.com", "ymail.com"): ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    ("fastmail.com", "fastmail.fm"): ("imap.fastmail.com", 993, "smtp.fastmail.com", 465),
}
ALWAYS_ASK = {"email_send"}  # asks every time, even in Auto mode, and "always" can't be chosen


def email_config():
    cfg = nc.st.config.get("email") or {}
    return cfg if cfg.get("imap_host") and cfg.get("user") and cfg.get("password") else None


def email_preset(address):
    domain = address.rpartition("@")[2].lower()
    return next((v for k, v in EMAIL_PRESETS.items() if domain in k), None)


def is_gmail():
    return "gmail.com" in (email_config() or {}).get("imap_host", "")


class Mailbox:
    """One IMAP connection for one tool call."""

    def __enter__(self):
        import imaplib
        cfg = email_config()
        if not cfg:
            raise nc.ToolError("email isn't set up yet: run `nanocode email` in a terminal")
        try:
            self.m = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg.get("imap_port") or 993), timeout=40)
            self.m.login(cfg["user"], cfg["password"])
        except (OSError, imaplib.IMAP4.error) as e:
            raise nc.ToolError(f"couldn't log in to {cfg['imap_host']}: {e}") from e
        try:   # servers (Gmail, Dovecot) list MOVE and UIDPLUS only once you're logged in
            typ, data = self.m.capability()
            if typ == "OK" and data and data[0]:
                self.m.capabilities = tuple(data[0].decode(errors="replace").upper().split())
        except (OSError, imaplib.IMAP4.error):
            pass
        return self

    def __exit__(self, *exc):
        try:
            self.m.logout()
        except Exception:
            pass

    def folders(self):
        """[(name, flags)] with special-use flags like \\Sent, \\Drafts, \\Junk, \\Trash, \\Archive, \\All."""
        typ, data = self.m.list()
        result = []
        for raw in data or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            match = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if match:
                result.append((match["name"].strip().strip('"'), match["flags"]))
        return result

    def special(self, kind):
        """The folder for "inbox", "sent", "drafts", "junk", "trash" or "archive" (else the name as given)."""
        kind_l = kind.lower()
        if kind_l == "inbox":
            return "INBOX"
        flags = {"sent": "\\Sent", "drafts": "\\Drafts", "junk": "\\Junk", "spam": "\\Junk", "trash": "\\Trash",
                 "archive": "\\All" if is_gmail() else "\\Archive"}.get(kind_l)
        names = self.folders()
        if flags:
            for name, f in names:
                if flags.lower() in f.lower():
                    return name
            for name, _ in names:
                if name.lower().rsplit("/", 1)[-1] in {"sent": ("sent", "sent items", "sent messages", "sent mail"),
                                                       "drafts": ("drafts",), "junk": ("junk", "spam", "junk email"),
                                                       "spam": ("junk", "spam"), "trash": ("trash", "deleted items"),
                                                       "archive": ("archive", "all mail")}[kind_l]:
                    return name
            raise nc.ToolError(f"no {kind_l} folder found (folders: {', '.join(n for n, _ in names)})")
        return kind

    def select(self, folder, readonly=True):
        name = self.special(folder or "inbox")
        typ, _ = self.m.select(f'"{name}"', readonly=readonly)
        if typ != "OK":
            raise nc.ToolError(f"can't open folder '{name}'")
        return name

    def uid(self, *args):
        typ, data = self.m.uid(*args)
        if typ != "OK":
            raise nc.ToolError(f"the mail server refused {args[0]}: {data}")
        return data

    def search(self, *criteria):
        data = self.uid("SEARCH", *criteria)
        return [int(x) for x in (data[0] or b"").split()]

    def headers(self, uids):
        if not uids:
            return []
        from email import policy
        from email.parser import BytesHeaderParser
        data = self.uid("FETCH", ",".join(map(str, uids)), "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
        rows, i = [], 0
        while i < len(data):
            item = data[i]
            if isinstance(item, tuple):
                meta = item[0].decode(errors="replace")
                tail = data[i + 1].decode(errors="replace") if i + 1 < len(data) and isinstance(data[i + 1], bytes) else ""
                uid = re.search(r"UID (\d+)", meta + tail)
                flags = re.search(r"FLAGS \(([^)]*)\)", meta + tail)
                h = BytesHeaderParser(policy=policy.default).parsebytes(item[1])
                rows.append({"uid": int(uid[1]) if uid else 0, "flags": flags[1] if flags else "",
                             "from": str(h.get("From", "")), "to": str(h.get("To", "")),
                             "subject": str(h.get("Subject", "")), "date": str(h.get("Date", ""))})
            i += 1
        return sorted(rows, key=lambda r: r["uid"], reverse=True)

    def message(self, uid):
        from email import message_from_bytes, policy
        data = self.uid("FETCH", str(uid), "(BODY.PEEK[])")
        raw = next((item[1] for item in data if isinstance(item, tuple)), None)
        if raw is None:
            raise nc.ToolError(f"no message with uid {uid} in this folder")
        return message_from_bytes(raw, policy=policy.default)


def email_rows(rows):
    if not rows:
        return "(no messages)"
    lines = []
    for r in rows:
        unread = "" if "\\Seen" in r["flags"] else " ●"
        star = " ★" if "\\Flagged" in r["flags"] else ""
        lines.append(f"[{r['uid']}]{unread}{star} {r['date'][:22]} · {r['from'][:48]} · {r['subject'][:90]}")
    return "\n".join(lines)


def uid_list(value):
    items = value if isinstance(value, list) else re.split(r"[\s,]+", str(value or ""))
    uids = [int(x) for x in items if str(x).strip().isdigit()]
    if not uids:
        raise nc.ToolError("give the message uids (the numbers in [brackets] from email_list)")
    return uids


@nc.tool("email_list", "List recent emails in a folder (inbox, sent, drafts, junk, trash, archive or a folder name), "
      "newest first. ● marks unread.",
      {"folder": {"type": "string", "description": "default inbox"}, "limit": {"type": "integer", "description": "default 20"},
       "unread_only": {"type": "boolean"}})
async def tool_email_list(args):
    def work():
        with Mailbox() as box:
            name = box.select(args.get("folder") or "inbox")
            uids = box.search("UNSEEN" if args.get("unread_only") else "ALL")
            limit = min(max(int(args.get("limit") or 20), 1), 100)
            return f"{name} · {len(uids)} {'unread' if args.get('unread_only') else 'messages'}\n" + \
                email_rows(box.headers(uids[-limit:]))
    return await asyncio.to_thread(work)


@nc.tool("email_search", "Search emails by words, sender or subject (Gmail search syntax works on Gmail).",
      {"query": {"type": "string"}, "folder": {"type": "string", "description": "default inbox"},
       "limit": {"type": "integer", "description": "default 20"}}, ["query"])
async def tool_email_search(args):
    def work():
        with Mailbox() as box:
            name = box.select(args.get("folder") or "inbox")
            query = args["query"].replace('"', "")
            key = "X-GM-RAW" if is_gmail() else "TEXT"
            if query.isascii():
                uids = box.search(key, f'"{query}"') if is_gmail() else box.search("CHARSET", "UTF-8", key, f'"{query}"')
            else:                     # imaplib only sends ASCII: other text goes as a UTF-8 literal
                box.m.literal = query.encode("utf-8")
                uids = box.search("CHARSET", "UTF-8", key)
            limit = min(max(int(args.get("limit") or 20), 1), 100)
            return f"{name} · {len(uids)} found for '{query}'\n" + email_rows(box.headers(uids[-limit:]))
    return await asyncio.to_thread(work)


@nc.tool("email_read", "Read one email (headers and text) by its uid. Doesn't mark it as read.",
      {"uid": {"type": "integer"}, "folder": {"type": "string", "description": "default inbox"}}, ["uid"])
async def tool_email_read(args):
    def work():
        with Mailbox() as box:
            box.select(args.get("folder") or "inbox")
            msg = box.message(int(args["uid"]))
        body = msg.get_body(preferencelist=("plain", "html"))
        text = ""
        if body is not None:
            text = body.get_content()
            if body.get_content_type() == "text/html":
                text = nc.page_text(text)[1]
        files = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
        head = "\n".join(f"{k}: {msg[k]}" for k in ("From", "To", "Cc", "Date", "Subject") if msg[k])
        extra = f"\nAttachments: {', '.join(files)}" if files else ""
        return nc.cap(f"{head}{extra}\n\n<email-content untrusted>\n{text.strip()}\n</email-content>", 20_000)
    return await asyncio.to_thread(work)


@nc.tool("email_folders", "List the mail folders.", {})
async def tool_email_folders(args):
    def work():
        with Mailbox() as box:
            return "\n".join(f"{name}  {flags}" for name, flags in box.folders())
    return await asyncio.to_thread(work)


def compose(args, reply_to=None):
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid
    cfg = email_config()
    msg = EmailMessage()
    msg["From"] = cfg.get("address") or cfg["user"]
    msg["To"] = args["to"]
    if args.get("cc"):
        msg["Cc"] = args["cc"]
    subject = args.get("subject") or ""
    if reply_to is not None:
        if not subject:
            subject = str(reply_to.get("Subject", ""))
            subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if reply_to.get("Message-ID"):
            msg["In-Reply-To"] = reply_to["Message-ID"]
            msg["References"] = f"{reply_to.get('References', '')} {reply_to['Message-ID']}".strip()
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(args["body"])
    return msg


COMPOSE_ARGS = {"to": {"type": "string"}, "subject": {"type": "string", "description": "for a reply, can be left out"},
                "body": {"type": "string"}, "cc": {"type": "string"},
                "reply_to_uid": {"type": "integer", "description": "uid of the email this replies to (threads it)"},
                "reply_folder": {"type": "string", "description": "folder of that email, default inbox"}}


def original(box, args):
    if not args.get("reply_to_uid"):
        return None
    box.select(args.get("reply_folder") or "inbox")
    return box.message(int(args["reply_to_uid"]))


@nc.tool("email_draft", "Save an email as a draft in the Drafts folder (nothing is sent). Prefer this over sending.",
      COMPOSE_ARGS, ["to", "body"])
async def tool_email_draft(args):
    if not await nc.allowed("email_draft", f"draft to {args['to']}: {args.get('subject') or '(reply)'}"):
        return nc.DENIED

    def work():
        import imaplib
        with Mailbox() as box:
            msg = compose(args, original(box, args))
            drafts = box.special("drafts")
            typ, data = box.m.append(f'"{drafts}"', "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if typ != "OK":
                raise nc.ToolError(f"couldn't save the draft: {data}")
            return f"draft saved in {drafts}: to {msg['To']} · {msg['Subject']}"
    return await asyncio.to_thread(work)


@nc.tool("email_send", "Send an email. Only when the user explicitly asked to send it; they confirm every send.",
      COMPOSE_ARGS, ["to", "body"])
async def tool_email_send(args):
    preview = f"to {args['to']}" + (f" cc {args['cc']}" if args.get("cc") else "") + \
        f": {args.get('subject') or '(reply)'} — {' '.join(args['body'].split())[:80]}"
    if not await nc.allowed("email_send", preview):
        return nc.DENIED

    def work():
        import imaplib
        import smtplib
        cfg = email_config()
        with Mailbox() as box:
            msg = compose(args, original(box, args))
            port = int(cfg.get("smtp_port") or 465)
            try:
                smtp = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=40) if port == 465 else \
                    smtplib.SMTP(cfg["smtp_host"], port, timeout=40)
                with smtp:
                    if port != 465:
                        smtp.starttls()
                    smtp.login(cfg["user"], cfg["password"])
                    smtp.send_message(msg)
            except (OSError, smtplib.SMTPException) as e:
                raise nc.ToolError(f"sending failed: {e}") from e
            if not is_gmail():                      # Gmail files sent mail itself
                try:
                    box.m.append(f'"{box.special("sent")}"', "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                except (nc.ToolError, imaplib.IMAP4.error):
                    pass
            return f"sent to {msg['To']}: {msg['Subject']}"
    return await asyncio.to_thread(work)


@nc.tool("email_tag", "Organize emails: mark read/unread, star, add or remove tags (Gmail labels), or move them "
      "(e.g. spam to junk, to archive or trash). Ask the user before changing many messages.",
      {"uids": {"type": "array", "items": {"type": "integer"}}, "folder": {"type": "string", "description": "default inbox"},
       "add": {"type": "array", "items": {"type": "string"}, "description": "read, starred, or tag names"},
       "remove": {"type": "array", "items": {"type": "string"}},
       "move_to": {"type": "string", "description": "junk, trash, archive, inbox or a folder name"}}, ["uids"])
async def tool_email_tag(args):
    uids = uid_list(args["uids"])
    what = ", ".join(x for x in [("+" + " +".join(args["add"])) if args.get("add") else "",
                                 ("-" + " -".join(args["remove"])) if args.get("remove") else "",
                                 f"move to {args['move_to']}" if args.get("move_to") else ""] if x)
    if not what:
        raise nc.ToolError("say what to do: add, remove or move_to")
    if not await nc.allowed("email_tag", f"{len(uids)} message{'s' if len(uids) != 1 else ''}: {what}"):
        return nc.DENIED

    def flag(name):
        return {"read": "\\Seen", "seen": "\\Seen", "starred": "\\Flagged", "flagged": "\\Flagged",
                "star": "\\Flagged"}.get(name.lower())

    def work():
        with Mailbox() as box:
            box.select(args.get("folder") or "inbox", readonly=False)
            ids = ",".join(map(str, uids))
            for change, key in (("+", "add"), ("-", "remove")):
                for name in args.get(key) or []:
                    system = flag(name)
                    if system:
                        box.uid("STORE", ids, f"{change}FLAGS", f"({system})")
                    elif is_gmail():
                        box.uid("STORE", ids, f"{change}X-GM-LABELS", f'("{name}")')
                    else:
                        box.uid("STORE", ids, f"{change}FLAGS", "(" + re.sub(r"[^\w-]", "_", name) + ")")
            if args.get("move_to"):
                target = box.special(args["move_to"])
                if "MOVE" in box.m.capabilities:
                    box.uid("MOVE", ids, f'"{target}"')
                else:
                    box.uid("COPY", ids, f'"{target}"')
                    box.uid("STORE", ids, "+FLAGS", "(\\Deleted)")
                    if "UIDPLUS" in box.m.capabilities:
                        box.uid("EXPUNGE", ids)      # only these, not other mail flagged deleted elsewhere
                    else:
                        box.m.expunge()
            return f"done: {len(uids)} message{'s' if len(uids) != 1 else ''} ({what})"
    return await asyncio.to_thread(work)


EMAIL_TRIAGE = ("Check my email inbox: list the unread messages, then give me a short summary grouped by what "
                "needs a reply, what's FYI, and what looks like spam or newsletters. Suggest tags and which ones "
                "to move to junk or archive, but don't change or send anything until I say so.")


async def cmd_email(arg):
    if arg in ("setup", "set up"):
        nc.out(f"⏺ Set up email in a terminal, so your password isn't typed into the chat:\n"
            f"  {nc.CYAN}nanocode email{nc.RESET}  {nc.DIM}(Gmail and iCloud need an app password){nc.RESET}\n")
        return
    if not email_config():
        nc.out(f"{nc.YELLOW}⏺ email isn't set up yet.{nc.RESET} Run {nc.CYAN}nanocode email{nc.RESET} in a terminal first.\n")
        return
    await nc.send(EMAIL_TRIAGE if not arg else f"Email task: {arg}", display=f"/email {arg}".strip())


def email_setup():
    """`nanocode email`: connect a mailbox (IMAP for reading, SMTP for sending)."""
    import imaplib
    nc.st.config = nc.load_config()
    print(f"{nc.BOLD}Email for nanocode{nc.RESET} {nc.DIM}(IMAP/SMTP; the password is stored in {nc.tilde(nc.config_file())}, "
          f"readable only by you){nc.RESET}\n")
    current = nc.st.config.get("email") or {}
    known = current.get("address", "")
    address = input(f"  Email address{f' [{known}]' if known else ''}: ").strip() or known
    if not address:
        print("nothing to set up")
        return 1
    preset = email_preset(address) or (current.get("imap_host"), current.get("imap_port") or 993,
                                       current.get("smtp_host"), current.get("smtp_port") or 465)

    def ask(label, default):
        value = input(f"  {label}{f' [{default}]' if default else ''}: ").strip()
        return value or default

    cfg = {"address": address, "imap_host": ask("IMAP server", preset[0]), "imap_port": int(ask("IMAP port", preset[1] or 993)),
           "smtp_host": ask("SMTP server", preset[2]), "smtp_port": int(ask("SMTP port (465 or 587)", preset[3] or 465)),
           "user": ask("Username", address)}
    if "gmail" in cfg["imap_host"] or "me.com" in cfg["imap_host"]:
        print(f"  {nc.DIM}Use an app password: Gmail → myaccount.google.com/apppasswords · iCloud → account.apple.com → App-Specific Passwords{nc.RESET}")
    cfg["password"] = getpass.getpass("  Password (hidden): ") or current.get("password", "")
    print(f"  {nc.DIM}checking…{nc.RESET}")
    try:
        box = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=30)
        box.login(cfg["user"], cfg["password"])
        box.logout()
    except Exception as e:
        print(f"  {nc.RED}✗ couldn't log in: {e}{nc.RESET}")
        return 1
    nc.st.config["email"] = cfg
    nc.save_config()
    print(f"  {nc.GREEN}✓ connected{nc.RESET} · in nanocode try {nc.CYAN}/email{nc.RESET}")
    return 0
