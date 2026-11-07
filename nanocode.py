#!/usr/bin/env python3
"""nanocode: a small coding agent for the terminal.

Runs on a Claude subscription (Claude Agent SDK), any OpenRouter model, or local models via Ollama.
https://github.com/car-mrazomor/nanocode-agent
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import dataclasses
import difflib
import fnmatch
import getpass
import glob
import io
import json
import os
import platform
import random
import re
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
from pathlib import Path

import httpx
from rich.console import Console
from rich.markdown import Markdown

import warnings

warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")
try:
    import claude_agent_sdk as sdk
except ImportError:  # e.g. no build for this device; OpenRouter and Ollama still work
    sdk = None

__version__ = "0.3.0"
REPO = "car-mrazomor/nanocode-agent"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/master"

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, YELLOW, RED = "\033[34m", "\033[36m", "\033[32m", "\033[33m", "\033[31m"
USER_BG = "\033[48;5;237m"
ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# Grayish theme with accents from the logo gradient (cyan → blue → violet → pink), toned down.
THEME = {
    "bg": "#141518", "panel": "#1e2024", "panel2": "#26292e", "border": "#34373d",
    "text": "#d5d6da", "muted": "#80838c", "faint": "#555860",
    "accent": "#7fa7c9", "accent2": "#9d8fc4", "pink": "#c08aae", "cyan": "#78b8c4",
    "success": "#8fb48b", "error": "#d07f7f", "warning": "#cfad73",
}


def _rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def fg(name):
    return "\033[38;2;{};{};{}m".format(*_rgb(THEME.get(name, name)))


def bg(name):
    return "\033[48;2;{};{};{}m".format(*_rgb(THEME.get(name, name)))

MAX_TOOL_OUTPUT = 30_000
MAX_OUTPUT_CHARS = 400_000
READ_LIMIT = 2000
IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next", ".nuxt", ".cache", ".idea",
    ".gradle", "target", ".terraform", ".nanocode",
}

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_DEFAULT_URL = "http://localhost:11434"

OPENROUTER_MODELS = {
    "openrouter/auto-code": "router for coding agents · price of picked model",
    "openrouter/auto": "general router · price of picked model",
    "z-ai/glm-5.3": "$0.07 / $1.40",
    "z-ai/glm-5.3-flash": "$0.15 / $0.50",
    "z-ai/glm-5.3-prime": "$2.80 / $8.80 · faster GLM 5.3",
    "anthropic/claude-opus-5.5": "$4 / $20",
    "anthropic/claude-sonnet-5.5": "$2 / $10",
    "anthropic/claude-haiku-5.5": "$0.10 / $0.50",
    "openai/gpt-5.6-sol": "$4 / $20",
    "openai/gpt-5.6-terra": "$2 / $12",
    "openai/gpt-5.6-luna": "$0.20 / $1.20",
    "google/gemini-4-argon": "$2 / $10",
    "google/gemini-3.8-flash": "$0.83 / $4.12",
    "deepseek/deepseek-v4-pro": "$0.67 / $1.33",
    "deepseek/deepseek-v4.1-flash": "$0.05 / $0.57",
    "moonshotai/kimi-k3.1-pro": "$2 / $10",
    "moonshotai/kimi-k2.7-code": "$0.60 / $3.50",
    "x-ai/grok-4.7": "$2 / $6",
    "minimax/minimax-m3.1-flash": "$0.07 / $0.30",
}
SDK_MODELS = {
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
    "claude-fable-5-1": "Fable 5.1",
    "opus": "alias, latest Opus",
    "sonnet": "alias, latest Sonnet",
    "haiku": "alias, latest Haiku",
}
PROVIDERS = {
    "claude": ("sdk", "Claude subscription", "Claude Agent SDK, uses your Claude Max/Pro login"),
    "openrouter": ("api", "OpenRouter", "any cloud model (Claude, GLM, GPT, Gemini...), pay per token"),
    "ollama": ("ollama", "Ollama", "local models on this machine, free and private"),
}
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "openrouter": "openrouter/auto-code"}

LEARN_PROMPT = (
    "Review our conversation so far and learn from it. Look for: mistakes you made and how they were fixed, "
    "things that took several attempts, corrections or preferences from me, and multi-step procedures likely "
    "to come up again. Save each durable lesson with `remember` (one short, specific sentence; scope global only "
    "if it applies to all projects) and each reusable procedure with `save_skill`. Skip anything trivial or "
    "one-off, and don't save duplicates of what's already in memory. Finish with a short list of what you saved."
)
RECAP_PROMPT = (
    "Give me a short recap of this session: what I asked for, what you did (files changed, commands run, results), "
    "and what's still open or worth doing next. A few short bullet points. Don't use any tools."
)
COMPACT_PROMPT = (
    "Summarize our conversation so far so we can continue the work with a fresh context: the goal, key decisions, "
    "files created or changed, current state, and next steps. Be concise and concrete; include exact file paths."
)


# --- Paths: app data in ~/.nanocode, project data in ./.nanocode ---

def home_dir():
    return Path(os.environ.get("NANOCODE_HOME") or Path.home() / ".nanocode")


def config_file():
    return home_dir() / "config.json"


def global_memory_file():
    return home_dir() / "NANOCODE.md"


def history_file():
    return home_dir() / "history"


def log_file():
    return home_dir() / "logs" / "error.log"


def project_dir():
    return Path.cwd() / ".nanocode"


def project_memory_file():
    return project_dir() / "memory.md"


def sessions_dir():
    return project_dir() / "sessions"


def skill_dirs():
    """Earlier wins when two skills share a name."""
    dirs = [project_dir() / "skills", Path.cwd() / ".claude" / "skills",
            home_dir() / "skills", Path.home() / ".claude" / "skills"]
    if rea_enabled():
        dirs.append(rea_dir() / "node_modules" / "rea-agents" / "skills")
    return dirs


def ensure_project_dir():
    """Creates .nanocode/ with its own .gitignore so memory and chats never get pushed, in any repo."""
    project_dir().mkdir(parents=True, exist_ok=True)
    ignore = project_dir() / ".gitignore"
    wanted = ["sessions/", "memory.md", "undo/", "shells/"]
    try:
        current = ignore.read_text().splitlines() if ignore.exists() else ["# nanocode: private, not committed (skills/ can be)"]
        missing = [w for w in wanted if w not in current]
        if missing:
            ignore.write_text("\n".join(current + missing) + "\n")
    except OSError:
        pass


def migrate_legacy():
    """Moves data from older versions (~/.config/nanocode, ~/.nanocode_history) into ~/.nanocode.
    Skipped when NANOCODE_HOME points elsewhere, so a custom or test home never takes your real files."""
    if os.environ.get("NANOCODE_HOME"):
        return
    legacy = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "nanocode"
    home = home_dir()
    moves = [(legacy / "config.json", home / "config.json"), (legacy / "NANOCODE.md", home / "NANOCODE.md"),
             (Path.home() / ".nanocode_history", home / "history")]
    if (legacy / "skills").is_dir():
        moves += [(d, home / "skills" / d.name) for d in (legacy / "skills").iterdir()]
    for src, dst in moves:
        if src.exists() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            except OSError:
                pass
    for d in (legacy / "skills", legacy):
        try:
            d.rmdir()
        except OSError:
            pass


def log_error(exc):
    try:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".log.old"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} nanocode {__version__}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


def tilde(path):
    path, home = str(path), str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def default_num_ctx():
    """Ollama context that fits in RAM. Too big spills onto the CPU: on an 8 GB Mac, 32k took 12 min vs 10 s at 8k."""
    ram_gb = None
    try:
        ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        if os.name == "nt":
            try:
                import ctypes

                class MemStatus(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

                status = MemStatus()
                status.dwLength = ctypes.sizeof(MemStatus)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
                ram_gb = status.ullTotalPhys / 1e9
            except Exception:
                pass
    if not ram_gb:
        return 8192
    return 8192 if ram_gb <= 12 else 16384 if ram_gb <= 24 else 32768


class State:
    def __init__(self):
        self.backend = None  # "sdk" | "api" | "ollama"
        self.model = None
        self.config = {}
        self.client = None
        self.messages = []  # OpenAI-format history for api/ollama; the SDK keeps its own
        self.session = {}
        self.usage = {"input": 0, "output": 0, "cost": 0.0}
        self.task = None
        self.task_started = 0.0
        self.output = ""
        self.version = 0
        self.live_md = None
        self.live = ""
        self.live_rendered_at = 0.0
        self.scroll = 0
        self.pending_permission = None
        self.always_allow = set()
        self.auto_approve = False
        self.headless = False
        self.undo = []
        self.turn_errors = 0
        self.last_error = None
        self.routed_model = None
        self.openrouter_key = None
        self.ollama_url = OLLAMA_DEFAULT_URL
        self.num_ctx = default_num_ctx()
        self.ollama_models = []
        self.trim_notice = False
        self.output_started = False
        self.ctx_tokens = 0
        self.ctx_budget = 0
        self.app = None
        self.output_window = None
        self.input_box = None
        self.rea = None
        self.home = True  # the start screen, until the first message or command
        self.sidebar_on = True
        self.dialog = None
        self.interrupt_armed = 0.0
        self.activity = []  # (tool, preview, is_error) this session, for /recap
        self.changed = {}  # path -> [lines added, lines removed] this session
        self.shells = {}  # name -> shell info, see Shells


st = State()


# --- Output: the scrolling area in the UI, or stdout in headless mode (-p) ---

def headless_write(stream, text):
    stream.write(text if stream.isatty() else ANSI_RE.sub("", text))
    stream.flush()


def invalidate():
    st.version += 1
    if st.app is not None and st.app.is_running:
        st.app.invalidate()


def out(text="", end="\n"):
    if st.headless:
        headless_write(sys.stderr, text + end)
        return
    st.output += text + end
    if len(st.output) > MAX_OUTPUT_CHARS:
        cut = st.output.find("\n", len(st.output) - MAX_OUTPUT_CHARS // 2)
        st.output = st.output[cut + 1:]
    invalidate()


def messages_width():
    """Width of the transcript column (terminal minus sidebar and padding)."""
    if st.app is None or not st.app.is_running:
        return 80
    columns = st.app.output.get_size().columns
    if sidebar_visible():
        columns -= SIDEBAR_WIDTH
    return max(20, columns - 4)


def render_markdown(text):
    width = max(20, messages_width() - 3)
    from rich.theme import Theme
    buf = io.StringIO()
    theme = Theme({"markdown.code": f"bold {THEME['cyan']}", "markdown.link": THEME["accent"],
                   "markdown.h1": f"bold {THEME['text']}", "markdown.h2": f"bold {THEME['text']}",
                   "markdown.block_quote": THEME["muted"], "markdown.item.bullet": THEME["accent2"]})
    Console(file=buf, force_terminal=True, color_system="truecolor", width=width, theme=theme).print(
        Markdown(text, code_theme="github-dark"))
    return buf.getvalue().rstrip("\n")


def render_live():
    st.live = "\n" + "\n".join("   " + line for line in render_markdown(st.live_md).split("\n"))
    st.live_rendered_at = time.monotonic()
    invalidate()


def stream_text(chunk):
    if st.live_md is None:
        st.live_md = ""
        if st.headless and st.output_started:
            headless_write(sys.stdout, "\n")
    st.live_md += chunk
    if st.headless:
        headless_write(sys.stdout, chunk)
        st.output_started = True
    elif time.monotonic() - st.live_rendered_at > 0.05:  # re-rendering markdown on every token is slow
        render_live()



def stream_end():
    if st.live_md is None:
        return
    if st.headless:
        headless_write(sys.stdout, "\n")
        st.live_md = None
        return
    final = ""
    if st.live_md.strip():
        render_live()
        final = st.live
    st.live_md, st.live = None, ""
    if final:
        out(final)
    else:
        invalidate()


_view_cache = {"key": None, "text": ""}


def visible_output():
    key = (st.version, st.scroll)
    if _view_cache["key"] != key:
        text = st.output + st.live
        if st.scroll:
            lines = text.split("\n")
            text = "\n".join(lines[: max(1, len(lines) - st.scroll)])
        _view_cache.update(key=key, text=text)
    return _view_cache["text"]


def fmt_tokens(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


DONE_VERBS = ["Cooked", "Brewed", "Baked", "Crafted", "Forged", "Simmered", "Whisked", "Tinkered", "Hammered",
              "Conjured", "Churned", "Stirred", "Sculpted", "Polished", "Assembled", "Wrangled", "Distilled",
              "Kneaded", "Toasted", "Steeped", "Shaped", "Stitched", "Tuned", "Seasoned"]


def fmt_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def fmt_cost(cost):
    if st.backend == "ollama":
        return "free (local)"
    cost = cost or 0
    amount = f"${cost:.4f}" if cost < 1 else f"${cost:.2f}"
    return f"{amount} (in your plan)" if st.backend == "sdk" else amount


def show_usage(tokens_in, tokens_out, cost, seconds):
    """The dim line after every response: time, tokens and cost."""
    st.usage["input"] += tokens_in
    st.usage["output"] += tokens_out
    st.usage["cost"] += cost or 0
    verb = random.choice(DONE_VERBS)
    parts = [f"{verb} for {fmt_duration(seconds)}", pretty_model(st.model), f"{fmt_tokens(tokens_in)} in",
             f"{fmt_tokens(tokens_out)} out", fmt_cost(cost), time.strftime('%H:%M')]
    out(f"\n   {fg('accent2')}▣{RESET} {fg('muted')}{' · '.join(parts)}{RESET}")


# --- Permissions ---

RISKY_TOOLS = {"write", "edit", "bash"}
DANGEROUS = re.compile(
    r"\brm\s+-[a-z]*r[a-z]*\s+(/|~|\$HOME|\*)(\s|$)|\bmkfs\b|\bdd\s+if=|:\(\)\s*\{|>\s*/dev/sd|"
    r"\bshutdown\b|\breboot\b|\bgit\s+push\b.*(--force|-f\b)|\bgit\s+reset\s+--hard\b|\bchmod\s+-R\s+777\s+/",
    re.I,
)


def answer_permission(key):
    if st.pending_permission is not None and not st.pending_permission.done():
        st.pending_permission.set_result(key)


async def allowed(tool_name, preview):
    dangerous = tool_name in ("bash", "shell") and bool(DANGEROUS.search(preview))
    if not dangerous and (st.auto_approve or tool_name in st.always_allow):
        return True
    if st.headless:
        reason = "looks dangerous" if dangerous else "run with --yes to allow"
        out(f"  {DIM}⎿  denied {tool_name} ({reason}){RESET}")
        return False
    stream_end()
    warn = f"   {fg('error')}{BOLD}⚠ this looks destructive{RESET}\n" if dangerous else ""
    options = "y allow · n deny" if dangerous else "y allow · a always · n deny"
    out(f"\n{warn}   {fg('warning')}△ Permission{RESET} {fg('text')}{tool_name}{RESET} "
        f"{fg('muted')}{preview[:120]}{RESET}\n     {fg('faint')}{options}{RESET}")
    st.pending_permission = asyncio.get_running_loop().create_future()
    try:
        answer = await st.pending_permission
    finally:
        st.pending_permission = None
    if answer == "a" and not dangerous:
        st.always_allow.add(tool_name)
    ok = answer in ("y", "a")
    out(f"     {fg('success') if ok else fg('error')}{'allowed' if ok else 'denied'}{RESET}")
    return ok


# --- Tools ---

class ToolError(Exception):
    pass


TOOLS = {}
TOOL_PREVIEW = {"rea_search": "query", "rea_schema": "tool", "rea_call": "tool",
                "read": "path", "write": "path", "edit": "path", "glob": "pattern", "grep": "pattern",
                "bash": "command", "shell": "command", "remember": "fact", "skill": "name", "save_skill": "name"}
DENIED = "error: the user denied this action. Ask what they want instead, or try a different approach."


def tool(name, description, properties, required=()):
    def wrap(fn):
        TOOLS[name] = {"name": name, "description": description, "fn": fn,
                       "schema": {"type": "object", "properties": properties, "required": list(required)}}
        return fn
    return wrap


def cap(text, limit=MAX_TOOL_OUTPUT):
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-limit // 3:]
    return f"{head}\n\n… [{len(text) - limit} characters cut] …\n\n{tail}"


def show_tool_call(name, args):
    key = TOOL_PREVIEW.get(name)
    preview = str(args.get(key, "")) if key else (str(next(iter(args.values()), "")) if args else "")
    if name == "shell":
        preview = " ".join(str(x) for x in (args.get("action"), args.get("name")) if x) + \
            (f": {preview}" if preview else "")
    preview = preview.replace("\n", " ")
    if len(preview) > 70:
        preview = preview[:67] + "…"
    icon = TOOL_ICONS.get(name, "⚙")
    out(f"\n   {fg('muted')}{icon} {fg('text')}{name.replace('_', ' ').capitalize()}{RESET} {fg('muted')}{preview}{RESET}")


TOOL_ICONS = {"read": "→", "write": "←", "edit": "←", "glob": "✱", "grep": "✱", "bash": "$", "shell": "$",
              "remember": "◆", "skill": "◈", "save_skill": "◈", "rea_search": "%", "rea_schema": "%", "rea_call": "%"}


def show_tool_result(text, is_error):
    lines = text.strip().split("\n") if text.strip() else ["(no output)"]
    first = lines[0][:90]
    more = f" … +{len(lines) - 1} lines" if len(lines) > 1 else ""
    color = fg("error") if is_error else fg("faint")
    out(f"     {color}{first}{more}{RESET}")


async def run_tool(name, args):
    """Runs a tool by name; never raises. Returns (text, is_error)."""
    entry = TOOLS.get(name)
    if not isinstance(args, dict):
        args = {}
    show_tool_call(name, args)
    if entry is None:
        text, is_error = f"error: unknown tool '{name}'. Available: {', '.join(TOOLS)}", True
    else:
        missing = [k for k in entry["schema"]["required"] if args.get(k) in (None, "")]
        if missing:
            text, is_error = f"error: missing required argument(s): {', '.join(missing)}", True
        else:
            try:
                text = await entry["fn"](args)
                is_error = text.startswith("error:")
            except ToolError as e:
                text, is_error = f"error: {e}", True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_error(e)
                text, is_error = f"error: {type(e).__name__}: {e}", True
    if is_error:
        st.turn_errors += 1
    key = TOOL_PREVIEW.get(name)
    st.activity.append((name, str(args.get(key, "")) if key else "", is_error))
    text = cap(text)
    show_tool_result(text, is_error)
    return text, is_error


def resolve(path):
    return Path(os.path.expanduser(str(path)))


def display_path(path):
    """Relative to the project when inside it, absolute otherwise."""
    path = os.path.abspath(path)
    cwd = os.getcwd()
    return os.path.relpath(path, cwd) if path == cwd or path.startswith(cwd + os.sep) else path


def read_text(path):
    """Reads keeping line endings as they are, so edits don't turn CRLF files into LF."""
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return False


