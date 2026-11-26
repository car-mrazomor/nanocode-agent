"""Export, import and share chats.

/export writes the current chat as a readable Markdown transcript or as JSON (the session file itself).
/import and `nanocode import <file.json|url>` add an exported chat to this project's saved chats (/resume).
/share uploads the Markdown transcript as a secret GitHub gist (with the GitHub CLI, `gh`), after showing what
will be shared and asking; /unshare <url> deletes it. Secrets that look like API keys and tokens are removed
from anything that leaves this machine.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import nanocode as nc
from nanokit import cli, command
from nanokit.sessions import content_text, screen_text

# Things that look like credentials. Each match is replaced by [REDACTED].
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-(?:or-v1-|ant-|proj-)?[A-Za-z0-9_\-]{20,}"),                       # OpenAI, Anthropic, OpenRouter
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),   # GitHub
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),                                      # Slack
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                                          # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),                                             # Google
    re.compile(r"\b(?:glpat|npm|pypi)-[A-Za-z0-9_\-]{20,}"),                               # GitLab, npm, PyPI
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),     # JWT
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/\-]{16,}=*"),
    re.compile(r"(?i)(\b[\w.\-]*(?:api[_-]?key|secret|token|password|passwd)[\"']?\s*[:=]\s*[\"']?)"
               r"(?!\[REDACTED\])[^\s\"',;]{8,}"),
]


# Config sections whose every value is a secret, whatever its name: {"api_keys": {"openai": "sk-…"}},
# an MCP server's "headers" ({"Authorization": …}) and "env" ({"GITHUB_PERSONAL_ACCESS_TOKEN": …}).
SECRET_SECTIONS = ("api_keys", "headers", "env")
SECRET_ENV = re.compile(r"(?:_API_KEY|_TOKEN|_SECRET|_PASSWORD)$|^(?:GH_TOKEN|GITHUB_TOKEN)$", re.I)
MIN_SECRET = 6       # shorter values would mangle ordinary text


def config_secrets(value=None, name="", inside=False):
    """Secret values from the config (API keys, provider keys and tokens, gateway bot tokens, MCP headers and env,
    the email password): removed wherever they appear."""
    value = nc.st.config if value is None else value
    inside = inside or name.lower() in SECRET_SECTIONS or any(w in name.lower() for w in nc.SECRET_WORDS)
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            found += config_secrets(v, str(k), inside)
    elif isinstance(value, list):
        for v in value:
            found += config_secrets(v, name, inside)
    elif isinstance(value, str) and len(value) >= MIN_SECRET and inside:
        found.append(value)
    return found


def env_secrets(environ=None):
    """Values of environment variables named like credentials (*_API_KEY, *_TOKEN, *_SECRET, GH_TOKEN…)."""
    environ = os.environ if environ is None else environ
    return [v for k, v in environ.items() if SECRET_ENV.search(k) and v and len(v) >= MIN_SECRET]


def redact_text(text, extra=()):
    """(text without secrets, how many were removed). `extra`: more known secret values."""
    count = 0
    known = {v for v in [*config_secrets(), *env_secrets(), *extra] if isinstance(v, str) and len(v) >= MIN_SECRET}
    for secret in sorted(known, key=len, reverse=True):    # longest first: one key may contain another
        count += text.count(secret)
        text = text.replace(secret, "[REDACTED]")
    for pattern in SECRET_PATTERNS:
        def sub(m):
            nonlocal count
            count += 1
            return (m.group(1) if pattern.groups else "") + "[REDACTED]"
        text = pattern.sub(sub, text)
    return text, count


def redact_value(value):
    """A JSON value with secrets removed from every string in it; (value, count)."""
    total = 0

    def walk(v):
        nonlocal total
        if isinstance(v, str):
            v, n = redact_text(v)
            total += n
            return v
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return walk(value), total


# --- The current chat, as data and as Markdown ---

def current_chat():
    """The current chat as its session file holds it (saved first), or None when there's nothing yet."""
    if not nc.st.session.get("title"):
        return None
    nc.save_session()
    path = nc.sessions_dir() / f"{nc.st.session['id']}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def to_markdown(data):
    created = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("created") or time.time()))
    usage = data.get("usage") or {}
    lines = [f"# {data.get('title') or 'nanocode chat'}", "",
             f"*nanocode chat · {data.get('model') or '?'} · {created}"
             + (f" · {nc.fmt_tokens(usage.get('input') or 0)} in, {nc.fmt_tokens(usage.get('output') or 0)} out"
                if usage.get("input") else "") + "*", ""]
    messages = data.get("messages") or []
    if not messages:
        lines += ["```text", screen_text(data.get("screen") or "").replace("```", "ʼʼʼ"), "```", ""]
        return "\n".join(lines)
    for m in messages:
        role = m.get("role")
        text = content_text(m.get("content")).strip()
        if role == "user":
            lines += ["## You", "", text, ""]
        elif role == "assistant":
            if text:
                lines += ["## nanocode", "", text, ""]
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                args = fn.get("arguments") or ""
                try:
                    args = json.dumps(json.loads(args), indent=1, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
                lines += [f"**Tool `{fn.get('name')}`**", "", "```json", args, "```", ""]
        elif role == "tool":
            output = text if len(text) <= 3000 else text[:3000] + f"\n… ({len(text) - 3000} more characters)"
            lines += ["<details><summary>result</summary>", "", "```text", output.replace("```", "ʼʼʼ"), "```", "",
                      "</details>", ""]
    return "\n".join(lines)


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "chat").strip("-").lower()[:40] or "chat"