def not_found(path):
    parent = path.parent if str(path.parent) else Path(".")
    hint = ""
    try:
        close = difflib.get_close_matches(path.name, os.listdir(parent), n=3, cutoff=0.6)
        if close:
            hint = f". Did you mean: {', '.join(str(parent / c) for c in close)}?"
    except OSError:
        pass
    return ToolError(f"{path} not found{hint}")


def track_change(path, old, new):
    added, removed = diff_stats(old or "", new)
    total = st.changed.setdefault(display_path(path), [0, 0])
    total[0] += added
    total[1] += removed


def record_undo(path, previous):
    st.undo.append({"path": str(path), "content": previous})
    del st.undo[:-50]


def show_diff(old, new, path):
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1))[2:]
    shown = 0
    for line in diff:
        if shown >= 16:
            out(f"    {DIM}… {len(diff) - shown} more diff lines{RESET}")
            break
        if line.startswith("@@"):
            continue
        color = GREEN if line.startswith("+") else RED if line.startswith("-") else DIM
        out(f"    {color}{line[:160]}{RESET}")
        shown += 1


def diff_stats(old, new):
    added = removed = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


@tool("read", "Read a text file with line numbers. For big files use offset (1-based line) and limit. "
      "Reading a directory lists its entries.",
      {"path": {"type": "string"}, "offset": {"type": "integer", "description": "first line to read, 1-based"},
       "limit": {"type": "integer", "description": f"max lines (default {READ_LIMIT})"}}, ["path"])
async def tool_read(args):
    path = resolve(args["path"])
    if path.is_dir():
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        names = [p.name + ("/" if p.is_dir() else "") for p in entries[:300]]
        more = f"\n… +{len(entries) - 300} more" if len(entries) > 300 else ""
        return f"{path} is a directory:\n" + "\n".join(names) + more
    if not path.exists():
        raise not_found(path)
    if is_binary(path):
        return f"{path} is a binary file ({path.stat().st_size} bytes); not shown."
    offset = max(1, int(args.get("offset") or 1))
    limit = max(1, int(args.get("limit") or READ_LIMIT))
    chosen, total = [], 0
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for total, line in enumerate(f, 1):
            if offset <= total < offset + limit:
                chosen.append(line.rstrip("\r\n"))
    if not chosen:
        return f"{path} is empty." if total == 0 else f"{path} has {total} lines; nothing at offset {offset}."
    body = "\n".join(f"{n}│{line[:2000]}" for n, line in enumerate(chosen, offset))
    end = offset + len(chosen) - 1
    if end < total or offset > 1:
        body += f"\n(lines {offset}-{end} of {total}; use offset/limit to read more)"
    return body


@tool("write", "Create or overwrite a file with the full content. Prefer `edit` for changes to existing files.",
      {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])
async def tool_write(args):
    path, content = resolve(args["path"]), args["content"]
    if path.is_dir():
        raise ToolError(f"{path} is a directory")
    old = read_text(path) if path.is_file() else None
    if old is not None:
        show_diff(old, content, path)
    if not await allowed("write", str(path)):
        return DENIED
    record_undo(path, old)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, content)
    track_change(path, old, content)
    added, removed = diff_stats(old or "", content)
    verb = "created" if old is None else "wrote"
    return f"{verb} {path} ({len(content.splitlines())} lines, +{added} -{removed})"


@tool("edit", "Replace exact text in a file. `old` must match the file exactly (including indentation) and be "
      "unique unless all=true. Read the file first.",
      {"path": {"type": "string"}, "old": {"type": "string", "description": "exact text to replace"},
       "new": {"type": "string", "description": "replacement text"},
       "all": {"type": "boolean", "description": "replace every occurrence"}}, ["path", "old"])
async def tool_edit(args):
    path, old, new = resolve(args["path"]), args["old"], args.get("new") or ""
    if not path.is_file():
        raise not_found(path)
    text = read_text(path)
    old, new = match_edit(text, old, new)
    count = text.count(old)
    if count == 0:
        raise ToolError(f"`old` text not found in {path}. Read the file again: it must match exactly, "
                        "including whitespace and indentation.")
    replace_all = bool(args.get("all"))
    if count > 1 and not replace_all:
        raise ToolError(f"`old` text appears {count} times in {path}. Include more surrounding lines to make "
                        "it unique, or set all=true.")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    show_diff(text, updated, path)
    if not await allowed("edit", str(path)):
        return DENIED
    record_undo(path, text)
    write_text(path, updated)
    track_change(path, text, updated)
    return f"edited {path} ({count if replace_all else 1} replacement{'s' if replace_all and count > 1 else ''})"


LINE_NUMBER = re.compile(r"(?m)^\s*\d+│ ?")


def match_edit(text, old, new):
    """Forgives common model mistakes: copied line numbers, extra indentation, LF vs CRLF.
    Returns the (old, new) pair to use; old is unchanged if nothing better matches."""
    if old in text:
        return old, new
    candidates = []
    if LINE_NUMBER.search(old):
        candidates.append((LINE_NUMBER.sub("", old), LINE_NUMBER.sub("", new)))
    dedented = textwrap.dedent(old)
    if dedented != old:
        indent = len(old.split("\n")[0]) - len(old.split("\n")[0].lstrip())
        candidates.append((dedented, "\n".join(line[indent:] if line[:indent].strip() == "" else line
                                               for line in new.split("\n"))))
    for cand_old, cand_new in [(old, new)] + candidates:
        if "\r\n" in text and "\r\n" not in cand_old:
            cand_old, cand_new = cand_old.replace("\n", "\r\n"), cand_new.replace("\n", "\r\n")
        if cand_old and cand_old in text:
            return cand_old, cand_new
    return old, new


def glob_to_regex(pattern):
    i, out_re = 0, ""
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out_re += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out_re += ".*"
            i += 2
            continue
        out_re += {"*": "[^/]*", "?": "[^/]"}.get(c, re.escape(c))
        i += 1
    return re.compile(out_re + r"\Z")


def walk(base, include_dirs=False):
    """Yields (relative posix path, absolute path), skipping vendored/cache folders."""
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        rel_root = os.path.relpath(root, base).replace(os.sep, "/")
        prefix = "" if rel_root == "." else rel_root + "/"
        if include_dirs:
            for d in dirs:
                yield prefix + d, os.path.join(root, d)
        for f in files:
            yield prefix + f, os.path.join(root, f)


@tool("glob", "Find files by glob pattern (e.g. **/*.py, src/*.ts). Newest first. Skips .git, node_modules, "
      "caches.", {"pattern": {"type": "string"}, "path": {"type": "string", "description": "folder (default .)"}},
      ["pattern"])
async def tool_glob(args):
    base = resolve(args.get("path") or ".")
    parts = Path(os.path.expanduser(args["pattern"])).parts
    fixed = []
    for part in parts:
        if any(c in part for c in "*?["):
            break
        fixed.append(part)
    if len(fixed) == len(parts):
        target = base / Path(*parts) if parts else base
        return display_path(target) if target.exists() else "no files found"
    if fixed:
        base = base / Path(*fixed)
    if not base.is_dir():
        raise not_found(base)
    regex = glob_to_regex("/".join(parts[len(fixed):]))
    matches = [abs_path for rel, abs_path in walk(base, include_dirs=True) if regex.match(rel)]

    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0

    matches.sort(key=mtime, reverse=True)
    if not matches:
        return "no files found"
    shown = [display_path(m) for m in matches[:200]]
    more = f"\n… +{len(matches) - 200} more" if len(matches) > 200 else ""
    return "\n".join(shown) + more


@tool("grep", "Search file contents with a regex. Returns path:line:text. Skips .git, node_modules, binaries.",
      {"pattern": {"type": "string"}, "path": {"type": "string", "description": "file or folder (default .)"},
       "include": {"type": "string", "description": "only files matching this glob, e.g. *.py"},
       "ignore_case": {"type": "boolean"}}, ["pattern"])
async def tool_grep(args):
    pattern, base = args["pattern"], resolve(args.get("path") or ".")
    include, ignore_case = args.get("include"), bool(args.get("ignore_case"))
    if not base.exists():
        raise not_found(base)
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--line-number", "--no-heading", "--color=never", "--max-columns=300", "--max-count=50"]
        cmd += ["-i"] if ignore_case else []
        cmd += ["--glob", include] if include else []
        cmd += ["-e", pattern, str(base)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                    stdin=subprocess.DEVNULL)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
        except asyncio.TimeoutError:
            proc.kill()
            raise ToolError("search timed out after 30s; narrow the path or pattern")
        if proc.returncode == 1:
            return "no matches"
        if proc.returncode not in (0, 1):
            raise ToolError(stderr.decode(errors="replace").strip()[:500])
        lines = stdout.decode(errors="replace").splitlines()
    else:
        try:
            regex = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}")
        files = [base] if base.is_file() else [Path(p) for _, p in walk(base)]
        lines = []
        for path in files:
            if include and not fnmatch.fnmatch(path.name, include):
                continue
            try:
                if path.stat().st_size > 2_000_000 or is_binary(path):
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if regex.search(line):
                            lines.append(f"{display_path(path)}:{n}:{line.rstrip()[:300]}")
                            if len(lines) >= 500:
                                break
            except OSError:
                continue
            if len(lines) >= 500:
                break
    if not lines:
        return "no matches"
    more = f"\n… +{len(lines) - 150} more matches; narrow the search" if len(lines) > 150 else ""
    return "\n".join(lines[:150]) + more


async def reap(proc, grace=3):
    """Stops a process (and its children), waits for it and closes its stdin, so nothing lingers."""
    if proc.stdin and not proc.stdin.is_closing():
        proc.stdin.close()
    if proc.returncode is None:
        kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            pass


def kill_tree(proc):
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


@tool("bash", "Run a shell command in the project folder (git, tests, builds, installs). Non-interactive: "
      "no stdin. Default timeout 120s, max 600.",
      {"command": {"type": "string"}, "timeout": {"type": "integer", "description": "seconds"}}, ["command"])
async def tool_bash(args):
    command = args["command"]
    timeout = min(max(int(args.get("timeout") or 120), 1), 600)
    if not await allowed("bash", command):
        return DENIED
    env = {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1"}
    extra = {"start_new_session": True} if os.name != "nt" else {}
    proc = await asyncio.create_subprocess_shell(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                 stderr=subprocess.STDOUT, env=env, **extra)
    chunks, shown, pending = [], 0, ""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def display(line):
        nonlocal shown
        if shown < 80:
            out(f"     {fg('border')}│{RESET} {fg('muted')}{line.rstrip()[:200]}{RESET}")
        elif shown == 80:
            out(f"     {fg('border')}│{RESET} {fg('faint')}… more output, hidden here{RESET}")
        shown += 1

    async def pump():
        nonlocal pending
        while True:
            data = await proc.stdout.read(65536)
            if not data:
                break
            text = decoder.decode(data)
            chunks.append(text)
            pending += text
            *lines, pending = pending.split("\n")
            for line in lines:
                display(line)
        if pending:
            display(pending)
        await proc.wait()

    status = None
    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass
        status = (f"[timed out after {timeout}s and was stopped. If it's meant to keep running, start it with "
                  "the `shell` tool instead]")
    except asyncio.CancelledError:
        kill_tree(proc)
        raise
    output = cap("".join(chunks).strip()) or "(no output)"
    if status is None:
        status = f"[exit code {proc.returncode}]"
    return f"{output}\n{status}"


# --- Background shells: commands that keep running (dev servers, watchers, long builds) ---
# Backends: tmux (you can watch: tmux attach -t nanocode-<name>), then screen, then nanocode itself
# (works everywhere, incl. Windows). Shells stop when nanocode exits.

SHELL_KEYS = {"enter": ("Enter", "\r"), "ctrl-c": ("C-c", "\x03"), "ctrl-d": ("C-d", "\x04"),
              "escape": ("Escape", "\x1b"), "none": (None, "")}
CONTROL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>78]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def shell_backend():
    choice = st.config.get("shell_backend", "auto")
    if choice in ("tmux", "screen", "builtin"):
        return choice if choice == "builtin" or shutil.which(choice) else "builtin"
    if os.name != "nt":
        for name in ("tmux", "screen"):
            if shutil.which(name):
                return name
    return "builtin"


async def run_cmd(*cmd, timeout=15):
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        kill_tree(proc)
        raise ToolError(f"{cmd[0]} didn't respond within {timeout}s")
    return proc.returncode, stdout.decode(errors="replace")


def clean_terminal_text(text):
    lines = []
    for line in CONTROL_RE.sub("", text.replace("\r\n", "\n")).split("\n"):
        lines.append(line.rsplit("\r", 1)[-1].rstrip())
    return "\n".join(lines).strip("\n")


def tail_file(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            data = f.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(clean_terminal_text(data).split("\n")[-n:])


async def shell_start(name, command):
    backend, cwd = shell_backend(), os.getcwd()
    ensure_project_dir()
    log = project_dir() / "shells" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    info = {"name": name, "command": command, "backend": backend, "log": str(log), "started": time.time()}
    session = f"nanocode-{name}"
    if backend == "tmux":
        await run_cmd("tmux", "kill-session", "-t", session)
        rc, output = await run_cmd("tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", cwd)
        if rc != 0:
            raise ToolError(f"tmux couldn't start: {output.strip()}")
        await run_cmd("tmux", "send-keys", "-t", session, "-l", command)
        await run_cmd("tmux", "send-keys", "-t", session, "Enter")
        info["attach"] = f"tmux attach -t {session}"
    elif backend == "screen":
        # Logging set in a config file at startup and plain `sh` inside: works the same on screen 4.0 (macOS) to 5.x.
        await run_cmd("screen", "-S", session, "-X", "quit")
        rc_file = log.with_suffix(".screenrc")
        rc_file.write_text(f"logfile {log}\nlogfile flush 1\ndeflog on\n")
        rc, output = await run_cmd("screen", "-c", str(rc_file), "-dmS", session, "sh")
        if rc != 0:
            raise ToolError(f"screen couldn't start: {output.strip()}")
        await asyncio.sleep(0.3)
        await run_cmd("screen", "-S", session, "-p", "0", "-X", "stuff", f"cd {shlex.quote(cwd)} && {command}\r")
        info["attach"] = f"screen -r {session}"
    else:
        extra = {"start_new_session": True} if os.name != "nt" else {}
        handle = open(log, "ab")
        info["proc"] = await asyncio.create_subprocess_shell(
            command, stdin=subprocess.PIPE, stdout=handle, stderr=subprocess.STDOUT, cwd=cwd,
            env={**os.environ, "PAGER": "cat", "GIT_PAGER": "cat"}, **extra)
        info["handle"] = handle
    st.shells[name] = info
    return info


async def shell_alive(info):
    session = f"nanocode-{info['name']}"
    if info["backend"] == "tmux":
        rc, output = await run_cmd("tmux", "list-panes", "-t", session, "-F", "#{pane_current_command}")
        shell_names = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh"}
        return rc == 0 and output.strip().split("\n")[0] not in shell_names
    if info["backend"] == "screen":
        rc, output = await run_cmd("screen", "-ls")
        return session in output
    return info["proc"].returncode is None


async def shell_output(info, lines):
    if info["backend"] == "tmux":
        rc, output = await run_cmd("tmux", "capture-pane", "-p", "-J", "-t", f"nanocode-{info['name']}",
                                   "-S", f"-{max(lines, 50)}")
        if rc == 0:
            return "\n".join(clean_terminal_text(output).split("\n")[-lines:])
        return ""
    return tail_file(info["log"], lines)


async def shell_stop(info):
    session = f"nanocode-{info['name']}"
    try:
        if info["backend"] == "tmux":
            await run_cmd("tmux", "kill-session", "-t", session)
        elif info["backend"] == "screen":
            await run_cmd("screen", "-S", session, "-X", "quit")
        else:
            await reap(info["proc"])
    except ToolError:
        pass
    if info.get("handle"):
        info["handle"].close()
    st.shells.pop(info["name"], None)


async def stop_all_shells():
    for info in list(st.shells.values()):
        await shell_stop(info)


@tool("shell", "Background shells for commands that keep running: dev servers, watchers, long builds, REPLs. "
      "action=start (name, command), read (name, lines), send (name, text and/or key), stop (name), list. "
      "Use bash for commands that finish on their own.",
      {"action": {"type": "string", "enum": ["start", "read", "send", "stop", "list"]},
       "name": {"type": "string", "description": "short name, e.g. dev or tests"},
       "command": {"type": "string", "description": "for start"},
       "text": {"type": "string", "description": "for send: text to type"},
       "key": {"type": "string", "enum": list(SHELL_KEYS), "description": "for send: key after the text (default enter)"},
       "lines": {"type": "integer", "description": "for read: how many recent lines (default 60)"}},
      ["action"])
async def tool_shell(args):
    action = args["action"]
    if action == "list":
        if not st.shells:
            return "no background shells running"
        rows = []
        for info in st.shells.values():
            state = "running" if await shell_alive(info) else "finished"
            rows.append(f"{info['name']}: {state} · {info['command']} · started {fmt_duration(time.time() - info['started'])} ago")
        return "\n".join(rows)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(args.get("name") or "")).strip("-")[:40]
    if not name:
        raise ToolError("`name` is required (e.g. dev)")
    if action == "start":
        command = args.get("command")
        if not command:
            raise ToolError("`command` is required to start a shell")
        if name in st.shells:
            if await shell_alive(st.shells[name]):
                raise ToolError(f"shell '{name}' is already running; read it, stop it, or pick another name")
            await shell_stop(st.shells[name])
        if not await allowed("shell", command):
            return DENIED
        info = await shell_start(name, command)
        await asyncio.sleep(2)
        state = "running" if await shell_alive(info) else "already finished"
        output = await shell_output(info, 20)
        attach = f"\n(the user can watch it: {info['attach']})" if info.get("attach") else ""
        return f"started '{name}' ({info['backend']}), {state}. First output:\n{output or '(none yet)'}{attach}"
    info = st.shells.get(name)
    if info is None:
        known = ", ".join(st.shells) or "none"
        raise ToolError(f"no shell named '{name}' (running: {known})")
    if action == "read":
        lines = min(max(int(args.get("lines") or 60), 1), 500)
        state = "running" if await shell_alive(info) else "finished"
        return f"[{name}: {state}]\n" + (await shell_output(info, lines) or "(no output yet)")
    if action == "send":
        text, key = args.get("text") or "", (args.get("key") or "enter").lower()
        if key not in SHELL_KEYS:
            raise ToolError(f"unknown key '{key}' (use: {', '.join(SHELL_KEYS)})")
        if text and not await allowed("shell", f"{name} ← {text}"):
            return DENIED
        tmux_key, raw = SHELL_KEYS[key]
        session = f"nanocode-{name}"
        if info["backend"] == "tmux":
            if text:
                await run_cmd("tmux", "send-keys", "-t", session, "-l", text)
            if tmux_key:
                await run_cmd("tmux", "send-keys", "-t", session, tmux_key)
        elif info["backend"] == "screen":
            await run_cmd("screen", "-S", session, "-p", "0", "-X", "stuff", text + raw)
        else:
            proc = info["proc"]
            if key == "ctrl-c" and proc.returncode is None:
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGINT)
                    else:
                        proc.terminate()
                except (ProcessLookupError, OSError):
                    pass
            elif proc.returncode is None and proc.stdin:
                proc.stdin.write((text + ("\n" if key == "enter" else raw)).encode())
                await proc.stdin.drain()
        await asyncio.sleep(1)
        return f"sent. Recent output:\n{await shell_output(info, 15) or '(none)'}"
    if action == "stop":
        await shell_stop(info)
        return f"stopped '{name}'"
    raise ToolError(f"unknown action '{action}'")


@tool("remember", "Save a lasting fact, preference or lesson to memory so it's known in future chats. "
      "scope: project (default) or global (all projects).",
      {"fact": {"type": "string"}, "scope": {"type": "string", "enum": ["project", "global"]}}, ["fact"])
async def tool_remember(args):
    return f"saved to {tilde(add_memory(args['fact'], args.get('scope') or 'project'))}"


@tool("skill", "Load a skill's full instructions by name. Do this before a task that matches a skill's description.",
      {"name": {"type": "string"}}, ["name"])
async def tool_skill(args):
    text = load_skill(args["name"])
    if text is None:
        raise ToolError(f"no skill named '{args['name']}'. Available: {', '.join(list_skills()) or 'none'}")
    return text


@tool("save_skill", "Save a reusable procedure as a skill, so future chats can follow it. Use after completing a "
      "non-trivial task that is likely to recur. Overwrites a skill with the same name (to improve it).",
      {"name": {"type": "string", "description": "short-kebab-case"},
       "description": {"type": "string", "description": "one line: when to use this skill"},
       "instructions": {"type": "string", "description": "markdown steps, commands, gotchas"},
       "scope": {"type": "string", "enum": ["project", "global"]}}, ["name", "description", "instructions"])
async def tool_save_skill(args):
    name = re.sub(r"[^a-z0-9-]+", "-", args["name"].lower()).strip("-")[:60]
    if not name:
        raise ToolError("name must contain letters or digits")
    root = home_dir() / "skills" if args.get("scope") == "global" else project_dir() / "skills"
    if root == project_dir() / "skills":
        ensure_project_dir()
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = read_text(path) if path.exists() else None
    record_undo(path, previous)
    description = " ".join(args["description"].split())
    write_text(path, f"---\nname: {name}\ndescription: {description}\n---\n\n{args['instructions'].strip()}\n")
    return f"{'updated' if previous else 'saved'} skill '{name}' at {tilde(path)}"


# --- Memory ---

def memory_files():
    """(label, path, text) for each memory file that exists, deduplicated."""
    candidates = [("global", global_memory_file()), ("project", project_memory_file())]
    candidates += [("shared, read-only", Path.cwd() / n) for n in ("NANOCODE.md", "CLAUDE.md", "AGENTS.md")]
    found, seen = [], set()
    for label, path in candidates:
        try:
            real = path.resolve()
            if real in seen or not path.is_file():
                continue
            seen.add(real)
            text = read_text(path).strip()
        except OSError:
            continue
        if text:
            found.append((label, path, text))
    return found


def add_memory(fact, scope="project"):
    if scope == "global":
        path = global_memory_file()
    else:
        ensure_project_dir()
        path = project_memory_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fact = " ".join(fact.split())
    if path.exists() and f"- {fact}" in (line.strip() for line in read_text(path).splitlines()):
        return path
    with open(path, "a", encoding="utf-8", newline="") as f:
        if path.stat().st_size == 0:
            f.write("# Memory\n\n")
        f.write(f"- {fact}\n")
    return path


def user_name():
    for _, _, text in memory_files():
        match = re.search(r"(?im)^\s*[-*]?\s*(?:my\s+)?name\s*(?::|is)\s*([^\n.,;<>]+)", text)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


# --- Skills ---

def parse_skill(path):
    text = read_text(path)
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip().strip("\"'")
            body = parts[2]
    return meta, body.strip()


def list_skills():
    skills = {}
    for root in skill_dirs():
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                meta, _ = parse_skill(path)
            except OSError:
                continue
            name = meta.get("name") or path.parent.name
            if name not in skills:
                skills[name] = {"description": meta.get("description", ""), "path": path}
    return skills


def load_skill(name):
    info = list_skills().get(name)
    if not info:
        return None
    _, body = parse_skill(info["path"])
    folder = info["path"].parent
    if name == REA_SKILL:
        body += ("\n\n## In nanocode\nREA's tools are reached through three nanocode tools: `rea_search` finds "
                 "tools by keywords, `rea_schema` shows a tool's arguments, and `rea_call` runs it, e.g. "
                 'rea_call(tool="inspect_artifact", arguments={...}). Wherever these instructions name a REA tool, '
                 "call it that way.")
    return f'<skill name="{name}">\n{body}\n\n(Files for this skill are in {folder}.)\n</skill>'


# --- REA (https://github.com/morluto/rea): optional reverse-engineering tools over MCP ---
# REA has ~230 tools whose schemas alone are ~80k tokens, far too many to give a model directly. The
# model searches them (rea_search), reads one schema (rea_schema) and runs it (rea_call). The server
# starts on first use, and any REA failure becomes an error message for the model: nanocode carries on.

REA_SKILL = "reverse-engineer-anything"
REA_OUTPUT_LIMIT = 12_000
REA_RISKY = re.compile(r"install|uninstall|push|launch|execute|run_|_run|start_|stop_|kill|load_|resume|capture|"
                       r"observe|record|attach|inject|spawn|frida|set_|unset_|annotate|write|delete|remove", re.I)


def rea_dir():
    return home_dir() / "tools" / "rea"


def rea_runtime():
    """{"node", "entry", "version"} recorded by the installer, or None if REA isn't usable."""
    try:
        info = json.loads((rea_dir() / "runtime.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not Path(info.get("entry", "")).is_file():
        return None
    if not Path(info.get("node", "")).exists():
        info["node"] = shutil.which("node") or ""
    return info if info["node"] else None


def rea_enabled():
    return st.config.get("rea", True) is not False and rea_runtime() is not None


REA_SAFE_PREFIXES = ("open_", "close_", "list_", "get_", "inspect_", "read_", "search_", "find_", "describe_",
                     "analyze_", "compare_", "decompile_", "demangle_", "current_", "procedure_", "xrefs")


def rea_is_risky(spec):
    annotations = spec.get("annotations") or {}
    if annotations.get("readOnlyHint") or spec["name"].startswith(REA_SAFE_PREFIXES):
        return False
    return bool(annotations.get("destructiveHint")) or bool(REA_RISKY.search(spec["name"]))


class ReaClient:
    """Minimal MCP client (JSON-RPC over stdio) for the REA server."""

    def __init__(self):
        self.proc = None
        self.reader = None
        self.pending = {}
        self.next_id = 0
        self.tools = None
        self.lock = asyncio.Lock()
        self.log = None

    def alive(self):
        # The reader ends when REA's output closes; on Windows that happens before returncode is set.
        return (self.proc is not None and self.proc.returncode is None
                and self.reader is not None and not self.reader.done())

    async def ensure(self):
        async with self.lock:
            if self.alive():
                return
            await self.close()
            info = rea_runtime()
            if info is None:
                raise ToolError("REA isn't installed. Install or update it with: nanocode update (needs Node.js 22+)")
            log_file().parent.mkdir(parents=True, exist_ok=True)
            self.log = open(log_file().parent / "rea.log", "ab")
            env = {**os.environ, "REA_MCP_INPUT_SCHEMA_PROFILE": "compact", "REA_MCP_OUTPUT_SCHEMA_PROFILE": "off",
                   "PATH": os.path.dirname(info["node"]) + os.pathsep + os.environ.get("PATH", "")}
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    info["node"], info["entry"], "mcp", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=self.log, env=env, limit=256 * 1024 * 1024)
            except OSError as e:
                raise ToolError(f"couldn't start REA: {e}") from e
            self.reader = asyncio.ensure_future(self._read())
            await asyncio.sleep(0)
            await self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "nanocode", "version": __version__}}, 120)
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _send(self, message):
        if not self.alive():
            raise ToolError("REA stopped unexpectedly (details: ~/.nanocode/logs/rea.log)")
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            await self.proc.stdin.drain()
        except (OSError, RuntimeError) as e:
            raise ToolError(f"REA stopped unexpectedly ({type(e).__name__}); it restarts on the next call") from e

    async def _read(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    future = self.pending.pop(msg["id"], None)
                    if future and not future.done():
                        future.set_result(msg)
                elif "id" in msg and "method" in msg:
                    await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                      "error": {"code": -32601, "message": "not supported by nanocode"}})
        except (asyncio.CancelledError, ToolError):
            pass
        except Exception as e:
            log_error(e)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ToolError("REA stopped unexpectedly (details: ~/.nanocode/logs/rea.log)"))
            self.pending.clear()

    async def request(self, method, params=None, timeout=600):
        self.next_id += 1
        rid = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        try:
            msg = await asyncio.wait_for(asyncio.shield(future), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            self.pending.pop(rid, None)
            try:
                await self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                  "params": {"requestId": rid, "reason": "cancelled by nanocode"}})
            except (ToolError, OSError):
                pass
            if isinstance(e, asyncio.CancelledError):
                raise
            raise ToolError(f"REA didn't finish within {timeout}s; try a narrower request or a bigger timeout") from e
        if "error" in msg:
            raise ToolError(f"REA: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    async def list_tools(self):
        if self.tools is None:
            await self.ensure()
            tools, cursor = [], None
            while True:
                result = await self.request("tools/list", {"cursor": cursor} if cursor else {}, 120)
                tools += result.get("tools", [])
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            self.tools = {t["name"]: t for t in tools}
        return self.tools

    async def call(self, name, arguments, timeout):
        await self.ensure()
        return await self.request("tools/call", {"name": name, "arguments": arguments}, timeout)

    async def close(self):
        if self.reader:
            self.reader.cancel()
            self.reader = None
        if self.proc is not None:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
            if self.proc.returncode is None:
                try:
                    self.proc.terminate()
                    await asyncio.wait_for(self.proc.wait(), 3)
                except (asyncio.TimeoutError, ProcessLookupError):
                    await reap(self.proc)
        self.proc = None
        if self.log:
            self.log.close()
            self.log = None


def rea():
    if st.rea is None:
        st.rea = ReaClient()
    return st.rea


def first_sentence(text):
    text = " ".join((text or "").split())
    end = text.find(". ")
    return (text if end == -1 else text[: end + 1])[:160]


@tool("rea_search", "Search REA's reverse-engineering tools by keywords (binaries, apps, APKs, Electron/JS bundles, "
      "websites, firmware, Android devices...). Returns tool names for rea_schema and rea_call.",
      {"query": {"type": "string"}, "limit": {"type": "integer", "description": "max results (default 12)"}},
      ["query"])
async def tool_rea_search(args):
    tools = await rea().list_tools()
    words = [w for w in re.split(r"[\s_\-/,.]+", args["query"].lower()) if len(w) > 1]
    scored = []
    for name, spec in tools.items():
        title = name.replace("_", " ")
        text = (title + " " + (spec.get("description") or "")).lower()
        score = sum(3 if w in title else 1 for w in words if w in text)
        if score:
            scored.append((-score, name))
    if not scored:
        return f"no REA tools match '{args['query']}'. Try other words (REA has {len(tools)} tools)."
    limit = min(max(int(args.get("limit") or 12), 1), 40)
    lines = [f"{name}: {first_sentence(tools[name].get('description'))}" +
             (" [changes state, asks permission]" if rea_is_risky(tools[name]) else "")
             for _, name in sorted(scored)[:limit]]
    return "\n".join(lines) + "\n(next: rea_schema to see a tool's arguments, then rea_call)"


@tool("rea_schema", "Show a REA tool's full description and its arguments (JSON schema).",
      {"tool": {"type": "string"}}, ["tool"])
async def tool_rea_schema(args):
    spec = await rea_tool_spec(args["tool"])
    return f"{spec['name']}\n\n{spec.get('description', '')}\n\narguments schema:\n{json.dumps(spec.get('inputSchema', {}))}"


async def rea_tool_spec(name):
    tools = await rea().list_tools()
    if name not in tools:
        close = difflib.get_close_matches(name, list(tools), n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else " Use rea_search to find tools."
        raise ToolError(f"REA has no tool '{name}'.{hint}")
    return tools[name]


@tool("rea_call", "Run a REA tool. `arguments` must match the schema from rea_schema.",
      {"tool": {"type": "string"}, "arguments": {"type": "object"},
       "timeout": {"type": "integer", "description": "seconds (default 600)"}}, ["tool"])
async def tool_rea_call(args):
    spec = await rea_tool_spec(args["tool"])
    arguments = args.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise ToolError("`arguments` must be a JSON object")
    if rea_is_risky(spec) and not await allowed("rea", f"{spec['name']} {json.dumps(arguments)}"):
        return DENIED
    timeout = min(max(int(args.get("timeout") or 600), 5), 3600)
    result = await rea().call(spec["name"], arguments, timeout)
    parts = []
    for item in result.get("content") or []:
        parts.append(item.get("text", "") if item.get("type") == "text" else f"[{item.get('type')} content]")
    text = "\n".join(p for p in parts if p) or (json.dumps(result["structuredContent"])
                                              if result.get("structuredContent") else "")
    text = cap(text, REA_OUTPUT_LIMIT)
    if result.get("isError"):
        if "schema" in text.lower():
            text += "\n\narguments schema:\n" + json.dumps(spec.get("inputSchema", {}))[:8000]
        return "error: " + (text or "REA reported an error")
    return text or "(no output)"


def active_tools():
    """Tools offered to the model: REA's only when it's installed and enabled."""
    return [t for t in TOOLS.values() if not t["name"].startswith("rea_") or rea_enabled()]


def build_system_prompt():
    parts = [
        "You are nanocode, a coding agent working in the user's terminal.\n"
        f"cwd: {os.getcwd()}\nOS: {platform.system()} {platform.release()}\ndate: {time.strftime('%Y-%m-%d')}\n\n"
        "Be concise. Read files before editing them and prefer `edit` over rewriting whole files. Use `bash` for "
        "git, tests and builds; use `shell` for anything that keeps running (dev servers, watchers). Do only what was asked; after changing code, run the relevant tests if there are "
        "any. If a tool returns an error, read it carefully and fix the cause instead of repeating the same call."
    ]
    memories = memory_files()
    if memories:
        parts.append("# Memory\nFacts, preferences and lessons saved earlier. Follow them.")
        for label, path, text in memories:
            parts.append(f'<memory source="{tilde(path)}" scope="{label}">\n{text}\n</memory>')
    skills = list_skills()
    if skills:
        lines = ["# Skills\nLoad a skill with the `skill` tool before a task that matches its description:"]
        lines += [f"- {name}: {info['description'][:200]}" for name, info in skills.items()]
        parts.append("\n".join(lines))
    if rea_enabled():
        parts.append(
            "# Reverse engineering\nFor reverse-engineering work (native binaries, apps, APKs, Electron/JS bundles, "
            f"websites, firmware, devices), load the `{REA_SKILL}` skill first, then use REA: rea_search to "
            "find a tool, rea_schema for its arguments, rea_call to run it. Some REA features need programs the user "
            "installs themselves (Ghidra, Hopper or IDA for native code; JADX and a JDK for APKs; adb; Apktool; "
            "Chrome; Binwalk; TShark). If REA reports one is missing, tell the user what to install and how, then "
            "continue with what works without it; don't retry the same call.")
    parts.append(
        "# Learning\nWhen the user corrects you or states a lasting preference, save it with `remember`. After "
        "finishing a non-trivial multi-step task that's likely to recur, save the procedure with `save_skill` "
        "(or improve the existing skill). Keep saved items short and specific; never save secrets."
    )
    return "\n\n".join(parts)


# --- Claude subscription (Claude Agent SDK) ---

def sdk_tools():
    def make_handler(name):
        async def handler(args):
            text, is_error = await run_tool(name, args)
            return {"content": [{"type": "text", "text": text}], "is_error": is_error}
        return handler

    return [sdk.SdkMcpTool(name=t["name"], description=t["description"], input_schema=t["schema"],
                           handler=make_handler(t["name"])) for t in active_tools()]


async def make_client(resume=None):
    if sdk is None:
        raise RuntimeError("claude-agent-sdk isn't installed, so the Claude provider can't run. "
                           "Reinstall nanocode, or switch provider: nanocode setup")
    server = sdk.create_sdk_mcp_server(name="nano", version=__version__, tools=sdk_tools())
    options = sdk.ClaudeAgentOptions(
        mcp_servers={"nano": server},
        model=st.model,
        allowed_tools=[f"mcp__nano__{t['name']}" for t in active_tools()],
        disallowed_tools=["Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch",
                          "Task", "TodoWrite", "NotebookEdit"],
        system_prompt=build_system_prompt(),
        include_partial_messages=True,
        resume=resume,
        cwd=os.getcwd(),
        setting_sources=[],
        strict_mcp_config=True,
    )
    if "tools" in {f.name for f in dataclasses.fields(sdk.ClaudeAgentOptions)}:
        options.tools = []  # no Claude Code built-ins: only nanocode's tools, which handle permissions themselves

    async def deny_others(tool_name, _input, _context):
        # Anything that still asks for permission isn't ours; answering keeps Claude Code from waiting forever.
        return sdk.PermissionResultDeny(message=f"{tool_name} isn't available in nanocode; use nanocode's tools")

    options.can_use_tool = deny_others
    client = sdk.ClaudeSDKClient(options=options)
    try:
        await client.connect()
    except Exception as e:
        if type(e).__name__ == "CLINotFoundError":
            raise RuntimeError("Claude Code CLI not found. Install it (curl -fsSL https://claude.ai/install.sh | bash), "
                               "log in with `claude` → /login, or switch provider: nanocode setup") from e
        raise RuntimeError(f"couldn't start Claude Code: {e}") from e
    return client


async def sdk_query(text):
    started = time.time()
    await st.client.query(text)
    stream_event = getattr(sdk, "StreamEvent", None) or getattr(sdk.types, "StreamEvent", None)
    async for msg in st.client.receive_response():
        if stream_event and isinstance(msg, stream_event):
            ev = msg.event
            kind = ev.get("type")
            if kind == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                stream_text(ev["delta"]["text"])
            elif kind == "content_block_stop":
                stream_end()
        elif isinstance(msg, sdk.AssistantMessage):
            for block in msg.content:
                if isinstance(block, sdk.ToolUseBlock) and not block.name.startswith("mcp__nano__"):
                    show_tool_call(block.name, block.input or {})
        elif isinstance(msg, sdk.ResultMessage):
            stream_end()
            if msg.session_id:
                st.session["sdk_session_id"] = msg.session_id
            usage = msg.usage or {}
            tokens_in = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                        "cache_creation_input_tokens"))
            if msg.is_error:
                detail = msg.result or ", ".join(msg.errors or []) or msg.subtype
                if "login" in str(detail).lower() or "api key" in str(detail).lower():
                    detail = f"{detail}. Log in with `claude` → /login, or switch provider: nanocode setup"
                raise RuntimeError(detail)
            show_usage(tokens_in, usage.get("output_tokens") or 0, msg.total_cost_usd, time.time() - started)