# --- /export ---

@command("/export", "save this chat to a file: /export [md|json] [path]")
async def cmd_export(arg):
    words = arg.split(maxsplit=1)
    kind = "md"
    if words and words[0].lower() in ("md", "markdown", "json"):
        kind = "json" if words.pop(0).lower() == "json" else "md"
    data = current_chat()
    if data is None:
        nc.out(f"{nc.DIM}nothing to export yet: send a message first{nc.RESET}\n")
        return
    path = Path(os.path.expanduser(words[0])) if words else Path(f"{slug(data.get('title'))}.{kind}")
    if words and path.suffix.lower() in (".json", ".md") and len(arg.split()) == 1:
        kind = "json" if path.suffix.lower() == ".json" else "md"
    text = json.dumps(data, indent=1, ensure_ascii=False) if kind == "json" else to_markdown(data)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        nc.out(f"{nc.RED}⏺ can't write {path}: {e}{nc.RESET}\n")
        return
    note = " (it holds the whole chat, tool output included: check before you share it)" if kind == "json" else ""
    nc.out(f"{nc.GREEN}⏺ Exported{nc.RESET} {nc.DIM}→ {nc.display_path(path.resolve())}{note}{nc.RESET}\n")


# --- /import, nanocode import ---

def load_export(source):
    """A chat exported with /export json, from a file or a URL."""
    if re.match(r"https?://", source):
        import httpx
        url = source
        gist = re.match(r"https://gist\.github\.com/(?:[\w-]+/)?([0-9a-f]+)/?$", url)
        if gist:                               # a gist page: its raw content
            url = f"https://gist.githubusercontent.com/raw/{gist.group(1)}"
        response = httpx.get(url, follow_redirects=True, timeout=30)
        response.raise_for_status()
        text = response.text
    else:
        text = Path(os.path.expanduser(source)).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("that isn't a JSON chat (export one with /export json)")
    if not isinstance(data, dict) or not (isinstance(data.get("messages"), list) or data.get("screen")):
        raise ValueError("that JSON isn't a nanocode chat (no messages)")
    return data


def import_chat(data):
    """Adds the chat to this project's sessions; returns (path, data)."""
    data = dict(data)
    nc.ensure_project_dir()
    nc.sessions_dir().mkdir(parents=True, exist_ok=True)
    sid = str(data.get("id") or "")
    if not re.match(r"^[A-Za-z0-9._-]{1,64}$", sid) or (nc.sessions_dir() / f"{sid}.json").exists():
        sid = nc.make_session_id()
    data.update(id=sid, title=str(data.get("title") or "imported chat"), updated=time.time(),
                imported=time.time())
    data.setdefault("created", time.time())
    data.setdefault("backend", "api")
    data.setdefault("messages", [])
    data["sdk_session_id"] = None if data.get("backend") == "sdk" else data.get("sdk_session_id")
    path = nc.sessions_dir() / f"{sid}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path, data


def import_note(data):
    if data.get("backend") == "sdk":
        return "a Claude subscription chat: its screen is kept, but the conversation stays with Claude Code"
    if nc.st.backend and data.get("backend") != nc.st.backend:
        return f"made with {data.get('backend')}: /resume lists it when that provider is in use"
    return "open it with /resume"


@command("/import", "add an exported chat (JSON file or URL) to this project's chats")
async def cmd_import(arg):
    if not arg.strip():
        nc.out(f"{nc.DIM}usage: /import <file.json|url>{nc.RESET}\n")
        return
    try:
        data = await asyncio.get_running_loop().run_in_executor(None, load_export, arg.strip())
        path, data = import_chat(data)
    except Exception as e:
        nc.out(f"{nc.RED}⏺ can't import: {e}{nc.RESET}\n")
        return
    nc.out(f"{nc.GREEN}⏺ Imported{nc.RESET} {data['title']} {nc.DIM}({import_note(data)}){nc.RESET}\n")