# --- OpenRouter and Ollama (we run the agent loop ourselves) ---

class RetryableError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def api_tools():
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["schema"]}} for t in active_tools()]


def estimate_tokens(obj):
    return len(json.dumps(obj, ensure_ascii=False)) // 4


def context_budget(system_prompt):
    total = st.num_ctx if st.backend == "ollama" else int(st.config.get("context_tokens", 150_000))
    reserve = 2048 if st.backend == "ollama" else 16_000
    return max(1024, total - reserve - estimate_tokens(system_prompt) - estimate_tokens(api_tools()))


def repair_history(messages):
    """Drops tool calls that never got results (and orphan results), so the history is valid to send again."""
    result, i = [], 0
    while i < len(messages):
        msg = messages[i]
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            j, results = i + 1, []
            while j < len(messages) and messages[j]["role"] == "tool":
                results.append(messages[j])
                j += 1
            if {c["id"] for c in msg["tool_calls"]} <= {r["tool_call_id"] for r in results}:
                result.append(msg)
                result.extend(results)
            i = j
        elif msg["role"] == "tool":
            i += 1
        else:
            result.append(msg)
            i += 1
    return result


def fit_history(messages, budget):
    """Shrinks old tool output, then drops the oldest turns, until the history fits. Keeps tool calls paired."""
    msgs = [dict(m) for m in messages]
    if estimate_tokens(msgs) <= budget:
        return msgs, False
    user_idx = [i for i, m in enumerate(msgs) if m["role"] == "user"]
    protect_from = user_idx[-2] if len(user_idx) >= 2 else 0
    for m in msgs[:protect_from]:
        if m["role"] == "tool" and len(m.get("content") or "") > 400:
            m["content"] = m["content"][:300] + "\n… [old output trimmed to save context]"
    dropped = False
    while estimate_tokens(msgs) > budget:
        user_idx = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        if len(user_idx) < 2:
            break
        msgs = msgs[user_idx[1]:]
        dropped = True
    if dropped and msgs and msgs[0]["role"] == "user" and isinstance(msgs[0].get("content"), str):
        msgs[0]["content"] = "[Earlier messages were trimmed to fit the context window.]\n\n" + msgs[0]["content"]
    return msgs, True


def api_error(status, body):
    try:
        err = json.loads(body).get("error", body)
        message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except (json.JSONDecodeError, AttributeError):
        message = body
    message = str(message)[:300]
    if status == 401:
        return f"OpenRouter rejected the API key ({message}). Fix it with: nanocode setup"
    if status == 402:
        return f"OpenRouter: out of credits ({message}). Add credits at https://openrouter.ai/settings/credits"
    if status == 404 and "model" in message.lower():
        return f"OpenRouter: {message}. Pick another model with /model"
    return f"OpenRouter {status}: {message}"


async def openrouter_stream(system_prompt, messages, use_tools=True):
    text, tool_calls, usage = "", {}, {}
    payload = {"model": st.model, "max_tokens": 16000, "stream": True, "usage": {"include": True},
               "messages": [{"role": "system", "content": system_prompt}] + messages}
    if use_tools:
        payload["tools"] = api_tools()
    headers = {"Authorization": f"Bearer {st.openrouter_key}", "X-Title": "nanocode",
               "HTTP-Referer": f"https://github.com/{REPO}"}
    timeout = httpx.Timeout(connect=20, read=300, write=60, pool=20)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            async with http.stream("POST", OPENROUTER_URL, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    message = api_error(resp.status_code, body)
                    if resp.status_code in (408, 429, 500, 502, 503, 504):
                        retry_after = resp.headers.get("retry-after")
                        raise RetryableError(message, float(retry_after) if retry_after and
                                             retry_after.replace(".", "").isdigit() else None)
                    raise RuntimeError(message)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        err = chunk["error"]
                        raise RuntimeError(f"OpenRouter: {err.get('message', err) if isinstance(err, dict) else err}")
                    if chunk.get("model"):
                        st.routed_model = chunk["model"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text += delta["content"]
                            stream_text(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = tool_calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] = fn.get("name") or slot["name"]
                            slot["arguments"] += fn.get("arguments") or ""
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.WriteError) as e:
        if text or tool_calls:
            raise RuntimeError(f"connection lost mid-reply ({type(e).__name__}); try again") from e
        raise RetryableError(f"network error ({type(e).__name__})") from e
    stream_end()
    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = [
            {"id": tc["id"] or f"call_{i}", "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}
            for i, tc in sorted(tool_calls.items())
        ]
    return message, {"input": usage.get("prompt_tokens") or 0, "output": usage.get("completion_tokens") or 0,
                     "cost": usage.get("cost") or 0}


def to_ollama_messages(messages):
    """Ollama wants tool arguments as objects and tool_name on results; our history is OpenAI-style."""
    names, result = {}, []
    for msg in messages:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            calls = []
            for call in msg["tool_calls"]:
                names[call["id"]] = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append({"function": {"name": call["function"]["name"], "arguments": args}})
            result.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        elif msg["role"] == "tool":
            result.append({"role": "tool", "content": msg["content"], "tool_name": names.get(msg["tool_call_id"], "")})
        else:
            result.append({"role": msg["role"], "content": msg.get("content") or ""})
    return result


def ollama_models(url=None, timeout=3):
    resp = httpx.get(f"{url or st.ollama_url}/api/tags", timeout=timeout)
    resp.raise_for_status()
    models = []
    for m in resp.json().get("models", []):
        details = m.get("details") or {}
        models.append({"name": m["name"], "size_gb": (m.get("size") or 0) / 1e9,
                       "params": details.get("parameter_size", "?"),
                       "tools": "tools" in (m.get("capabilities") or ["tools"])})
    return sorted(models, key=lambda m: (not m["tools"], m["name"]))


def refresh_ollama_models():
    try:
        st.ollama_models = ollama_models()
    except Exception:
        st.ollama_models = []
    return st.ollama_models


async def ollama_stream(system_prompt, messages, use_tools=True):
    """Native /api/chat, not the OpenAI-compatible one, so num_ctx can be set (the default would cut prompts)."""
    text, tool_calls, usage = "", [], {}
    payload = {"model": st.model, "stream": True, "options": {"num_ctx": st.num_ctx},
               "messages": [{"role": "system", "content": system_prompt}] + to_ollama_messages(messages)}
    if use_tools:
        payload["tools"] = api_tools()
    timeout = httpx.Timeout(connect=10, read=900, write=60, pool=10)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            async with http.stream("POST", f"{st.ollama_url}/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    try:
                        error = str(json.loads(body).get("error", body))
                    except (json.JSONDecodeError, AttributeError):
                        error = body
                    if "not found" in error:
                        error += f" (download it with: ollama pull {st.model})"
                    elif "does not support tools" in error:
                        error += " (pick a model with tool support: /model)"
                    raise RuntimeError(f"Ollama: {error[:300]}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(f"Ollama: {chunk['error']}")
                    message = chunk.get("message") or {}
                    if message.get("content"):
                        text += message["content"]
                        stream_text(message["content"])
                    for call in message.get("tool_calls") or []:
                        fn = call.get("function") or {}
                        tool_calls.append({"id": call.get("id") or f"call_{len(tool_calls) + 1}", "type": "function",
                                           "function": {"name": fn.get("name", ""),
                                                        "arguments": json.dumps(fn.get("arguments") or {})}})
                    if chunk.get("done"):
                        usage = {"input": chunk.get("prompt_eval_count", 0), "output": chunk.get("eval_count", 0)}
    except httpx.ConnectError as e:
        raise RuntimeError(f"can't reach Ollama at {st.ollama_url}. Start it (open the Ollama app, or run: "
                           "ollama serve)") from e
    except httpx.ReadTimeout as e:
        raise RuntimeError("Ollama stopped responding (15 min without output). Try a smaller model or context.") from e
    stream_end()
    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message, usage


async def model_call(system_prompt, messages, use_tools=True):
    if st.backend == "ollama":
        return await ollama_stream(system_prompt, messages, use_tools)
    for attempt in range(4):
        try:
            return await openrouter_stream(system_prompt, messages, use_tools)
        except RetryableError as e:
            if attempt == 3:
                raise RuntimeError(f"{e} (gave up after 4 tries)") from e
            wait = min(e.retry_after or 2 ** (attempt + 1), 30)
            out(f"  {DIM}⎿  {e}; retrying in {wait:.0f}s{RESET}")
            await asyncio.sleep(wait)


async def api_query(text):
    """One user turn: call the model, run its tools, repeat until it answers without tools."""
    started = time.time()
    totals = {"input": 0, "output": 0, "cost": 0.0}
    st.messages.append({"role": "user", "content": text})
    try:
        for _ in range(100):
            system_prompt = build_system_prompt()
            st.ctx_budget = context_budget(system_prompt)
            history, trimmed = fit_history(st.messages, st.ctx_budget)
            st.ctx_tokens = estimate_tokens(history)
            if trimmed and not st.trim_notice:
                out(f"  {DIM}⎿  chat is long: older messages are trimmed to fit (use /compact to summarize){RESET}")
                st.trim_notice = True
            message, usage = await model_call(system_prompt, history)
            st.ctx_tokens += estimate_tokens(message)
            for k in totals:
                totals[k] += usage.get(k) or 0
            st.messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                break
            for call in calls:
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                    result, _ = await run_tool(call["function"]["name"], args)
                except json.JSONDecodeError:
                    st.turn_errors += 1
                    result = "error: tool arguments were not valid JSON; send the call again with valid JSON"
                st.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        else:
            out(f"  {YELLOW}⎿  stopped after 100 steps in one turn{RESET}")
    except BaseException:
        st.messages[:] = repair_history(st.messages)
        raise
    show_usage(totals["input"], totals["output"], totals["cost"], time.time() - started)
    if st.routed_model and (st.model or "").startswith("openrouter/"):
        out(f"  {DIM}routed to {st.routed_model}{RESET}")


async def compact():
    if st.backend == "sdk":
        out(f"{DIM}⏺ compacting…{RESET}")
        await sdk_query("/compact")
        out(f"{GREEN}⏺ Compacted{RESET}\n")
        return
    if not st.messages:
        out(f"{DIM}nothing to compact yet{RESET}\n")
        return
    before = estimate_tokens(st.messages)
    out(f"{DIM}⏺ summarizing the conversation…{RESET}")
    history, _ = fit_history(st.messages + [{"role": "user", "content": COMPACT_PROMPT}],
                             context_budget(build_system_prompt()))
    message, _ = await model_call(build_system_prompt(), history, use_tools=False)
    summary = (message.get("content") or "").strip()
    if not summary:
        raise RuntimeError("the model returned an empty summary; nothing was changed")
    st.messages[:] = [{"role": "user", "content": f"Summary of our conversation so far:\n\n{summary}"},
                      {"role": "assistant", "content": "Got it. I have the context; what's next?"}]
    st.trim_notice = False
    st.ctx_tokens = estimate_tokens(st.messages)
    out(f"{GREEN}⏺ Compacted{RESET} {DIM}{fmt_tokens(before)} → {fmt_tokens(estimate_tokens(st.messages))} tokens{RESET}\n")


async def recap():
    """Facts nanocode tracked itself (instant, free), then a short recap written by the model."""
    if not st.session.get("title"):
        out(f"{DIM}nothing to recap yet: this chat is empty{RESET}\n")
        return
    lines = [f"{BOLD}Recap{RESET} {DIM}· {st.session['title']} · {fmt_duration(time.time() - st.session['created'])} "
             f"· {fmt_tokens(st.usage['input'])} in · {fmt_tokens(st.usage['output'])} out · "
             f"{fmt_cost(st.usage['cost'])}{RESET}"]
    if st.changed:
        lines.append(f"  {BOLD}files changed{RESET}")
        lines += [f"    {path}  {GREEN}+{a}{RESET} {RED}-{r}{RESET}" for path, (a, r) in st.changed.items()]
    commands = list(dict.fromkeys(p for name, p, _ in st.activity if name in ("bash", "shell") and p))
    if commands:
        lines.append(f"  {BOLD}commands{RESET}")
        lines += [f"    {DIM}${RESET} {c[:100]}" for c in commands[-8:]]
    if st.activity:
        errors = sum(1 for *_, err in st.activity if err)
        lines.append(f"  {DIM}{len(st.activity)} tool calls{f', {errors} failed' if errors else ''}{RESET}")
    out("\n".join(lines))
    started = time.time()
    if st.backend == "sdk":
        await sdk_query(RECAP_PROMPT)
        return
    history, _ = fit_history(st.messages + [{"role": "user", "content": RECAP_PROMPT}],
                             context_budget(build_system_prompt()))
    _, usage = await model_call(build_system_prompt(), history, use_tools=False)
    show_usage(usage.get("input") or 0, usage.get("output") or 0, usage.get("cost"), time.time() - started)


# --- Sessions: .nanocode/sessions/<id>.json ---

def new_session():
    st.session = {"id": time.strftime("%Y%m%d-%H%M%S"), "title": None, "created": time.time(), "sdk_session_id": None}
    st.activity, st.changed = [], {}


def save_session():
    if not st.session.get("title"):
        return
    if not st.session.get("id"):
        st.session.update(id=time.strftime("%Y%m%d-%H%M%S"), created=time.time())
    try:
        ensure_project_dir()
        sessions_dir().mkdir(parents=True, exist_ok=True)
        data = {**st.session, "backend": st.backend, "model": st.model, "updated": time.time(),
                "version": __version__, "messages": st.messages if st.backend != "sdk" else [],
                "usage": st.usage, "activity": st.activity[-500:], "changed": st.changed,
                "screen": st.output[-200_000:]}
        path = sessions_dir() / f"{st.session['id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log_error(e)


def list_sessions(limit=20):
    sessions = []
    if sessions_dir().is_dir():
        for path in sessions_dir().glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("backend") == st.backend and data.get("title"):
                sessions.append(data)
    return sorted(sessions, key=lambda s: s.get("updated", 0), reverse=True)[:limit]


def time_ago(ts):
    secs = time.time() - ts
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


async def resume_session(arg):
    sessions = list_sessions()
    if not arg:
        if not sessions:
            out(f"{DIM}no saved chats in this folder yet{RESET}\n")
            return
        lines = [f"{BOLD}Saved chats{RESET} {DIM}(/resume <n>){RESET}"]
        for i, s in enumerate(sessions, 1):
            lines.append(f"  {CYAN}{i:>2}{RESET}. {s['title']}  {DIM}{s.get('model')} · {time_ago(s['updated'])}{RESET}")
        out("\n".join(lines) + "\n")
        return
    match = sessions[int(arg) - 1] if arg.isdigit() and 1 <= int(arg) <= len(sessions) else \
        next((s for s in sessions if s["id"] == arg), None)
    if not match:
        out(f"{RED}⏺ no saved chat '{arg}' (try /resume to list them){RESET}\n")
        return
    if st.backend == "sdk":
        if not match.get("sdk_session_id"):
            out(f"{RED}⏺ that chat has no Claude session id, so it can't be resumed{RESET}\n")
            return
        st.model = match.get("model") or st.model
        if st.client:
            await st.client.disconnect()
        st.client = await make_client(resume=match["sdk_session_id"])
    else:
        st.model = match.get("model") or st.model
        st.messages[:] = match.get("messages") or []
    st.session = {k: match.get(k) for k in ("id", "title", "created", "sdk_session_id")}
    st.usage.update(input=0, output=0, cost=0.0)
    st.usage.update({k: v for k, v in (match.get("usage") or {}).items() if k in st.usage})
    st.activity = [tuple(a) for a in match.get("activity") or []]
    st.changed = {k: list(v) for k, v in (match.get("changed") or {}).items()}
    st.output = match.get("screen", "") if not st.headless else ""
    st.trim_notice = False
    out(f"{GREEN}⏺ Resumed:{RESET} {match['title']} {DIM}({time_ago(match['updated'])}){RESET}\n")


# --- Commands ---

COMMANDS = {
    "/help": "commands and keys",
    "/model": "list models, or switch: /model <name>",
    "/provider": "list providers, or switch: /provider <name>",
    "/resume": "list saved chats, or /resume <n>",
    "/compact": "summarize the chat to free up context",
    "/undo": "revert the last file change",
    "/learn": "learn from this chat: save lessons and skills",
    "/memory": "show memory",
    "/skills": "list skills (run one with /<name>)",
    "/auto": "toggle auto-approve for edits and commands",
    "/rea": "reverse-engineering tools: status, or /rea on|off",
    "/recap": "what happened this session, and what's next",
    "/shells": "background shells: list, or /shells stop <name>",
    "/cost": "tokens and cost this session",
    "/config": "show settings (change: nanocode setup)",
    "/terminal-setup": "make Shift+Enter, Cmd+Backspace and Cmd+arrows work in this terminal",
    "/c": "new chat (old one stays saved)",
    "/q": "quit",
}
ALIASES = {"/clear": "/c", "/new": "/c", "/exit": "/q", "/quit": "/q"}
KEYS_HELP = [("Enter", "send"), ("Shift+Enter / Alt+Enter / Ctrl+J", "new line"),
             ("Ctrl+U (Cmd+Backspace)", "delete everything before the cursor"),
             ("Ctrl/Alt+← →", "jump by word"), ("Home/End, Ctrl+A/E", "start/end of line"),
             ("Esc", "stop the reply"), ("Ctrl+C", "stop the reply, clear the input, or quit"),
             ("↑ / ↓", "input history"), ("PageUp / PageDown", "scroll"), ("Tab", "complete commands, models, @files")]


def show_help():
    lines = [f"{BOLD}Commands{RESET}"]
    lines += [f"  {CYAN}{cmd:<9}{RESET} {DIM}{desc}{RESET}" for cmd, desc in COMMANDS.items()]
    lines += ["", f"{BOLD}Keys{RESET}"]
    lines += [f"  {CYAN}{key:<34}{RESET} {DIM}{desc}{RESET}" for key, desc in KEYS_HELP]
    lines += ["", f"  {DIM}Shift+Enter or Cmd+Backspace not working? /terminal-setup configures your terminal{RESET}"]
    lines += ["", f"  {DIM}@path      attach a file to your message{RESET}",
              f"  {DIM}# note     save a note to project memory{RESET}",
              f"  {DIM}/<skill>   run a skill, e.g. /review app.py{RESET}"]
    out("\n".join(lines) + "\n")


def model_choices():
    if st.backend == "sdk":
        return SDK_MODELS
    if st.backend == "ollama":
        return {m["name"]: f"{m['params']} · {m['size_gb']:.1f} GB" + ("" if m["tools"] else " · no tool support")
                for m in st.ollama_models}
    return OPENROUTER_MODELS


def provider_status(name):
    """Why a provider can't be used right now, or None if it's ready."""
    if name == "claude":
        return None if sdk else "claude-agent-sdk isn't installed on this system"
    if name == "openrouter":
        return None if os.environ.get("OPENROUTER_API_KEY") or st.config.get("openrouter_api_key") else \
            "no API key yet: run `nanocode setup` (or set OPENROUTER_API_KEY)"
    if name == "ollama":
        try:
            ollama_models(st.config.get("ollama_url") or OLLAMA_DEFAULT_URL, timeout=1)
            return None if st.config.get("models", {}).get("ollama") else "no model chosen yet: run `nanocode setup`"
        except Exception:
            return "Ollama isn't running (open the Ollama app, or run: ollama serve)"
    return "unknown provider"


async def cmd_provider(arg):
    current = st.config.get("provider", "claude")
    if not arg:
        lines = [f"{BOLD}Providers{RESET} {DIM}(/provider <name>){RESET}"]
        for name, (_, label, desc) in PROVIDERS.items():
            mark = f"{GREEN}●{RESET}" if name == current else " "
            problem = provider_status(name)
            note = f"{YELLOW}{problem}{RESET}" if problem else f"{DIM}{desc}{RESET}"
            model = st.config.get("models", {}).get(name) or DEFAULT_MODELS.get(name) or ""
            lines.append(f" {mark} {CYAN}{name:<11}{RESET} {label:<20} {DIM}{model:<28}{RESET} {note}")
        out("\n".join(lines) + "\n")
        return
    name = arg.split()[0].lower()
    if name not in PROVIDERS:
        out(f"{RED}⏺ unknown provider '{name}'{RESET} {DIM}(choose: {', '.join(PROVIDERS)}){RESET}\n")
        return
    if name == current:
        out(f"{DIM}already using {PROVIDERS[name][1]}{RESET}\n")
        return
    problem = provider_status(name)
    if problem:
        out(f"{RED}⏺ can't switch to {PROVIDERS[name][1]}:{RESET} {problem}\n")
        return
    old_backend, saved = st.backend, dict(st.config)
    os.environ.pop("MODEL", None)  # a `-m` from the command line was meant for the old provider
    st.config["provider"] = name
    error = apply_config(name)
    if error:
        st.config.clear()
        st.config.update(saved)
        apply_config()
        out(f"{RED}⏺ can't switch: {error}{RESET}\n")
        return
    if old_backend == "sdk" and st.client:
        await st.client.disconnect()
        st.client = None
    keeps_history = old_backend != "sdk" and st.backend != "sdk"
    if not keeps_history:
        st.messages.clear()
        new_session()
    if st.backend == "sdk":
        st.client = await make_client()
    save_config()
    st.ctx_budget = st.ctx_tokens = 0
    note = "the chat continues" if keeps_history else "new chat started (the old one is saved: /resume)"
    out(f"{GREEN}⏺ Switched to {PROVIDERS[name][1]}{RESET} · {st.model} {DIM}· {note}{RESET}\n")


async def cmd_model(arg):
    if arg:
        st.model = arg
        if st.backend == "sdk" and st.client:
            await st.client.set_model(arg)
        st.config.setdefault("models", {})[st.config.get("provider", "")] = arg
        save_config()
        out(f"{DIM}⏺ model: {arg} (saved as default){RESET}\n")
        return
    if st.backend == "ollama":
        refresh_ollama_models()
    title = "installed in Ollama" if st.backend == "ollama" else "input / output per 1M tokens"
    lines = [f"{BOLD}Models{RESET} {DIM}({title}){RESET}"]
    if st.backend == "ollama" and not st.ollama_models:
        lines.append(f"  {DIM}none found at {st.ollama_url}; is Ollama running? Pull one: ollama pull qwen3{RESET}")
    for name, desc in model_choices().items():
        mark = f"{GREEN}●{RESET}" if name == st.model else " "
        lines.append(f" {mark} {CYAN}{name:<32}{RESET} {DIM}{desc}{RESET}")
    if st.backend == "api":
        lines.append(f"\n  {DIM}any slug from openrouter.ai/models works too{RESET}")
    out("\n".join(lines) + "\n")


def cmd_undo():
    if not st.undo:
        out(f"{DIM}nothing to undo{RESET}\n")
        return
    entry = st.undo.pop()
    path = Path(entry["path"])
    if entry["content"] is None:
        if path.exists():
            path.unlink()
        out(f"{GREEN}⏺ Undone:{RESET} removed {tilde(path)} {DIM}(it was created by nanocode){RESET}\n")
    else:
        current = read_text(path) if path.exists() else ""
        write_text(path, entry["content"])
        added, removed = diff_stats(current, entry["content"])
        out(f"{GREEN}⏺ Undone:{RESET} restored {tilde(path)} {DIM}(+{added} -{removed}){RESET}\n")
    if st.backend != "sdk":
        st.messages.append({"role": "user", "content": f"[I undid your last change to {path}.]"})
        st.messages.append({"role": "assistant", "content": "Noted."})


TERMINAL_MARK = "nanocode: Shift+Enter = new line, Cmd+Backspace = delete line, Cmd+arrows = line start/end"


def detect_terminal():
    env = os.environ
    if env.get("KITTY_WINDOW_ID") or env.get("TERM") == "xterm-kitty":
        return "kitty"
    if env.get("GHOSTTY_RESOURCES_DIR") or env.get("TERM_PROGRAM") == "ghostty":
        return "ghostty"
    if env.get("TERM_PROGRAM") == "vscode":
        hint = (env.get("VSCODE_GIT_ASKPASS_MAIN", "") + env.get("VSCODE_IPC_HOOK_CLI", "")).lower()
        return "cursor" if "cursor" in hint else "windsurf" if "windsurf" in hint else "vscode"
    if env.get("TERM_PROGRAM") == "WezTerm":
        return "wezterm"
    if env.get("TERM_PROGRAM") == "iTerm.app":
        return "iterm2"
    if env.get("TERM_PROGRAM") == "Apple_Terminal":
        return "terminal.app"
    if env.get("ALACRITTY_WINDOW_ID") or env.get("ALACRITTY_SOCKET"):
        return "alacritty"
    if env.get("WT_SESSION"):
        return "windows-terminal"
    return None


def terminal_config_change(terminal):
    """(config file, text to append) for terminals whose config nanocode can safely add to, else None."""
    mac = sys.platform == "darwin"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if terminal == "kitty":
        path = Path(os.environ["KITTY_CONFIG_DIRECTORY"]) / "kitty.conf" if os.environ.get("KITTY_CONFIG_DIRECTORY") \
            else config_home / "kitty" / "kitty.conf"
        lines = ["map shift+enter send_text all \\x1b\\r"]
        if mac:
            lines += ["map cmd+backspace send_text all \\x15", "map cmd+left send_text all \\x01",
                      "map cmd+right send_text all \\x05"]
        return path, f"\n# {TERMINAL_MARK}\n" + "\n".join(lines) + "\n"
    if terminal == "ghostty":
        path = config_home / "ghostty" / "config"
        if mac and not path.exists() and (Path.home() / "Library/Application Support/com.mitchellh.ghostty/config").exists():
            path = Path.home() / "Library/Application Support/com.mitchellh.ghostty/config"
        lines = ["keybind = shift+enter=text:\\x1b\\r"]
        if mac:
            lines += ["keybind = super+backspace=text:\\x15", "keybind = super+left=text:\\x01",
                      "keybind = super+right=text:\\x05"]
        return path, f"\n# {TERMINAL_MARK}\n" + "\n".join(lines) + "\n"
    return None


def editor_keybindings_path(terminal):
    app = {"vscode": "Code", "cursor": "Cursor", "windsurf": "Windsurf"}[terminal]
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app / "User" / "keybindings.json"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", "")) / app / "User" / "keybindings.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / app / "User" / "keybindings.json"


MANUAL_TERMINAL_SETUP = {
    "iterm2": "iTerm2 → Settings → Profiles → Keys → Key Mappings → + : Shift+Enter → 'Send Escape Sequence' → \\r. "
              "For Cmd+Backspace and Cmd/Option+arrows, pick Presets → 'Natural Text Editing'.",
    "terminal.app": "Terminal.app can't remap Shift+Enter: use Option+Enter (Settings → Profiles → Keyboard → "
                    "'Use Option as Meta key') or Ctrl+J for new lines.",
    "wezterm": "add to ~/.wezterm.lua keys: { key = 'Enter', mods = 'SHIFT', action = wezterm.action.SendString '\\x1b\\r' }",
    "alacritty": "add to alacritty.toml: [[keyboard.bindings]] key = \"Return\" mods = \"Shift\" chars = \"\\u001b\\r\"",
    "windows-terminal": "Settings → Open JSON file → actions: { \"command\": { \"action\": \"sendInput\", "
                        "\"input\": \"\\u001b\\r\" }, \"keys\": \"shift+enter\" }",
}


async def terminal_setup():
    terminal = detect_terminal()
    if terminal is None:
        out(f"{DIM}couldn't tell which terminal this is. New lines always work with Alt+Enter or Ctrl+J; "
            f"Ctrl+U deletes everything before the cursor.{RESET}\n")
        return
    if terminal in MANUAL_TERMINAL_SETUP:
        out(f"⏺ {terminal}: {MANUAL_TERMINAL_SETUP[terminal]}\n  {DIM}Meanwhile, Alt+Enter and Ctrl+J insert new lines.{RESET}\n")
        return
    if terminal in ("vscode", "cursor", "windsurf"):
        path = editor_keybindings_path(terminal)
        entry = {"key": "shift+enter", "command": "workbench.action.terminal.sendSequence",
                 "args": {"text": "\u001b\r"}, "when": "terminalFocus"}
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() and path.read_text().strip() else []
        except (OSError, json.JSONDecodeError):
            out(f"⏺ {terminal}: add this to {tilde(path)} (it has comments, so I won't edit it):\n  {json.dumps(entry)}\n")
            return
        if any(e.get("key") == "shift+enter" and "terminal" in str(e.get("when", "")) for e in current if isinstance(e, dict)):
            out(f"⏺ {terminal} already sends something for Shift+Enter in the terminal ({tilde(path)})\n")
            return
        if not await allowed("terminal-setup", f"add Shift+Enter = new line to {tilde(path)}"):
            return
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".nanocode-backup"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current + [entry], indent=2) + "\n", encoding="utf-8")
        out(f"{GREEN}⏺ Done:{RESET} Shift+Enter now inserts a new line in {terminal}'s terminal {DIM}(backup: "
            f"{path.name}.nanocode-backup){RESET}\n")
        return
    path, text = terminal_config_change(terminal)
    existing = read_text(path) if path.exists() else ""
    if TERMINAL_MARK in existing:
        out(f"⏺ {terminal} is already set up {DIM}({tilde(path)}){RESET}\n")
        return
    summary = "Shift+Enter = new line" + (", Cmd+Backspace = delete line, Cmd+←/→ = line start/end"
                                          if sys.platform == "darwin" else "")
    if not await allowed("terminal-setup", f"add to {tilde(path)}: {summary}"):
        return
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".nanocode-backup"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    reload = {"kitty": "reload kitty's config (Ctrl+Shift+F5, or Cmd+Ctrl+, on macOS) or open a new window",
              "ghostty": "reload Ghostty's config (Cmd+Shift+, on macOS) or open a new window"}[terminal]
    out(f"{GREEN}⏺ Done:{RESET} {summary} in {terminal}. To apply it, {reload}. "
        f"{DIM}(backup: {path.name}.nanocode-backup){RESET}\n")


async def cmd_shells(arg):
    parts = arg.split()
    if len(parts) == 2 and parts[0] == "stop":
        info = st.shells.get(parts[1])
        if info:
            await shell_stop(info)
            out(f"{GREEN}⏺ Stopped{RESET} {parts[1]}\n")
        else:
            out(f"{RED}⏺ no shell named {parts[1]}{RESET}\n")
        return
    if not st.shells:
        out(f"{DIM}no background shells · backend: {shell_backend()} · the model starts them for dev servers, "
            f"watchers and long builds{RESET}\n")
        return
    lines = [f"{BOLD}Background shells{RESET} {DIM}(stop: /shells stop <name>; they stop when nanocode exits){RESET}"]
    for info in list(st.shells.values()):
        state = f"{GREEN}running{RESET}" if await shell_alive(info) else f"{DIM}finished{RESET}"
        watch = f" {DIM}· watch: {info['attach']}{RESET}" if info.get("attach") else ""
        lines.append(f"  {CYAN}{info['name']}{RESET} {state} {DIM}{info['command'][:60]}{RESET}{watch}")
    out("\n".join(lines) + "\n")


async def cmd_rea(arg):
    if arg in ("on", "off"):
        st.config["rea"] = arg == "on"
        save_config()
        if arg == "off" and st.rea:
            await st.rea.close()
        await refresh_sdk_prompt()
    info = rea_runtime()
    if info is None:
        out(f"{DIM}REA isn't installed. It needs Node.js 22+; then run: nanocode update{RESET}\n")
        return
    state = f"{GREEN}on{RESET}" if rea_enabled() else f"{YELLOW}off{RESET} {DIM}(/rea on){RESET}"
    running = "running" if st.rea and st.rea.alive() else "starts on first use"
    tools = f" · {len(st.rea.tools)} tools" if st.rea and st.rea.tools else ""
    out(f"⏺ REA {info.get('version', '')} {state} {DIM}· {running}{tools} · skill: /{REA_SKILL}{RESET}\n")


def show_memory():
    files = memory_files()
    lines = [f"{BOLD}Memory{RESET} {DIM}(loaded into every chat){RESET}"]
    if not files:
        lines.append(f"  {DIM}empty: type '# something to remember', or tell me to remember it{RESET}")
    for label, path, text in files:
        lines.append(f"\n  {CYAN}{tilde(path)}{RESET} {DIM}({label}){RESET}")
        lines += [f"  {DIM}│{RESET} {line}" for line in text.splitlines()[:40]]
    lines.append(f"\n  {DIM}edit: {tilde(project_memory_file())} (project) · {tilde(global_memory_file())} (global){RESET}")
    out("\n".join(lines) + "\n")


def show_skills():
    skills = list_skills()
    lines = [f"{BOLD}Skills{RESET} {DIM}(loaded when needed, or run /<name>){RESET}"]
    if not skills:
        lines.append(f"  {DIM}none yet. nanocode saves skills as it learns, or add "
                     f".nanocode/skills/<name>/SKILL.md yourself{RESET}")
    for name, info in skills.items():
        lines.append(f"  {CYAN}/{name:<18}{RESET} {info['description'][:70]}  {DIM}{tilde(info['path'])}{RESET}")
    out("\n".join(lines) + "\n")


def show_config():
    lines = [f"{BOLD}Config{RESET} {DIM}{tilde(config_file())}{RESET}"]
    for key, value in st.config.items():
        if "key" in key and isinstance(value, str) and value:
            value = f"{value[:8]}…{value[-4:]}"
        lines.append(f"  {CYAN}{key:<18}{RESET} {value}")
    lines.append(f"\n  {DIM}change provider / key / model: nanocode setup · switch model now: /model{RESET}")
    return "\n".join(lines)


def expand_mentions(text):
    attached = []
    for raw in dict.fromkeys(re.findall(r"@([^\s]+)", text)):
        raw = raw.rstrip(".,;:!?)")
        path = resolve(raw)
        if path.is_file() and not is_binary(path):
            try:
                content = read_text(path)
            except OSError:
                continue
            attached.append(f'<file path="{raw}">\n{cap(content, 100_000)}\n</file>')
            out(f"  {DIM}⎿  attached {raw}{RESET}")
    return text + ("\n\n" + "\n\n".join(attached) if attached else "")


async def refresh_sdk_prompt():
    """The SDK fixes the system prompt at connect time; reconnect (same chat) so new memory applies now."""
    if st.backend == "sdk" and st.client:
        await st.client.disconnect()
        st.client = await make_client(resume=st.session.get("sdk_session_id"))


def user_block(text):
    """Your message: an accent bar on the left, on a raised panel."""
    width = messages_width()
    inner = max(10, width - 1)
    bar, panel = f"{fg('accent')}┃{RESET}", bg("panel")
    body = []
    for line in text.split("\n"):
        indent = " " * (len(line) - len(line.lstrip(" ")))
        chunks = textwrap.wrap(line.strip(" "), max(10, inner - 4 - len(indent)), replace_whitespace=False)
        body += [indent + chunks[0]] + chunks[1:] if chunks else [""]
    rows = [f"{bar}{panel}{' ' * inner}{RESET}"]
    rows += [f"{bar}{panel}  {fg('text')}{line.ljust(inner - 4)}  {RESET}" for line in body]
    rows.append(f"{bar}{panel}{' ' * inner}{RESET}")
    return "\n" + "\n".join(rows)


def short_title(text, limit=60):
    line = " ".join(text.split("\n")[0].split())
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0]
    return (cut if len(cut) > limit // 2 else line[:limit]).rstrip(" ,.;:") + "…"


async def send(text, display=None):
    """Sends a user message to the model and runs the turn."""
    if display is not None and not st.headless:
        out(user_block(display))
    if not st.session.get("title"):
        st.session["title"] = short_title(display or text)
    st.turn_errors = 0
    try:
        if st.backend == "sdk":
            await sdk_query(text)
        else:
            await api_query(text)
    finally:
        save_session()


async def run_input(text):
    """Handles one input from the prompt: a command, a # memory note, a /skill, or a message."""
    st.last_error = None
    try:
        word, _, arg = text.partition(" ")
        word = ALIASES.get(word, word)
        arg = arg.strip()
        if text.startswith("#") and len(text) > 1 and not text.startswith("#!"):
            path = add_memory(text.lstrip("#").strip())
            out(f"{GREEN}⏺ Remembered{RESET} {DIM}→ {tilde(path)}{RESET}\n")
            await refresh_sdk_prompt()
        elif word == "/help":
            show_help()
        elif word == "/provider":
            await cmd_provider(arg)
        elif word == "/model":
            await cmd_model(arg)
        elif word == "/resume":
            await resume_session(arg)
        elif word == "/compact":
            await compact()
        elif word == "/undo":
            cmd_undo()
        elif word == "/learn":
            await send(LEARN_PROMPT, display="/learn")
        elif word == "/memory":
            show_memory()
        elif word == "/skills":
            show_skills()
        elif word == "/rea":
            await cmd_rea(arg)
        elif word == "/auto":
            st.auto_approve = not st.auto_approve if arg not in ("on", "off") else arg == "on"
            state = f"{YELLOW}on{RESET}: edits and commands run without asking (risky ones still ask)" \
                if st.auto_approve else f"{GREEN}off{RESET}: I'll ask before edits and commands"
            out(f"⏺ Auto-approve {state}\n")
        elif word == "/shells":
            await cmd_shells(arg)
        elif word == "/recap":
            await recap()
        elif word == "/cost":
            u = st.usage
            cost = f" · ${u['cost']:.4f}" if u["cost"] else ""
            out(f"{DIM}session: {fmt_tokens(u['input'])} in · {fmt_tokens(u['output'])} out{cost}{RESET}\n")
        elif word == "/terminal-setup":
            await terminal_setup()
        elif word == "/config":
            out(show_config() + "\n")
        elif word == "/c":
            if st.backend == "sdk":
                if st.client:
                    await st.client.disconnect()
                st.client = await make_client()
            st.messages.clear()
            new_session()
            st.output, st.trim_notice, st.ctx_tokens = "", False, 0
            out(f"{GREEN}⏺ New chat{RESET} {DIM}(the old one is saved: /resume){RESET}\n")
        elif word.startswith("/") and word[1:] in list_skills():
            out(f"  {DIM}⎿  loaded skill {word[1:]}{RESET}")
            task = expand_mentions(arg) if arg else f"Run the {word[1:]} skill."
            await send(load_skill(word[1:]) + "\n\n" + task, display=text)
        elif word.startswith("/") and " " not in word[1:] and len(word) > 1 and "/" not in word[1:]:
            out(f"{RED}⏺ unknown command {word}{RESET} {DIM}(/help lists them){RESET}\n")
        else:
            await send(expand_mentions(text), display=text)
            if st.turn_errors >= 2 and st.config.get("auto_learn"):
                out(f"  {DIM}⎿  that took a few tries; learning from it (auto_learn){RESET}")
                await send(LEARN_PROMPT, display="/learn")
            elif st.turn_errors >= 3 and not st.headless:
                out(f"  {DIM}tip: that took a few tries; /learn saves the lessons for next time{RESET}")
            out()
    except asyncio.CancelledError:
        stream_end()
        out()
    except Exception as e:
        stream_end()
        st.last_error = str(e)
        log_error(e)
        out(f"{RED}⏺ Error: {e}{RESET}\n")


# --- Interactive UI (prompt_toolkit) ---

LOGO = [
    "█▀▀▄ ▄▀▀▄ █▀▀▄ ▄▀▀▄ ▄▀▀▀ ▄▀▀▄ █▀▀▄ █▀▀▀",
    "█  █ █▀▀█ █  █ █  █ █    █  █ █  █ █▀▀ ",
    "▀  ▀ ▀  ▀ ▀  ▀  ▀▀   ▀▀▀  ▀▀  ▀▀▀  ▀▀▀▀",
]
LOGO_COLORS = [51, 45, 39, 33, 63, 99, 135, 171]


def gradient(line):
    result = ""
    for i, ch in enumerate(line):
        result += f"\033[38;5;{LOGO_COLORS[i * len(LOGO_COLORS) // len(line)]}m{ch}" if ch != " " else " "
    return result + RESET


def pretty_model(model):
    return SDK_MODELS.get(model, model) if model in SDK_MODELS and not model.isalpha() else (model or "")


def banner():
    """Compact header: the gradient logo, with name/version, model and folder beside it."""
    width = shutil.get_terminal_size((80, 24)).columns
    logo = [gradient(line) for line in LOGO]
    logo_width = len(LOGO[0])
    side_width = max(20, width - logo_width - 6)
    cwd = tilde(os.getcwd())
    if len(cwd) > side_width:
        cwd = "…" + cwd[-(side_width - 1):]
    info = [f"{BOLD}nanocode{RESET} {DIM}v{__version__}{RESET}",
            f"{pretty_model(st.model)} {DIM}·{RESET} {provider_label()}",
            f"{DIM}{cwd}{RESET}"]
    if width >= logo_width + 30:
        rows = [f" {logo_line}   {text}" for logo_line, text in zip(logo, info)]
    else:
        rows = [f" {line}" for line in logo] + [""] + [f" {text}" for text in info]
    name = user_name()
    greeting = f"Welcome back, {name}" if name else "Welcome to nanocode"
    n_memory, n_skills = len(memory_files()), len(list_skills())
    extras = [greeting]
    if n_memory or n_skills:
        extras.append(f"{n_memory} memory file{'s' if n_memory != 1 else ''} · {n_skills} skill{'s' if n_skills != 1 else ''}")
    rows += ["", f" {DIM}{' · '.join(extras)}{RESET}",
             f" {DIM}/ commands · @ attach file · # remember · /resume · /help{RESET}"]
    return "\n" + "\n".join(rows) + "\n"


SIDEBAR_WIDTH = 42
PLACEHOLDERS = ["Fix a TODO in the codebase", "What is the tech stack of this project?", "Fix broken tests",
                "Explain how this repo is structured", "Add tests for the last change"]
LOGO_HEX = ["#6fb6c6", "#6aa9cc", "#6f9ccb", "#7a92c8", "#8a8cc4", "#9b88bf", "#ad87b6", "#bf89ae"]


def sidebar_visible():
    if st.app is None or not st.app.is_running or not st.sidebar_on or st.home:
        return False
    return st.app.output.get_size().columns >= 110


def start_input(text):
    """Runs a prompt line (message or /command) in the background, like pressing Enter."""
    if st.task and not st.task.done():
        out(f"   {fg('muted')}still working; press Esc twice to stop{RESET}")
        return False
    st.home = False
    st.scroll = 0
    st.task_started = time.time()
    st.task = asyncio.ensure_future(run_input(text))
    return True


def build_app():
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    # Shift/Ctrl+Enter in terminals that report them distinctly (kitty protocol, xterm modifyOtherKeys):
    # treat them as Ctrl+J, which inserts a new line. Plain Enter (\r) still sends.
    for seq in ("\x1b[13;2u", "\x1b[27;2;13~", "\x1b[13;5u", "\x1b[27;5;13~"):
        ANSI_SEQUENCES[seq] = Keys.ControlJ
    from prompt_toolkit.application import Application
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition, completion_is_selected, has_completions
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float, FloatContainer, HSplit, Layout,
                                       VSplit, Window, WindowAlign)
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.layout.processors import Processor, Transformation
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import TextArea

    T = THEME

    class NanoCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/resume "):
                typed = text[len("/resume "):]
                for i, s in enumerate(list_sessions(), 1):
                    if typed in str(i) or typed.lower() in s["title"].lower():
                        yield Completion(str(i), start_position=-len(typed), display=f"{i}. {s['title']}",
                                         display_meta=f"{s.get('model')} · {time_ago(s['updated'])}")
                return
            if text.startswith("/provider "):
                typed = text[len("/provider "):]
                for name, (_, label, desc) in PROVIDERS.items():
                    if name.startswith(typed.lower()):
                        yield Completion(name, start_position=-len(typed), display_meta=f"{label} · {desc}")
                return
            if text.startswith("/model "):
                typed = text[len("/model "):]
                for name, desc in model_choices().items():
                    if typed.lower() in name.lower():
                        yield Completion(name, start_position=-len(typed), display_meta=desc)
                return
            if text.startswith("/") and " " not in text:
                for cmd, desc in COMMANDS.items():
                    if cmd.startswith(text):
                        yield Completion(cmd, start_position=-len(text), display_meta=desc)
                for name, info in list_skills().items():
                    if f"/{name}".startswith(text) and f"/{name}" not in COMMANDS:
                        yield Completion(f"/{name}", start_position=-len(text),
                                         display_meta=f"skill · {info['description'][:60]}")
                return
            word = document.get_word_before_cursor(WORD=True)
            if word.startswith("@"):
                prefix = word[1:]
                try:
                    matches = sorted(p for p in glob.glob(os.path.expanduser(prefix) + "*")
                                     if os.path.basename(p) not in IGNORE_DIRS)[:50]
                except OSError:
                    matches = []
                for path in matches:
                    shown = path + "/" if os.path.isdir(path) else path
                    yield Completion("@" + shown, start_position=-len(word), display=shown)

    class Placeholder(Processor):
        def apply_transformation(self, ti):
            if ti.document.text == "" and ti.lineno == 0:
                hint = PLACEHOLDERS[int(st.session.get("created", 0)) % len(PLACEHOLDERS)]
                return Transformation([("class:placeholder", f'Ask anything... "{hint}"')])
            return Transformation(ti.fragments)

    try:
        history_file().parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_file()))
    except OSError:
        history = InMemoryHistory()

    # --- transcript ---
    st.output_window = Window(
        FormattedTextControl(lambda: ANSI(visible_output()),
                             get_cursor_position=lambda: Point(x=0, y=visible_output().count("\n")),
                             show_cursor=False),
        wrap_lines=True, style="class:transcript",
    )

    # --- prompt panel ---
    st.input_box = TextArea(multiline=True, height=Dimension(min=1, max=10), dont_extend_height=True,
                            completer=NanoCompleter(), complete_while_typing=True, history=history,
                            style="class:panel", input_processors=[Placeholder()])
    st.input_box.accept_handler = on_enter

    def meta_row():
        mode = ("Auto", "class:mode.auto") if st.auto_approve else ("Ask", "class:mode")
        return [(mode[1], mode[0]), ("class:panel", "  "), ("class:panel.text", pretty_model(st.model)),
                ("class:panel", "  "), ("class:panel.muted", provider_label())]

    def bar_style():
        if st.pending_permission is not None:
            return "class:bar.warning"
        return "class:bar.busy" if st.task and not st.task.done() else "class:bar"

    pad = lambda width: Window(width=width, style="class:panel")  # noqa: E731
    prompt_panel = HSplit([
        VSplit([
            Window(width=1, char="┃", style=bar_style),
            pad(2),
            HSplit([Window(height=1, style="class:panel"), st.input_box, Window(height=1, style="class:panel"),
                    Window(FormattedTextControl(meta_row), height=1, style="class:panel")]),
            pad(2),
        ]),
        VSplit([Window(width=1, char="╹", style=bar_style), Window(height=1, char="▀", style="class:panel.edge")]),
    ])

    def footer_left():
        if st.pending_permission is not None:
            return [("class:warning", " △ "), ("class:muted", "waiting for permission: y allow · a always · n deny")]
        if st.task and not st.task.done():
            frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 10) % 10]
            armed = st.interrupt_armed and time.time() - st.interrupt_armed < 2
            return [("class:accent", f" {frame} "), ("class:muted", f"{fmt_duration(time.time() - st.task_started)}  "),
                    ("class:warning" if armed else "class:muted", "esc again to interrupt" if armed else "esc interrupt")]
        branch = git_branch()
        right = sum(len(text) for _, text in footer_right())
        total = st.app.output.get_size().columns - (SIDEBAR_WIDTH if sidebar_visible() else 0) - 4
        room = max(8, total - right - len(branch) - 5)
        return [("class:muted", f" {fit_left(tilde(os.getcwd()), room)}"), ("class:faint", f"  {branch}" if branch else "")]

    def footer_right():
        parts = []
        if st.scroll:
            parts.append(("class:muted", f"↑{st.scroll} pgdn  "))
        if st.backend != "sdk" and st.ctx_budget and st.messages:
            pct = min(100, st.ctx_tokens * 100 // st.ctx_budget)
            parts.append(("class:warning" if pct >= 70 else "class:muted", f"{pct}% context  "))
        if st.usage["input"] or st.usage["output"]:
            parts.append(("class:muted", f"{fmt_cost(st.usage['cost'])}  "))
        parts += [("class:text", "ctrl+p"), ("class:muted", " commands ")]
        return parts

    footer = VSplit([Window(FormattedTextControl(footer_left), height=1),
                     Window(FormattedTextControl(footer_right), height=1, align=WindowAlign.RIGHT)])

    # --- sidebar ---
    def sidebar_text():
        width = SIDEBAR_WIDTH - 4
        lines = []
        title = st.session.get("title") or "New session"
        for chunk in textwrap.wrap(title, width)[:3]:
            lines += [("class:sidebar.title", chunk), ("", "\n")]
        lines.append(("", "\n"))
        lines += [("class:sidebar.heading", "Context"), ("", "\n")]
        tokens = st.usage["input"] + st.usage["output"]
        lines += [("class:sidebar.text", f"{tokens:,} tokens"), ("", "\n")]
        if st.backend != "sdk" and st.ctx_budget:
            lines += [("class:sidebar.muted", f"{min(100, st.ctx_tokens * 100 // st.ctx_budget)}% used"), ("", "\n")]
        lines += [("class:sidebar.muted", f"{fmt_cost(st.usage['cost'])} spent"), ("", "\n"), ("", "\n")]
        if st.changed:
            lines += [("class:sidebar.heading", "Modified Files"), ("", "\n")]
            for path, (added, removed) in list(st.changed.items())[-12:]:
                name = path if len(path) <= width - 12 else "…" + path[-(width - 13):]
                lines += [("class:sidebar.text", name), ("class:success", f" +{added}"),
                          ("class:error", f" -{removed}"), ("", "\n")]
            lines.append(("", "\n"))
        if st.shells:
            lines += [("class:sidebar.heading", "Shells"), ("", "\n")]
            for name, info in st.shells.items():
                lines += [("class:accent", "● "), ("class:sidebar.text", name),
                          ("class:sidebar.muted", f"  {info['command'][:width - len(name) - 4]}"), ("", "\n")]
            lines.append(("", "\n"))
        if rea_runtime() is not None:
            state = "on" if rea_enabled() else "off"
            lines += [("class:sidebar.heading", "REA"), ("", "\n"),
                      ("class:sidebar.muted", f"{state} · {'running' if st.rea and st.rea.alive() else 'idle'}"),
                      ("", "\n"), ("", "\n")]
        return lines

    def sidebar_footer():
        return [("class:sidebar.muted", fit_left(tilde(os.getcwd()), SIDEBAR_WIDTH - 4)), ("", "\n"),
                ("class:accent", "● "), ("class:sidebar.text", "nanocode"), ("class:sidebar.muted", f" v{__version__}")]

    sidebar = ConditionalContainer(
        VSplit([Window(width=2, style="class:sidebar"),
                HSplit([Window(height=1, style="class:sidebar"),
                        Window(FormattedTextControl(sidebar_text), style="class:sidebar", wrap_lines=True),
                        Window(FormattedTextControl(sidebar_footer), height=2, style="class:sidebar"),
                        Window(height=1, style="class:sidebar")]),
                Window(width=2, style="class:sidebar")], width=SIDEBAR_WIDTH),
        filter=Condition(sidebar_visible))

    # --- home screen: logo and prompt in the middle ---
    def logo_fragments():
        fragments = []
        for row in LOGO:
            for i, ch in enumerate(row):
                color = LOGO_HEX[i * len(LOGO_HEX) // len(row)]
                fragments.append((f"fg:{color} bold" if ch != " " else "", ch))
            fragments.append(("", "\n"))
        return fragments

    def home_hint():
        name = user_name()
        hello = f"Welcome back, {name}" if name else "Welcome to nanocode"
        return [("class:muted", f"{hello} · "), ("class:text", "ctrl+p"), ("class:muted", " commands · "),
                ("class:text", "/"), ("class:muted", " commands · "), ("class:text", "@"), ("class:muted", " files")]

    def home_footer():
        return [("class:muted", f" {fit_left(tilde(os.getcwd()), st.app.output.get_size().columns - 14)}")]

    home = HSplit([
        Window(),
        Window(FormattedTextControl(logo_fragments), height=len(LOGO), align=WindowAlign.CENTER),
        Window(height=2),
        VSplit([Window(), HSplit([prompt_panel], width=Dimension(max=75, preferred=75)), Window()]),
        Window(height=1),
        Window(FormattedTextControl(home_hint), height=1, align=WindowAlign.CENTER),
        Window(),
        VSplit([Window(FormattedTextControl(home_footer), height=1),
                Window(FormattedTextControl(lambda: [("class:muted", f"v{__version__} ")]), height=1,
                       align=WindowAlign.RIGHT)]),
    ])

    session = VSplit([
        HSplit([
            VSplit([Window(width=2), st.output_window, Window(width=2)]),
            Window(height=1),
            VSplit([Window(width=2), HSplit([prompt_panel, footer]), Window(width=2)]),
        ]),
        sidebar,
    ])

    # --- dialogs: command palette, models, sessions, providers ---
    class SearchHint(Processor):
        def apply_transformation(self, ti):
            if ti.document.text == "":
                return Transformation(list(ti.fragments) + [("class:dialog.muted", "Search")])
            return Transformation(ti.fragments)

    dialog_filter = TextArea(height=1, multiline=False, style="class:dialog.input", prompt="  ",
                             input_processors=[SearchHint()])
    dialog_open = Condition(lambda: st.dialog is not None)

    def dialog_items():
        if st.dialog is None:
            return []
        query = dialog_filter.text.lower().strip()
        items = [it for it in st.dialog["items"] if not query or all(w in (it[0] + " " + it[1]).lower()
                                                                     for w in query.split())]
        st.dialog["index"] = min(st.dialog["index"], max(0, len(items) - 1))
        return items

    def dialog_list():
        items = dialog_items()
        if not items:
            return [("class:dialog.muted", "  no matches")]
        start = max(0, st.dialog["index"] - 11)
        fragments = []
        for i, (label, meta, _) in enumerate(items[start:start + 12], start):
            selected = i == st.dialog["index"]
            style = "class:dialog.selected" if selected else "class:dialog.text"
            width = 56
            text = f"  {label}"
            gap = max(1, width - len(text) - len(meta) - 2)
            fragments += [(style, text + " " * gap), (style + (" class:dialog.meta" if not selected else ""), meta + "  "),
                          ("", "\n")]
        return fragments

    def dialog_title():
        if st.dialog is None:
            return []
        return [("class:dialog.title", f"  {st.dialog['title']}"), ("class:dialog.muted", "   esc close")]

    dialog = ConditionalContainer(
        HSplit([Window(height=1, style="class:dialog"),
                Window(FormattedTextControl(dialog_title), height=1, style="class:dialog"),
                Window(height=1, style="class:dialog"),
                dialog_filter,
                Window(height=1, style="class:dialog"),
                Window(FormattedTextControl(dialog_list), height=Dimension(max=12), style="class:dialog"),
                Window(height=1, style="class:dialog")], width=60),
        filter=dialog_open)

    def open_dialog(title, items):
        st.dialog = {"title": title, "items": items, "index": 0}
        dialog_filter.text = ""
        st.app.layout.focus(dialog_filter)

    def close_dialog():
        st.dialog = None
        st.app.layout.focus(st.input_box)

    def run_command(text):
        return lambda: start_input(text)

    def models_dialog():
        if st.backend == "ollama":
            refresh_ollama_models()
        items = [(name, ("● " if name == st.model else "") + desc, run_command(f"/model {name}"))
                 for name, desc in model_choices().items()]
        open_dialog("Switch model", items)

    def sessions_dialog():
        items = [(s["title"], time_ago(s["updated"]), run_command(f"/resume {s['id']}")) for s in list_sessions()]
        open_dialog("Sessions", items or [("no saved chats yet", "", lambda: None)])

    def providers_dialog():
        items = []
        for name, (_, label, desc) in PROVIDERS.items():
            current = "● " if name == st.config.get("provider") else ""
            items.append((label, current + (provider_status(name) or desc)[:30], run_command(f"/provider {name}")))
        open_dialog("Switch provider", items)

    def palette():
        actions = [
            ("New session", "ctrl+x n", run_command("/c")),
            ("Switch session", "ctrl+x l", sessions_dialog),
            ("Switch model", "ctrl+x m", models_dialog),
            ("Switch provider", "", providers_dialog),
            ("Toggle sidebar", "ctrl+x b", toggle_sidebar),
            ("Compact session", "ctrl+x c", run_command("/compact")),
            ("Undo last file change", "ctrl+x u", run_command("/undo")),
        ]
        actions += [(f"{cmd}  {desc}", "", run_command(cmd)) for cmd, desc in COMMANDS.items()
                    if cmd not in ("/c", "/compact", "/undo", "/model", "/provider", "/resume")]
        actions += [(f"/{name}  {info['description'][:40]}", "skill", run_command(f"/{name}"))
                    for name, info in list_skills().items()]
        actions.append(("Exit", "ctrl+x q", lambda: st.app.exit()))
        open_dialog("Commands", actions)

    def toggle_sidebar():
        st.sidebar_on = not st.sidebar_on
        invalidate()

    # --- keys ---
    kb = KeyBindings()
    waiting = Condition(lambda: st.pending_permission is not None)
    busy = Condition(lambda: st.task is not None and not st.task.done())
    no_dialog = ~dialog_open

    def interrupt():
        st.interrupt_armed = 0
        if st.pending_permission is not None:
            answer_permission("n")
        elif st.backend == "sdk" and st.client:
            asyncio.ensure_future(st.client.interrupt())
        elif st.task:
            st.task.cancel()
        out(f"   {fg('error')}interrupted{RESET}")

    @kb.add("c-c", filter=no_dialog)
    def _(event):
        if busy():
            interrupt()
        elif event.current_buffer.text:
            event.current_buffer.reset()
        else:
            event.app.exit()

    @kb.add("c-d", filter=no_dialog)
    def _(event):
        if not event.current_buffer.text:
            event.app.exit()

    @kb.add("enter", filter=has_completions & completion_is_selected & no_dialog)
    def _(event):
        buff = event.current_buffer
        buff.apply_completion(buff.complete_state.current_completion)

    @kb.add("enter", filter=~(has_completions & completion_is_selected) & ~waiting & no_dialog)
    def _(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter", filter=no_dialog)
    @kb.add("c-j", filter=no_dialog)
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("c-u", filter=no_dialog)
    def _(event):
        buff = event.current_buffer
        buff.delete_before_cursor(len(buff.document.text_before_cursor))

    @kb.add("escape", "left", filter=no_dialog)
    def _(event):
        buff = event.current_buffer
        buff.cursor_position += buff.document.find_previous_word_beginning(count=1) or -buff.cursor_position

    @kb.add("escape", "right", filter=no_dialog)
    def _(event):
        buff = event.current_buffer
        buff.cursor_position += buff.document.find_next_word_ending(count=1) or len(buff.document.text_after_cursor)

    @kb.add("escape", filter=no_dialog)
    def _(event):
        buff = event.current_buffer
        if buff.complete_state:
            buff.cancel_completion()
        elif st.pending_permission is not None:
            interrupt()
        elif busy():
            if st.interrupt_armed and time.time() - st.interrupt_armed < 2:
                interrupt()
            else:
                st.interrupt_armed = time.time()
                invalidate()

    @kb.add("y", filter=waiting & no_dialog)
    @kb.add("n", filter=waiting & no_dialog)
    @kb.add("a", filter=waiting & no_dialog)
    def _(event):
        answer_permission(event.key_sequence[0].data)

    def page():
        info = st.output_window.render_info
        return max(1, info.window_height - 2) if info else 10

    @kb.add("pageup", filter=no_dialog)
    def _(event):
        st.scroll = min(st.scroll + page(), (st.output + st.live).count("\n"))
        invalidate()

    @kb.add("pagedown", filter=no_dialog)
    def _(event):
        st.scroll = max(0, st.scroll - page())
        invalidate()

    @kb.add("c-p", filter=no_dialog)
    def _(event):
        palette()

    leader = {"n": run_command("/c"), "l": sessions_dialog, "m": models_dialog, "b": toggle_sidebar,
              "c": run_command("/compact"), "u": run_command("/undo"), "h": run_command("/help"),
              "q": lambda: st.app.exit()}
    for key, action in leader.items():
        kb.add("c-x", key, filter=no_dialog)(lambda event, action=action: action())

    @kb.add("up", filter=dialog_open)
    @kb.add("c-p", filter=dialog_open)
    def _(event):
        st.dialog["index"] = max(0, st.dialog["index"] - 1)

    @kb.add("down", filter=dialog_open)
    @kb.add("c-n", filter=dialog_open)
    def _(event):
        st.dialog["index"] = min(len(dialog_items()) - 1, st.dialog["index"] + 1)

    @kb.add("enter", filter=dialog_open)
    def _(event):
        items = dialog_items()
        chosen = items[st.dialog["index"]][2] if items else None
        close_dialog()
        if chosen:
            chosen()

    @kb.add("escape", filter=dialog_open, eager=True)
    @kb.add("c-c", filter=dialog_open)
    def _(event):
        close_dialog()

    style = Style.from_dict({
        "root": f"bg:{T['bg']} fg:{T['text']}",
        "transcript": f"bg:{T['bg']}",
        "panel": f"bg:{T['panel']} fg:{T['text']}",
        "panel.text": f"bg:{T['panel']} fg:{T['text']}",
        "panel.muted": f"bg:{T['panel']} fg:{T['muted']}",
        "panel.edge": f"bg:{T['bg']} fg:{T['panel']}",
        "placeholder": f"fg:{T['faint']}",
        "mode": f"bg:{T['panel']} fg:{T['accent']} bold",
        "mode.auto": f"bg:{T['panel']} fg:{T['warning']} bold",
        "bar": f"bg:{T['bg']} fg:{T['accent']}",
        "bar.busy": f"bg:{T['bg']} fg:{T['accent2']}",
        "bar.warning": f"bg:{T['bg']} fg:{T['warning']}",
        "text": f"fg:{T['text']}", "muted": f"fg:{T['muted']}", "faint": f"fg:{T['faint']}",
        "accent": f"fg:{T['accent']}", "warning": f"fg:{T['warning']}",
        "success": f"fg:{T['success']}", "error": f"fg:{T['error']}",
        "sidebar": f"bg:{T['panel']}",
        "sidebar.title": f"bg:{T['panel']} fg:{T['text']} bold",
        "sidebar.heading": f"bg:{T['panel']} fg:{T['text']} bold",
        "sidebar.text": f"bg:{T['panel']} fg:{T['text']}",
        "sidebar.muted": f"bg:{T['panel']} fg:{T['muted']}",
        "dialog": f"bg:{T['panel2']}",
        "dialog.title": f"bg:{T['panel2']} fg:{T['text']} bold",
        "dialog.muted": f"bg:{T['panel2']} fg:{T['muted']}",
        "dialog.text": f"bg:{T['panel2']} fg:{T['text']}",
        "dialog.meta": f"fg:{T['muted']}",
        "dialog.selected": f"bg:{T['accent']} fg:{T['bg']}",
        "dialog.input": f"bg:{T['panel2']} fg:{T['text']}",
        "completion-menu": f"bg:{T['panel2']} fg:{T['text']}",
        "completion-menu.completion.current": f"bg:{T['accent']} fg:{T['bg']}",
        "completion-menu.meta.completion": f"bg:{T['panel2']} fg:{T['muted']}",
        "completion-menu.meta.completion.current": f"bg:{T['accent']} fg:{T['bg']}",
        "scrollbar.background": f"bg:{T['panel2']}", "scrollbar.button": f"bg:{T['border']}",
    })

    app = Application(
        layout=Layout(
            FloatContainer(
                content=HSplit([DynamicContainer(lambda: home if st.home else session)], style="class:root"),
                floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8, scroll_offset=1)),
                        Float(content=dialog, top=3)],
            ),
            focused_element=st.input_box,
        ),
        key_bindings=kb,
        style=style,
        color_depth=color_depth(),
        full_screen=True,
        refresh_interval=0.1,
    )
    app.ttimeoutlen = 0.05
    app.timeoutlen = 0.1
    st.app = app
    return app