@cli("import", "add an exported chat (file.json or URL) to this project's chats")
def import_cli(argv):
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print("usage: nanocode import <file.json|url>", file=sys.stderr)
        return 2
    try:
        path, data = import_chat(load_export(argv[0]))
    except Exception as e:
        print(f"can't import: {e}", file=sys.stderr)
        return 1
    print(f"imported \"{data['title']}\" → {nc.tilde(path)} ({import_note(data)})")
    return 0


# --- /share, /unshare: secret GitHub gists ---

GIST_URL = re.compile(r"https://gist\.github\.com/[\w./-]+")


async def gh(*args, timeout=60):
    """(exit code, output) of the GitHub CLI."""
    return await nc.run_cmd("gh", *args, timeout=timeout)


async def gh_ready():
    """None when `gh` can make gists, else what's missing."""
    if not shutil.which("gh"):
        return "the GitHub CLI isn't installed (https://cli.github.com), so there's nowhere to share to"
    code, _ = await gh("auth", "status", timeout=20)
    if code != 0:
        return "the GitHub CLI isn't logged in: run `gh auth login`"
    return None


@command("/share", "share this chat as a secret GitHub gist (asks first)")
async def cmd_share(arg):
    data = current_chat()
    if data is None:
        nc.out(f"{nc.DIM}nothing to share yet: send a message first{nc.RESET}\n")
        return
    if nc.st.headless:
        nc.out(f"{nc.RED}⏺ /share asks before uploading, so it only works in the app{nc.RESET}\n")
        return
    problem = await gh_ready()
    if problem:
        nc.out(f"{nc.RED}⏺ {problem}{nc.RESET}\n")
        return
    text, removed = redact_text(to_markdown(data))
    title = redact_text(str(data.get("title") or ""))[0]     # the gist's description and file name are public too
    folder = Path(tempfile.mkdtemp(prefix="nanocode-share-"))
    path = folder / f"{slug(title)}.md"
    path.write_text(text, encoding="utf-8")
    preview = "\n".join(f"     {nc.DIM}│ {line[:100]}{nc.RESET}" for line in text.split("\n")[:12])
    nc.out(f"\n{nc.BOLD}This will be uploaded{nc.RESET} {nc.DIM}as a secret gist (anyone with the link can read it): "
           f"{len(text):,} characters, {text.count(chr(10)) + 1} lines"
           + (f", {removed} secret{'s' * (removed != 1)} removed" if removed else "")
           + f"\n     full text: {path}{nc.RESET}\n{preview}\n     {nc.DIM}│ …{nc.RESET}")
    try:
        if nc.st.permission_lock is None:
            nc.st.permission_lock = asyncio.Lock()
        async with nc.st.permission_lock:
            ok = await nc.ask_permission("share", f"upload \"{title}\" to a secret gist", False)
        if not ok:
            return
        code, output = await gh("gist", "create", "--desc", f"nanocode chat: {title}", str(path))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    url = GIST_URL.search(output or "")
    if code != 0 or not url:
        nc.out(f"{nc.RED}⏺ gh couldn't make the gist: {(output or '').strip()[-300:]}{nc.RESET}\n")
        return
    nc.copy_to_clipboard(url.group(0))
    nc.out(f"{nc.GREEN}⏺ Shared{nc.RESET} {url.group(0)} {nc.DIM}(copied · /unshare {url.group(0)} deletes it)"
           f"{nc.RESET}\n")


@command("/unshare", "delete a chat shared with /share: /unshare <gist url>")
async def cmd_unshare(arg):
    match = re.search(r"([0-9a-f]{20,})", arg.strip())
    if not match:
        nc.out(f"{nc.DIM}usage: /unshare <gist url>{nc.RESET}\n")
        return
    problem = await gh_ready()
    if problem:
        nc.out(f"{nc.RED}⏺ {problem}{nc.RESET}\n")
        return
    code, output = await gh("gist", "delete", match.group(1), "--yes")
    if code != 0 and "unknown flag" in output:   # an older gh doesn't ask, and doesn't know --yes
        code, output = await gh("gist", "delete", match.group(1))
    if code != 0:
        nc.out(f"{nc.RED}⏺ couldn't delete it: {output.strip()[-300:]}{nc.RESET}\n")
        return
    nc.out(f"{nc.GREEN}⏺ Unshared{nc.RESET} {nc.DIM}the gist is deleted{nc.RESET}\n")