def color_depth():
    from prompt_toolkit.output.color_depth import ColorDepth
    if os.environ.get("PROMPT_TOOLKIT_COLOR_DEPTH"):
        return None
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or os.name == "nt":
        return ColorDepth.TRUE_COLOR
    return None


def fit_left(text, width):
    return text if len(text) <= width else "…" + text[-(width - 1):] if width > 1 else ""


_branch_cache = {"at": 0.0, "value": ""}


def git_branch():
    if time.time() - _branch_cache["at"] > 5:
        _branch_cache["at"] = time.time()
        try:
            head = (Path.cwd() / ".git" / "HEAD").read_text().strip()
            _branch_cache["value"] = head.rsplit("/", 1)[-1] if head.startswith("ref:") else head[:8]
        except OSError:
            _branch_cache["value"] = ""
    return _branch_cache["value"]


def on_enter(buff):
    text = buff.text.strip()
    if not text:
        return False
    if ALIASES.get(text, text) == "/q":
        st.app.exit()
        return False
    if not start_input(text):
        return True
    return False


# --- Config and setup wizard ---

def load_config():
    try:
        return json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config():
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st.config, f, indent=2)
    tmp.replace(path)


def provider_label():
    return PROVIDERS.get(st.config.get("provider", "claude"), PROVIDERS["claude"])[1]


def ask(prompt, default=""):
    hint = f" {DIM}[{default}]{RESET}" if default else ""
    try:
        value = input(f"  {prompt}{hint}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)
    return value or default


def pick(title, options, default=None):
    """Numbered menu of (value, label, description). Enter keeps the default; typing a value also works."""
    print(f"\n  {BOLD}{title}{RESET}")
    for i, (value, label, desc) in enumerate(options, 1):
        mark = f"{GREEN}●{RESET}" if value == default else " "
        print(f"  {mark} {CYAN}{i:>2}{RESET}) {label}  {DIM}{desc}{RESET}")
    default_n = next((str(i) for i, o in enumerate(options, 1) if o[0] == default), "")
    while True:
        choice = ask("choose", default_n)
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1][0]
        if choice and not choice.isdigit():
            return choice
        print(f"  {DIM}type a number from 1 to {len(options)}{RESET}")


def setup_wizard():
    st.config = load_config()
    print("\n" + "\n".join("  " + gradient(line) for line in LOGO) + "\n")
    print(f"  {BOLD}Setup{RESET} {DIM}· saved to {tilde(config_file())} · change anytime: nanocode setup{RESET}")
    if sdk is None:
        print(f"  {YELLOW}claude-agent-sdk isn't installed here, so only OpenRouter and Ollama will work.{RESET}")
    provider = pick("Which provider?", [(k, v[1], v[2]) for k, v in PROVIDERS.items()],
                    st.config.get("provider", "claude" if sdk else "openrouter"))
    if provider not in PROVIDERS:
        provider = "claude"
    st.config["provider"] = provider
    models = st.config.setdefault("models", {})
    current = models.get(provider) or DEFAULT_MODELS.get(provider)

    if provider == "claude":
        print(f"\n  {DIM}Uses your Claude Code login. Not logged in yet? Run 'claude' and type /login.{RESET}")
        models["claude"] = pick("Model", [(k, k, v) for k, v in SDK_MODELS.items()], current)
    elif provider == "openrouter":
        if os.environ.get("OPENROUTER_API_KEY"):
            print(f"\n  {DIM}Using OPENROUTER_API_KEY from your environment.{RESET}")
        else:
            saved = st.config.get("openrouter_api_key", "")
            keep = f" {DIM}[keep {saved[:8]}…{saved[-4:]}]{RESET}" if saved else ""
            print(f"\n  {DIM}Get a key at https://openrouter.ai/keys{RESET}")
            try:
                key = getpass.getpass(f"  OpenRouter API key{keep}: ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                sys.exit(130)
            if key:
                st.config["openrouter_api_key"] = key
            elif not saved:
                print(f"  {YELLOW}No key saved. You can also set OPENROUTER_API_KEY later.{RESET}")
        models["openrouter"] = pick("Model (or type any OpenRouter slug)",
                                    [(k, k, v) for k, v in OPENROUTER_MODELS.items()], current)
    elif provider == "ollama":
        st.config["ollama_url"] = ask("Ollama URL", st.config.get("ollama_url", OLLAMA_DEFAULT_URL)).rstrip("/")
        try:
            installed = ollama_models(st.config["ollama_url"])
        except Exception:
            installed = None
            print(f"\n  {YELLOW}Can't reach Ollama at {st.config['ollama_url']}.{RESET}")
            print(f"  {DIM}Install it from https://ollama.com/download, start it, then: ollama pull qwen3{RESET}")
        if installed:
            options = [(m["name"], m["name"], f"{m['params']} · {m['size_gb']:.1f} GB" +
                        ("" if m["tools"] else " · no tool support")) for m in installed]
            models["ollama"] = pick("Model (needs tool support to edit files and run commands)", options,
                                    current or installed[0]["name"])
        else:
            models["ollama"] = ask("Model name (pull it later with: ollama pull <name>)", current or "qwen3")
        size = ask("Context size in tokens (bigger = slower on small machines)",
                   str(st.config.get("ollama_num_ctx", default_num_ctx())))
        st.config["ollama_num_ctx"] = int(size) if size.isdigit() else default_num_ctx()

    if rea_runtime() is not None:
        st.config["rea"] = pick("REA reverse-engineering tools (loaded only when a task needs them)",
                                [(True, "on", "recommended"), (False, "off", "")],
                                st.config.get("rea", True)) in (True, "on", "yes", "y")
    st.config["permissions"] = pick(
        "Before editing files or running commands",
        [("ask", "ask me first", "recommended"), ("auto", "just do it", "destructive commands still ask")],
        st.config.get("permissions", "ask"))
    if st.config["permissions"] not in ("ask", "auto"):
        st.config["permissions"] = "ask"
    save_config()
    print(f"\n  {GREEN}⏺ Saved.{RESET} {PROVIDERS[provider][1]} · {models[provider]}\n")


def apply_config(provider=None):
    """Sets provider, model and keys from config (env vars win). Returns an error message or None."""
    provider = provider or st.config.get("provider", "claude")
    if provider not in PROVIDERS:
        return f"unknown provider '{provider}' (choose: {', '.join(PROVIDERS)})"
    st.backend = PROVIDERS[provider][0]
    st.model = os.environ.get("MODEL") or st.config.get("models", {}).get(provider) or DEFAULT_MODELS.get(provider)
    st.auto_approve = st.config.get("permissions") == "auto"
    if st.backend == "sdk" and sdk is None:
        return "claude-agent-sdk isn't installed. Reinstall nanocode, or pick another provider: nanocode setup"
    if st.backend == "api":
        st.openrouter_key = os.environ.get("OPENROUTER_API_KEY") or st.config.get("openrouter_api_key")
        if not st.openrouter_key:
            return "no OpenRouter API key. Run: nanocode setup  (or export OPENROUTER_API_KEY=...)"
    if st.backend == "ollama":
        url = os.environ.get("OLLAMA_HOST") or st.config.get("ollama_url") or OLLAMA_DEFAULT_URL
        st.ollama_url = (url if url.startswith("http") else "http://" + url).rstrip("/")
        st.num_ctx = int(st.config.get("ollama_num_ctx") or default_num_ctx())
        refresh_ollama_models()
        if not st.model:
            return "no Ollama model set. Run: nanocode setup"
    return None


# --- CLI ---

def doctor():
    ok = f"{GREEN}✓{RESET}"
    bad = f"{RED}✗{RESET}"
    warn = f"{YELLOW}!{RESET}"
    print(f"{BOLD}nanocode {__version__}{RESET} {DIM}doctor{RESET}\n")
    st.config = load_config()
    print(f"  {ok} Python {platform.python_version()} on {platform.system()} {platform.machine()}")
    print(f"  {ok} home: {tilde(home_dir())}")
    from importlib import metadata
    for name in ("httpx", "rich", "prompt_toolkit"):
        try:
            print(f"  {ok} {name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            print(f"  {bad} {name} missing: reinstall nanocode")
    print(f"  {ok if sdk else warn} claude-agent-sdk {getattr(sdk, '__version__', '') if sdk else 'not installed (Claude provider unavailable)'}")
    rea_info = rea_runtime()
    if rea_info is None:
        print(f"  {warn} REA not installed (optional reverse-engineering tools; needs Node.js 22+, then nanocode update)")
    else:
        enabled = "" if st.config.get("rea", True) is not False else " (turned off: /rea on)"
        print(f"  {ok} REA {rea_info.get('version', '')}{enabled}")
    print(f"  {ok if shutil.which('rg') else warn} ripgrep {'found' if shutil.which('rg') else 'not found (grep uses the slower built-in search)'}")
    st.config = load_config()
    if not st.config.get("provider"):
        print(f"  {warn} not set up yet: run nanocode")
        return 0
    error = apply_config()
    print(f"  {ok} provider: {provider_label()} · model: {st.model}")
    if error:
        print(f"  {bad} {error}")
        return 1
    if st.backend == "api":
        try:
            resp = httpx.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {st.openrouter_key}"},
                             timeout=10)
            print(f"  {ok if resp.status_code == 200 else bad} OpenRouter key {'works' if resp.status_code == 200 else f'rejected ({resp.status_code})'}")
        except httpx.HTTPError as e:
            print(f"  {bad} can't reach OpenRouter: {e}")
    if st.backend == "ollama":
        names = [m["name"] for m in st.ollama_models]
        if not names:
            print(f"  {bad} Ollama not reachable at {st.ollama_url}")
        else:
            model = next((m for m in st.ollama_models if m["name"] == st.model), None)
            print(f"  {ok} Ollama at {st.ollama_url} ({len(names)} models)")
            if model is None:
                print(f"  {bad} model {st.model} not installed: ollama pull {st.model}")
            elif not model["tools"]:
                print(f"  {warn} model {st.model} doesn't support tools; it can't edit files or run commands")
            print(f"  {ok} context: {st.num_ctx} tokens")
    if st.backend == "sdk":
        cli = shutil.which("claude")
        print(f"  {ok if cli else warn} claude CLI {cli or 'not on PATH (the SDK uses its bundled copy)'}")
    return 0


def run_installer(*args):
    """Downloads and runs the latest installer (used by update/uninstall)."""
    windows = os.name == "nt"
    local = home_dir() / "app" / ("install.ps1" if windows else "install.sh")
    if args and local.exists():
        script = local
    else:
        url = f"{RAW_BASE}/{'install.ps1' if windows else 'install.sh'}"
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            print(f"{RED}⏺ couldn't download the installer: {e}{RESET}")
            return 1
        script = Path(tempfile.mkdtemp()) / local.name
        script.write_text(resp.text, encoding="utf-8")
    if windows:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        cmd += [{"--uninstall": "-Uninstall", "--purge": "-Purge"}.get(a, a) for a in args]
    else:
        cmd = ["sh", str(script), *args]
    return subprocess.call(cmd)


async def run_headless(prompt, resume_last=False):
    st.headless = True
    new_session()
    if st.backend == "sdk":
        st.client = await make_client()
    try:
        if resume_last and list_sessions(1):
            await resume_session("1")
        await run_input(prompt)
    finally:
        await stop_all_shells()
        if st.rea:
            await st.rea.close()
        if st.client:
            await st.client.disconnect()
    return 1 if st.last_error else 0


async def run_interactive(resume_last=False):
    app = build_app()
    new_session()

    async def startup():
        try:
            if st.backend == "sdk":
                st.client = await make_client()
            if resume_last:
                if list_sessions(1):
                    await resume_session("1")
                else:
                    out(f"{DIM}no saved chat to continue in this folder{RESET}\n")
        except Exception as e:
            log_error(e)
            out(f"{RED}⏺ {e}{RESET}\n")

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, ctx: log_error(ctx.get("exception") or RuntimeError(ctx.get("message"))))
    st.task_started = time.time()
    st.task = asyncio.ensure_future(startup())
    try:
        await app.run_async()
    finally:
        if st.task and not st.task.done():
            st.task.cancel()
        await stop_all_shells()
        if st.rea:
            await st.rea.close()
        if st.client:
            try:
                await asyncio.wait_for(st.client.disconnect(), 5)
            except Exception:
                pass


def read_piped_stdin():
    """Input piped into `nanocode -p`. Doesn't wait on an idle stdin that's merely left open (IDEs, cron, CI)."""
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        if stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode):
            return sys.stdin.read()
    except (OSError, ValueError):
        pass
    import threading

    chunks, started, done = [], threading.Event(), threading.Event()

    def reader():
        try:
            first = sys.stdin.read(1)
            started.set()
            if first:
                chunks.append(first + sys.stdin.read())
        except (OSError, ValueError):
            pass
        finally:
            started.set()
            done.set()

    threading.Thread(target=reader, daemon=True).start()
    if not started.wait(1.0):
        return ""
    done.wait()
    return "".join(chunks)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="nanocode", description="A small coding agent for the terminal.",
        epilog="commands: setup (provider, key, model) · config · doctor · update · uninstall [--purge]")
    parser.add_argument("command", nargs="?", choices=["setup", "config", "doctor", "update", "uninstall", "version"],
                        help=argparse.SUPPRESS)
    parser.add_argument("-p", "--print", dest="prompt", metavar="PROMPT",
                        help="run one prompt without the UI and print the answer ('-' reads stdin)")
    parser.add_argument("-c", "--continue", dest="resume", action="store_true", help="continue the last chat here")
    parser.add_argument("-y", "--yes", action="store_true", help="allow edits and commands without asking")
    parser.add_argument("-m", "--model", help="model for this run")
    parser.add_argument("--provider", choices=list(PROVIDERS), help="provider for this run")
    parser.add_argument("--purge", action="store_true", help="with uninstall: also delete config, memory and skills")
    parser.add_argument("-v", "--version", action="store_true", help="print the version")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version or args.command == "version":
        print(f"nanocode {__version__}")
        return 0
    if args.command == "update":
        return run_installer()
    if args.command == "uninstall":
        return run_installer("--uninstall", *(["--purge"] if args.purge else []))
    migrate_legacy()
    if args.command == "doctor":
        return doctor()
    if args.command == "setup":
        setup_wizard()
        return 0
    st.config = load_config()
    if args.command == "config":
        text = show_config()
        print(text if sys.stdout.isatty() else ANSI_RE.sub("", text))
        return 0

    prompt = args.prompt
    if prompt is not None:
        if prompt == "-":
            prompt = sys.stdin.read()
        else:
            piped = read_piped_stdin()
            if piped.strip():
                prompt = f"{prompt}\n\n<stdin>\n{cap(piped, 100_000)}\n</stdin>"
        if not prompt.strip():
            print("nothing to do: the prompt is empty", file=sys.stderr)
            return 2
    elif not st.config.get("provider"):
        if not sys.stdin.isatty():
            print("nanocode isn't set up yet: run `nanocode setup` in a terminal", file=sys.stderr)
            return 2
        setup_wizard()

    if args.model:
        os.environ["MODEL"] = args.model
    error = apply_config(args.provider)
    if error:
        print(f"{RED}⏺ {error}{RESET}", file=sys.stderr)
        return 1
    if args.yes:
        st.auto_approve = True
    try:
        if prompt is not None:
            return asyncio.run(run_headless(prompt, args.resume))
        asyncio.run(run_interactive(args.resume))
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
