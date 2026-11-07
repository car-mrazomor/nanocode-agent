#!/usr/bin/env python3
"""nanocode - minimal claude code alternative"""

class ClientErr(Exception):
    pass

import glob as globlib
import io
import json
import os
import re
import subprocess
import sys
import shutil
import time
import httpx
from rich.console import Console
from rich.markdown import Markdown
from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk import (AssistantMessage,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
    StreamEvent,
    UserMessage
)
from claude_agent_sdk import (
    ClaudeSDKError,
    ResultError,
)
import asyncio
from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, completion_is_selected, has_completions
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window, FloatContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import TextArea

# ANSI colors
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, YELLOW, RED = (
    "\033[34m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
)
USER_BG = "\033[48;5;237m"  # dark gray background for your messages


MODEL = None
CLIENT = None
TASK = None
OUTPUT = ""  # everything shown in the output area (ANSI colors allowed)

BACKEND = None  # "sdk" (Claude Code SDK / Max sub) or "api" (OpenRouter)
OPENROUTER_KEY = None
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MESSAGES = []  # conversation history for API mode (the SDK keeps its own)

LIVE_MD = None  # raw markdown of the reply currently streaming (None = nothing streaming)
LIVE = ""  # LIVE_MD rendered to ANSI, shown under OUTPUT until the block is done

SCROLL = 0  # how many lines the output is scrolled up from the bottom (PageUp/PageDown)
TASK_STARTED = 0.0  # time.time() when the current reply started (for the spinner)
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

PENDING_PERMISSION = None  # asyncio.Future waiting for y/n/a, or None
ALWAYS_ALLOW = set()  # tools the user said "always" to (this session only)

SESSION_USAGE = {"input": 0, "output": 0, "cost": 0.0}  # totals for /cost

HISTORY_FILE = os.path.expanduser("~/.nanocode_history")

COMMANDS = {
    "/help": "show commands and keys",
    "/model": "list models, or switch: /model <name> (Tab completes)",
    "/cost": "tokens and cost for this session",
    "/resume": "list saved chats, or /resume <n> to continue one",
    "/memory": "show what's in memory (NANOCODE.md files)",
    "/skills": "list available skills (run one with /<skill-name>)",
    "/c": "clear conversation",
    "/q": "quit",
    "/exit": "quit",
}

KEYS_HELP = [
    ("Enter", "send"),
    ("Alt+Enter", "new line"),
    ("Esc", "stop current reply"),
    ("↑ / ↓", "input history"),
    ("PageUp / PageDown", "scroll output"),
    ("Tab", "complete / and @file"),
    ("Ctrl+C / Ctrl+D", "quit"),
]


def out(text="", end="\n"):
    """Use this instead of print(): appends to the output area."""
    global OUTPUT
    OUTPUT += text + end
    if app.is_running:
        app.invalidate()


# --- Full-screen layout: output on top, input + model line pinned at bottom ---

class NanoCompleter(Completer):
    """'/' at the start suggests commands, '@' suggests file paths."""
    
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/resume "):
            typed = text[len("/resume "):]
            for i, s in enumerate(list_sessions(), 1):
                if typed in str(i) or typed.lower() in s["title"].lower():
                    yield Completion(str(i), start_position=-len(typed), display=f"{i}. {s['title']}",
                                     display_meta=f"{s['model']} · {time_ago(s['updated'])}")
            return
        if text.startswith("/model "):
            typed = text[len("/model "):]
            for name, desc in model_choices().items():
                if typed in name:  # match anywhere, so "glm" finds z-ai/glm-5.3
                    yield Completion(name, start_position=-len(typed), display_meta=desc)
            return
        if text.startswith("/") and " " not in text:
            for cmd, desc in COMMANDS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)
            for name, info in list_skills().items():
                if f"/{name}".startswith(text) and f"/{name}" not in COMMANDS:
                    yield Completion(f"/{name}", start_position=-len(text), display_meta=f"skill · {info['description'][:60]}")
            return

        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            prefix = word[1:]
            matches = sorted(globlib.glob(prefix + "*"))[:50]
            for path in matches:
                shown = path + "/" if os.path.isdir(path) else path
                yield Completion("@" + shown, start_position=-len(word), display=shown)


def visible_output():
    """OUTPUT + LIVE, cut off at the bottom by SCROLL lines (that's how scrolling works)."""
    text = OUTPUT + LIVE
    if SCROLL:
        lines = text.split("\n")
        text = "\n".join(lines[: max(1, len(lines) - SCROLL)])
    return text


output_window = Window(
    FormattedTextControl(
        lambda: ANSI(visible_output()),
        get_cursor_position=lambda: Point(x=0, y=visible_output().count("\n")),  # keeps view at the end
        show_cursor=False,
    ),
    wrap_lines=True,
)
input_box = TextArea(
    prompt="❯ ",
    multiline=True,
    height=Dimension(min=1, max=10),  # grows with multi-line input
    dont_extend_height=True,
    completer=NanoCompleter(),
    complete_while_typing=True,
    history=FileHistory(HISTORY_FILE),
)


def status_text():
    parts = []
    if PENDING_PERMISSION is not None:
        parts.append(("#e5c07b", " waiting for permission: y / n / a "))
    elif TASK and not TASK.done():
        frame = SPINNER[int(time.time() * 10) % len(SPINNER)]
        parts.append(("#61afef", f" {frame} working… {time.time() - TASK_STARTED:.0f}s "))
        parts.append(("#888888", "(esc to stop) "))
    if SCROLL:
        parts.append(("#888888", f" ↑ scrolled {SCROLL} lines (PageDown) "))
    routed = f" → {ROUTED_MODEL}" if BACKEND == "api" and ROUTED_MODEL and MODEL.startswith("openrouter/") else ""
    parts.append(("#888888", f" model: {MODEL}{routed}"))
    return parts


status = Window(FormattedTextControl(status_text), height=1)

kb = KeyBindings()
waiting_permission = Condition(lambda: PENDING_PERMISSION is not None)


@kb.add("c-c")
@kb.add("c-d")
def _(event):
    event.app.exit()


@kb.add("enter", filter=has_completions & completion_is_selected)
def _(event):
    """Enter on a highlighted suggestion just picks it (doesn't send)."""
    buff = event.current_buffer
    buff.apply_completion(buff.complete_state.current_completion)


@kb.add("enter", filter=~(has_completions & completion_is_selected) & ~waiting_permission)
def _(event):
    event.current_buffer.validate_and_handle()  # calls on_enter


@kb.add("escape", "enter")  # Alt+Enter
def _(event):
    event.current_buffer.insert_text("\n")


@kb.add("escape")
def _(event):
    buff = event.current_buffer
    if PENDING_PERMISSION is not None:
        answer_permission("n")
    elif buff.complete_state:
        buff.cancel_completion()
    elif TASK and not TASK.done():
        if BACKEND == "sdk":
            asyncio.create_task(CLIENT.interrupt())  # SDK finishes the turn cleanly
        else:
            TASK.cancel()
        out(f"  {RED}⎿  Interrupted{RESET}")


@kb.add("y", filter=waiting_permission)
@kb.add("n", filter=waiting_permission)
@kb.add("a", filter=waiting_permission)
def _(event):
    answer_permission(event.key_sequence[0].data)


@kb.add("pageup")
def _(event):
    global SCROLL
    page = output_window.render_info.window_height - 2 if output_window.render_info else 10
    total = (OUTPUT + LIVE).count("\n")
    SCROLL = min(SCROLL + page, total)


@kb.add("pagedown")
def _(event):
    global SCROLL
    page = output_window.render_info.window_height - 2 if output_window.render_info else 10
    SCROLL = max(0, SCROLL - page)


app = Application(
    layout=Layout(
        FloatContainer(
            content=HSplit([output_window, Window(height=1, char="─", style="#444444"), input_box, status]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8, scroll_offset=1))],
        ),
        focused_element=input_box,
    ),
    key_bindings=kb,
    full_screen=True,
    refresh_interval=0.1,  # redraw for the spinner
)
app.ttimeoutlen = 0.05  # make a single Esc react fast
app.timeoutlen = 0.1  # don't wait long to see if Esc is the start of Alt+key


# --- Permission prompt for risky tools (bash, write, edit) ---


def answer_permission(key):
    global PENDING_PERMISSION
    if PENDING_PERMISSION is not None and not PENDING_PERMISSION.done():
        PENDING_PERMISSION.set_result(key)


async def ask_permission(tool_name, preview):
    """Ask y/n/a in the UI. Returns True if allowed."""
    global PENDING_PERMISSION
    if tool_name in ALWAYS_ALLOW:
        return True
    stream_end()
    out(f"\n{YELLOW}? Allow {BOLD}{tool_name}{RESET}{YELLOW}({preview[:80]}){RESET}  {DIM}[y]es / [n]o / [a]lways{RESET}")
    PENDING_PERMISSION = asyncio.get_running_loop().create_future()
    try:
        answer = await PENDING_PERMISSION
    finally:
        PENDING_PERMISSION = None
    if answer == "a":
        ALWAYS_ALLOW.add(tool_name)
    allowed = answer in ("y", "a")
    out(f"  {DIM}⎿  {'allowed' if allowed else 'denied'}{RESET}")
    return allowed


DENIED = {"content": [{"type": "text", "text": "error: user denied permission for this tool call"}]}


# --- Tool implementations ---
# --- Tool definitions: (description, schema, function) ---


@tool("read", "Read file with line numbers (file path, not directory)", {"path": str})
async def read(args):
    lines = open(args["path"]).readlines()
    offset = args.get("offset", 0)
    limit = args.get("limit", len(lines))
    selected = lines[offset : offset + limit]
    
    return {"content": [{"type": "text", "text": "".join(f"{offset + idx + 1:4}| {line}" for idx, line in enumerate(selected))}]}


@tool("write", "Write content to file", {"path": str, "content": str})
async def write(args):
    if not await ask_permission("write", args.get("path", "")):
        return DENIED
    with open(args["path"], "w") as f:
        f.write(args["content"])
    
    return {"content": [{"type": "text", "text": "OK"}]}


@tool("edit", "Replace old with new in file (old must be unique unless all=true)", {"old": str, "new": str, "all": str})
async def edit(args):
    if not await ask_permission("edit", args.get("path", "")):
        return DENIED
    text = open(args["path"]).read()
    old, new = args["old"], args["new"]
    if old not in text:
        return {"content": [{"type": "text", "text": "error: old_string not found"}]}
    count = text.count(old)
    if not args.get("all") and count > 1:
        return {"content": [{"type": "text", "text": f"error: old_string appears {count} times, must be unique (use all=true)"}]}
    replacement = (
        text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    )
    with open(args["path"], "w") as f:
        f.write(replacement)
    
    return {"content": [{"type": "text", "text": "OK"}]}


@tool("glob", "Find files by pattern, sorted by mtime", {"pat": str | None, "path": str | None})
async def glob(args):
    pattern = (args.get("path", ".") + "/" + args["pat"]).replace("//", "/")
    files = globlib.glob(pattern, recursive=True)
    files = sorted(
        files,
        key=lambda f: os.path.getmtime(f) if os.path.isfile(f) else 0,
        reverse=True,
    )

    return {"content": [{"type": "text", "text": "\n".join(files) or "none"}]}


@tool("grep", "Search files for regex pattern", {"pat": str | None, "path": str | None})
async def grep(args):
    pattern = re.compile(args["pat"])
    hits = []
    for filepath in globlib.glob(args.get("path", ".") + "/**", recursive=True):
        try:
            for line_num, line in enumerate(open(filepath), 1):
                if pattern.search(line):
                    hits.append(f"{filepath}:{line_num}:{line.rstrip()}")
        except Exception:
            pass
    
    return {"content": [{"type": "text", "text": "\n".join(hits[:50]) or "none"}]}


@tool("bash", "Run shell command", {"cmd": str})
async def bash(args):
    if not await ask_permission("bash", args.get("cmd", "")):
        return DENIED
    proc = await asyncio.create_subprocess_shell(
        args["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    output_lines = []

    async def pump():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            out(f"  {DIM}│ {line.rstrip()}{RESET}")
            output_lines.append(line)
        await proc.wait()

    try:
        await asyncio.wait_for(pump(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        output_lines.append("\n(timed out after 30s)")
    
    return {"content": [{"type": "text", "text": "".join(output_lines).strip() or "(empty)"}]}


# --- Memory: files loaded into every chat ---
#   global:  ~/.config/nanocode/NANOCODE.md   (about you + preferences, all projects; made by install.sh)
#   project: ./.nanocode/memory.md            (this project, never committed)
# NANOCODE.md, CLAUDE.md and AGENTS.md in the project root are read too (read-only, team-shared).

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "nanocode")
LOCAL_DIR = os.path.join(os.getcwd(), ".nanocode")
PROJECT_MEMORY = os.path.join(LOCAL_DIR, "memory.md")
GLOBAL_MEMORY = os.path.join(CONFIG_DIR, "NANOCODE.md")
EXTRA_MEMORY = [os.path.join(os.getcwd(), name) for name in ("NANOCODE.md", "CLAUDE.md", "AGENTS.md")]


def ensure_local_dir():
    """Create .nanocode/ with its own .gitignore, so memory and chats are never pushed in any repo."""
    os.makedirs(LOCAL_DIR, exist_ok=True)
    ignore = os.path.join(LOCAL_DIR, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w") as f:
            f.write("# nanocode: keep private stuff out of git (skills/ can be committed)\nsessions/\nmemory.md\n")


def user_name():
    """Your name from memory ('- Name: Ivan' or 'my name is Ivan'), for the greeting."""
    for _, _, text in memory_files():
        match = re.search(r"(?im)^\s*[-*]?\s*(?:my\s+)?name\s*(?::|is)\s*([^\n.,;]+)", text)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def memory_files():
    """(label, path, text) for every memory file that exists."""
    found = []
    for label, path in [("global", GLOBAL_MEMORY), ("project", PROJECT_MEMORY)] + [("shared, read-only", p) for p in EXTRA_MEMORY]:
        if os.path.isfile(path):
            try:
                text = open(path, errors="replace").read().strip()
            except OSError:
                continue
            if text:
                found.append((label, path, text))
    return found


def add_memory(fact, scope="project"):
    path = GLOBAL_MEMORY if scope == "global" else PROJECT_MEMORY
    if scope != "global":
        ensure_local_dir()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.isfile(path)
    with open(path, "a") as f:
        if new_file:
            f.write("# Memory\n\n")
        f.write(f"- {fact.strip()}\n")
    return path


@tool(
    "remember",
    "Save a fact to long-term memory so it is known in future chats. Use when the user states a lasting "
    "preference, convention, or project fact (e.g. 'we use pnpm', 'never edit generated/'). "
    "scope: 'project' (this repo, default) or 'global' (all projects).",
    {"fact": str, "scope": str},
)
async def remember(args):
    path = add_memory(args["fact"], args.get("scope") or "project")
    return {"content": [{"type": "text", "text": f"saved to {path}"}]}


# --- Skills: folders with a SKILL.md, loaded only when needed ---
#   ./.nanocode/skills/<name>/SKILL.md   ./.claude/skills/<name>/SKILL.md      (project)
#   ~/.config/nanocode/skills/<name>/SKILL.md   ~/.claude/skills/<name>/SKILL.md  (global)
# SKILL.md starts with frontmatter:  ---\nname: x\ndescription: when to use it\n---

SKILL_DIRS = [  # earlier wins when two skills have the same name
    os.path.join(os.getcwd(), ".nanocode", "skills"),
    os.path.join(os.getcwd(), ".claude", "skills"),
    os.path.join(CONFIG_DIR, "skills"),
    os.path.expanduser("~/.claude/skills"),
]


def parse_skill(path):
    """Returns (frontmatter dict, body) of a SKILL.md."""
    text = open(path, errors="replace").read()
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
    """name -> {description, path, dir}"""
    skills = {}
    for root in SKILL_DIRS:
        for path in sorted(globlib.glob(os.path.join(root, "*", "SKILL.md"))):
            try:
                meta, _ = parse_skill(path)
            except OSError:
                continue
            name = meta.get("name") or os.path.basename(os.path.dirname(path))
            if name not in skills:
                skills[name] = {"description": meta.get("description", ""), "path": path, "dir": os.path.dirname(path)}
    return skills


def load_skill(name):
    """Full skill text for the model, or None."""
    info = list_skills().get(name)
    if not info:
        return None
    _, body = parse_skill(info["path"])
    return f'<skill name="{name}">\n{body}\n\n(This skill\'s files are in {info["dir"]} - use read/bash on them if it mentions any.)\n</skill>'


@tool("skill", "Load a skill's full instructions by name. Do this before a task that matches a skill's description.", {"name": str})
async def skill(args):
    text = load_skill(args["name"])
    if text is None:
        return {"content": [{"type": "text", "text": f"error: no skill named '{args['name']}'. Available: {', '.join(list_skills()) or 'none'}"}]}
    return {"content": [{"type": "text", "text": text}]}


def build_system_prompt():
    """Base prompt + memory + skill list. Rebuilt for every API request and every SDK connect."""
    parts = [f"Concise coding assistant. cwd: {os.getcwd()}"]

    memories = memory_files()
    if memories:
        parts.append("# Memory\nInstructions and facts saved by the user. Follow them.")
        for label, path, text in memories:
            parts.append(f'<memory source="{path}" scope="{label}">\n{text}\n</memory>')
    parts.append("When the user tells you a lasting preference or project fact, save it with the `remember` tool.")

    skills = list_skills()
    if skills:
        lines = ["# Skills\nLoad a skill with the `skill` tool before doing a task that matches its description:"]
        lines += [f"- {name}: {info['description'][:200]}" for name, info in skills.items()]
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


ALL_TOOLS = [read, write, edit, glob, grep, bash, remember, skill]


def make_scheme(system_prompt, model, resume=None):
    nano = create_sdk_mcp_server(name="nano", version="1.0.0", tools=ALL_TOOLS)
    options = ClaudeAgentOptions(
        mcp_servers={"nano": nano},
        model=model,
        allowed_tools=[f"mcp__nano__{t.name}" for t in ALL_TOOLS],
        permission_mode='acceptEdits',
        disallowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite"],
        system_prompt=system_prompt,
        include_partial_messages=True,  # needed for word-by-word streaming
        resume=resume,  # Claude Code session id to continue (/resume), or None for a new one
    )

    return options


async def make_client(model, resume=None):
    system_prompt = build_system_prompt()
    try:
        client = ClaudeSDKClient(options=make_scheme(system_prompt=system_prompt, model=model, resume=resume))
        await client.connect()
    except ClaudeSDKError:
        raise ClientErr()

    return client


def separator():
    return f"{DIM}{'─' * max(os.get_terminal_size().columns, 80)}{RESET}"


def render_markdown(text):
    """Render markdown to an ANSI string with rich (headings, lists, code blocks, ...)."""
    width = app.output.get_size().columns - 2 if app.is_running else 80
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="256", width=width).print(Markdown(text))
    return buf.getvalue().rstrip("\n")


# --- Live streaming: text grows in LIVE, then gets committed to OUTPUT ---


def stream_start():
    global LIVE_MD, LIVE
    LIVE_MD, LIVE = "", ""


def stream_text(chunk):
    global LIVE_MD, LIVE
    if LIVE_MD is None:
        stream_start()
    LIVE_MD += chunk
    LIVE = f"\n{CYAN}⏺{RESET} {render_markdown(LIVE_MD)}"  # re-render so markdown stays formatted
    if app.is_running:
        app.invalidate()


def stream_end():
    global LIVE_MD, LIVE
    if LIVE_MD is None:
        return
    final = LIVE
    LIVE_MD, LIVE = None, ""
    if final:
        out(final)


def fmt_tokens(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def show_usage(usage, cost, seconds):
    """Dim line after each reply: tokens in/out, cost (if known), time. Also adds to session totals."""
    tokens_in = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    tokens_out = usage.get("output_tokens") or 0
    SESSION_USAGE["input"] += tokens_in
    SESSION_USAGE["output"] += tokens_out
    SESSION_USAGE["cost"] += cost or 0
    parts = [f"{fmt_tokens(tokens_in)} in", f"{fmt_tokens(tokens_out)} out"]
    if cost:
        parts.append(f"${cost:.4f}")
    parts.append(f"{seconds:.1f}s")
    out(f"\n  {DIM}{' · '.join(parts)}{RESET}")


async def handle(client, user_input):
    while True:
        finish = False
        await client.query(user_input)
        async for msg in client.receive_response():
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if ev.get("type") == "content_block_start" and ev["content_block"].get("type") == "text":
                    stream_start()
                elif ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    stream_text(ev["delta"]["text"])
                elif ev.get("type") == "content_block_stop":
                    stream_end()
            
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        tool_name = block.name.removeprefix("mcp__nano__")
                        tool_args = block.input
                        arg_preview = str(list(tool_args.values())[0])[:50].removeprefix("select:mcp__nano__")
                        out(
                            f"\n{GREEN}⏺ {tool_name.capitalize()}{RESET}({DIM}{arg_preview}{RESET})"
                        )

            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        content = block.content
                        text = content if isinstance(content, str) else "".join(
                            c.get("text", "") for c in (content or []))
                        lines = text.split("\n")
                        preview = lines[0][:60]
                        if len(lines) > 1:
                            preview += f" ... +{len(lines) - 1} lines"
                        out(f"  {DIM}⎿  {preview}{RESET}")

            elif isinstance(msg, ResultMessage):
                try:
                    stream_end()  # text was already streamed, just close it
                    show_usage(msg.usage or {}, msg.total_cost_usd, msg.duration_ms / 1000)
                    SESSION["sdk_session_id"] = msg.session_id  # needed to /resume this chat later
                    finish = True
                except ResultError:
                    return

        if finish:
            break

    out()


# --- API mode (OpenRouter /chat/completions: works with every model, incl. auto routers) ---

API_TOOLS = {t.name: t for t in ALL_TOOLS}
JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}

# Suggested OpenRouter models for /model (Tab completes). Any other OpenRouter slug works too.
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

# Claude Code SDK accepts these aliases or full Claude model IDs
SDK_MODELS = {
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
    "claude-fable-5-1": "Fable 5.1",
    "opus": "alias, latest Opus",
    "sonnet": "alias, latest Sonnet",
    "haiku": "alias, latest Haiku",
}

ROUTED_MODEL = None  # model an auto router actually picked for the last reply


def model_choices():
    return SDK_MODELS if BACKEND == "sdk" else OPENROUTER_MODELS


def make_api_schema():
    """Turn the @tool definitions into OpenAI-style function schemas."""
    result = []
    for t in API_TOOLS.values():
        properties = {name: {"type": JSON_TYPES.get(typ, "string")} for name, typ in t.input_schema.items()}
        result.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {"type": "object", "properties": properties, "required": list(properties)},
            },
        })
    return result


async def run_api_tool(name, args):
    try:
        result = await API_TOOLS[name].handler(args)
        return "".join(c.get("text", "") for c in result["content"])
    except Exception as err:
        return f"error: {err}"


async def stream_api(system_prompt):
    """One streamed /chat/completions call. Streams text to the screen.
    Returns (assistant message, usage)."""
    global ROUTED_MODEL
    text = ""
    tool_calls = {}  # index -> {"id", "name", "arguments"}
    usage = {}
    async with httpx.AsyncClient(timeout=None) as http:
        async with http.stream(
            "POST",
            API_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "X-Title": "nanocode",
            },
            json={
                "model": MODEL,
                "max_tokens": 8192,
                "messages": [{"role": "system", "content": system_prompt}] + MESSAGES,
                "tools": make_api_schema(),
                "stream": True,
                "usage": {"include": True},  # OpenRouter: send tokens + cost in the last chunk
            },
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                raise Exception(f"API {resp.status_code}: {body[:300]}")

            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # skips ": OPENROUTER PROCESSING" keep-alives too
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                chunk = json.loads(data)
                if "error" in chunk:
                    raise Exception(chunk["error"].get("message", chunk["error"]))
                if chunk.get("model"):
                    ROUTED_MODEL = chunk["model"]
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

    stream_end()
    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = [
            {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}
            for _, tc in sorted(tool_calls.items())
        ]
    return message, usage


async def handle_api(user_input):
    """Agentic loop for API mode: keep calling the API until no more tool calls."""
    system_prompt = build_system_prompt()  # picks up memory/skill changes right away
    start_len = len(MESSAGES)  # to roll back the whole turn if it gets interrupted
    started = time.time()
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
    MESSAGES.append({"role": "user", "content": user_input})

    try:
        await api_turn(system_prompt, total_usage)
    except asyncio.CancelledError:
        del MESSAGES[start_len:]  # half-finished turn would break the next request
        raise

    show_usage(total_usage, total_usage["cost"], time.time() - started)
    if ROUTED_MODEL and MODEL.startswith("openrouter/"):
        out(f"  {DIM}routed to {ROUTED_MODEL}{RESET}")
    out()


async def api_turn(system_prompt, total_usage):
    while True:
        message, usage = await stream_api(system_prompt)
        total_usage["input_tokens"] += usage.get("prompt_tokens") or 0
        total_usage["output_tokens"] += usage.get("completion_tokens") or 0
        total_usage["cost"] += usage.get("cost") or 0
        MESSAGES.append(message)

        for call in message.get("tool_calls", []):
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"])
            except json.JSONDecodeError:
                args = None

            if args is None:
                result = "error: tool arguments were not valid JSON"
            else:
                arg_preview = str(next(iter(args.values()), ""))[:50]
                out(f"\n{GREEN}⏺ {name.capitalize()}{RESET}({DIM}{arg_preview}{RESET})")
                result = await run_api_tool(name, args)
                lines = result.split("\n")
                preview = lines[0][:60]
                if len(lines) > 1:
                    preview += f" ... +{len(lines) - 1} lines"
                out(f"  {DIM}⎿  {preview}{RESET}")

            MESSAGES.append({"role": "tool", "tool_call_id": call["id"], "content": result})

        if not message.get("tool_calls"):
            break


def expand_mentions(user_input):
    """Attach the contents of every @file in the message."""
    attached = []
    for path in dict.fromkeys(re.findall(r"@(\S+)", user_input)):
        if os.path.isfile(path):
            try:
                content = open(path, errors="replace").read(100_000)
            except OSError:
                continue
            attached.append(f'<file path="{path}">\n{content}\n</file>')
            out(f"  {DIM}⎿  attached {path}{RESET}")
    if not attached:
        return user_input
    return user_input + "\n\n" + "\n\n".join(attached)


def show_help():
    lines = [f"{BOLD}Commands{RESET}"]
    lines += [f"  {CYAN}{cmd:<8}{RESET} {DIM}{desc}{RESET}" for cmd, desc in COMMANDS.items()]
    lines += ["", f"{BOLD}Keys{RESET}"]
    lines += [f"  {CYAN}{key:<18}{RESET} {DIM}{desc}{RESET}" for key, desc in KEYS_HELP]
    lines += ["", f"  {DIM}@path in a message attaches that file{RESET}"]
    lines += [f"  {DIM}# note   saves the note to project memory (.nanocode/memory.md, never committed){RESET}"]
    lines += [f"  {DIM}/<skill> runs a skill, e.g. /review this file{RESET}"]
    out("\n".join(lines) + "\n")


def show_cost():
    u = SESSION_USAGE
    cost = f" · ${u['cost']:.4f}" if u["cost"] else ""
    out(f"{DIM}session: {fmt_tokens(u['input'])} in · {fmt_tokens(u['output'])} out{cost}{RESET}\n")


# --- Saved chats: .nanocode/sessions/<id>.json in the project folder (gitignored) ---

SESSIONS_DIR = os.path.join(os.getcwd(), ".nanocode", "sessions")
SESSION = {}  # current chat: id, title, created, sdk_session_id


def new_session():
    global SESSION
    SESSION = {"id": time.strftime("%Y%m%d-%H%M%S"), "title": None, "created": time.time(), "sdk_session_id": None}


def save_session():
    """Write the current chat to disk. Called after every reply."""
    if not SESSION.get("title"):
        return  # nothing said yet
    ensure_local_dir()
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    data = {
        **SESSION,
        "backend": BACKEND,
        "model": MODEL,
        "updated": time.time(),
        "messages": MESSAGES if BACKEND == "api" else [],  # SDK keeps its own history, we keep its id
        "screen": OUTPUT,  # what was on screen, shown again on resume
    }
    path = os.path.join(SESSIONS_DIR, f"{SESSION['id']}.json")
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)  # atomic: never leaves a half-written file


def list_sessions():
    """Saved chats for the current backend, newest first."""
    sessions = []
    for path in globlib.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            data = json.load(open(path))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("backend") == BACKEND:
            sessions.append(data)
    return sorted(sessions, key=lambda s: s.get("updated", 0), reverse=True)[:20]


def time_ago(ts):
    secs = time.time() - ts
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


async def resume_session(arg):
    global CLIENT, MODEL, OUTPUT, SESSION
    sessions = list_sessions()
    if not arg:
        if not sessions:
            out(f"{DIM}no saved chats yet{RESET}\n")
            return
        lines = [f"{BOLD}Saved chats{RESET} {DIM}(/resume <n>){RESET}"]
        for i, s in enumerate(sessions, 1):
            lines.append(f"  {CYAN}{i:>2}{RESET}. {s['title']}  {DIM}{s['model']} · {time_ago(s['updated'])}{RESET}")
        out("\n".join(lines) + "\n")
        return

    match = None
    if arg.isdigit() and 1 <= int(arg) <= len(sessions):
        match = sessions[int(arg) - 1]
    else:
        match = next((s for s in sessions if s["id"] == arg), None)
    if not match:
        out(f"{RED}⏺ no saved chat '{arg}' (try /resume to list them){RESET}\n")
        return

    MODEL = match["model"]
    if BACKEND == "sdk":
        if not match.get("sdk_session_id"):
            out(f"{RED}⏺ that chat has no Claude Code session id, can't resume it{RESET}\n")
            return
        await CLIENT.disconnect()
        CLIENT = await make_client(MODEL, resume=match["sdk_session_id"])
    else:
        MESSAGES[:] = match["messages"]

    SESSION = {k: match[k] for k in ("id", "title", "created", "sdk_session_id")}
    SESSION_USAGE.update({"input": 0, "output": 0, "cost": 0.0})
    OUTPUT = match.get("screen", "")
    out(f"{GREEN}⏺ Resumed:{RESET} {match['title']} {DIM}({time_ago(match['updated'])}){RESET}\n")


async def refresh_sdk_prompt():
    """The SDK fixes the system prompt when it connects, so reconnect (same chat) to apply new memory now."""
    global CLIENT
    if BACKEND != "sdk":
        return  # API mode rebuilds the prompt on every request anyway
    await CLIENT.disconnect()
    CLIENT = await make_client(MODEL, resume=SESSION.get("sdk_session_id"))


def show_memory():
    files = memory_files()
    lines = [f"{BOLD}Memory{RESET} {DIM}(loaded into every chat){RESET}"]
    if not files:
        lines.append(f"  {DIM}empty: type '# something to remember' or ask me to remember it{RESET}")
    for label, path, text in files:
        lines.append(f"\n  {CYAN}{path}{RESET} {DIM}({label}){RESET}")
        lines += [f"  {DIM}│{RESET} {line}" for line in text.splitlines()[:40]]
    lines.append(f"\n  {DIM}edit: {PROJECT_MEMORY} (project) · {GLOBAL_MEMORY} (global){RESET}")
    out("\n".join(lines) + "\n")


def show_skills():
    skills = list_skills()
    lines = [f"{BOLD}Skills{RESET} {DIM}(Claude loads them when needed, or run /<name>){RESET}"]
    if not skills:
        lines.append(f"  {DIM}none yet - add .nanocode/skills/<name>/SKILL.md with name + description frontmatter{RESET}")
    for name, info in skills.items():
        where = os.path.relpath(info["path"]) if info["path"].startswith(os.getcwd()) else info["path"].replace(os.path.expanduser("~"), "~")
        lines.append(f"  {CYAN}/{name:<16}{RESET} {info['description'][:70]}  {DIM}{where}{RESET}")
    out("\n".join(lines) + "\n")


async def run_input(user_input):
    """Runs one line of user input (command or message) in the background."""
    global MODEL, CLIENT, OUTPUT
    try:
        if user_input.startswith("#") and len(user_input) > 1:
            path = add_memory(user_input.lstrip("#").strip())
            out(f"{GREEN}⏺ Remembered{RESET} {DIM}→ {path}{RESET}\n")
            await refresh_sdk_prompt()
        elif user_input == "/memory":
            show_memory()
        elif user_input == "/skills":
            show_skills()
        elif user_input.startswith("/resume"):
            parts = user_input.split(maxsplit=1)
            await resume_session(parts[1] if len(parts) == 2 else None)
        elif user_input == "/help":
            show_help()
        elif user_input == "/cost":
            show_cost()
        elif user_input.startswith("/model"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                MODEL = parts[1]
                if BACKEND == "sdk":
                    await CLIENT.set_model(MODEL)  # API mode just reads MODEL on the next call
                out(f"{DIM}⏺ model changed to {MODEL}{RESET}\n")
            else:
                lines = [f"{BOLD}Models{RESET} {DIM}(input / output per 1M tokens){RESET}"]
                for name, desc in model_choices().items():
                    mark = f"{GREEN}●{RESET}" if name == MODEL else " "
                    lines.append(f" {mark} {CYAN}{name:<30}{RESET} {DIM}{desc}{RESET}")
                if BACKEND == "api":
                    lines.append(f"\n  {DIM}any slug from openrouter.ai/models works too{RESET}")
                out("\n".join(lines) + "\n")
        elif user_input == "/c":
            if BACKEND == "sdk":
                await CLIENT.disconnect()
                CLIENT = await make_client(MODEL)
            else:
                MESSAGES.clear()
            new_session()  # the old chat stays saved, /resume brings it back
            OUTPUT = ""
            out(f"{GREEN}⏺ Cleared conversation{RESET}\n")
        else:
            width = app.output.get_size().columns
            for i, text_line in enumerate(user_input.split("\n")):
                prefix = " ❯ " if i == 0 else "   "
                line = f"{prefix}{text_line}".ljust(width)  # pad so the gray bar spans the full width
                out(f"{USER_BG}{line}{RESET}")
            message = expand_mentions(user_input)
            skill_name = user_input[1:].split(maxsplit=1)[0] if user_input.startswith("/") and len(user_input) > 1 else None
            if skill_name and skill_name in list_skills():  # /<skill> [what to do]
                task = user_input[len(skill_name) + 1:].strip()
                message = load_skill(skill_name) + "\n\n" + (expand_mentions(task) if task else f"Run the {skill_name} skill.")
                out(f"  {DIM}⎿  loaded skill {skill_name}{RESET}")
            if not SESSION.get("title"):
                SESSION["title"] = user_input.split("\n")[0][:60]
            try:
                if BACKEND == "sdk":
                    await handle(CLIENT, message)
                else:
                    await handle_api(message)
            finally:
                save_session()  # also saves when interrupted or on error
    except asyncio.CancelledError:
        stream_end()  # keep whatever text already streamed
        out()
    except Exception as err:
        out(f"{RED}⏺ Error: {err}{RESET}\n")


def on_enter(buff):
    global TASK, TASK_STARTED, SCROLL
    user_input = buff.text.strip()
    if not user_input:
        return False
    if user_input in ("/q", "/exit"):
        app.exit()
        return False
    if TASK and not TASK.done():
        out(f"{DIM}(still working, wait){RESET}")
        return False
    SCROLL = 0  # jump back to the bottom when you send something
    TASK_STARTED = time.time()
    TASK = asyncio.create_task(run_input(user_input))
    return False  # False = clear the input box


# --- Welcome banner ---

LOGO = [  # "nanocode" in 3-row block letters
    "█▀▀▄ ▄▀▀▄ █▀▀▄ ▄▀▀▄ ▄▀▀▀ ▄▀▀▄ █▀▀▄ █▀▀▀",
    "█  █ █▀▀█ █  █ █  █ █    █  █ █  █ █▀▀ ",
    "▀  ▀ ▀  ▀ ▀  ▀  ▀▀   ▀▀▀  ▀▀  ▀▀▀  ▀▀▀▀",
]
LOGO_COLORS = [51, 45, 39, 33, 63, 99, 135, 171]  # 256-color gradient: cyan → blue → purple → pink


def gradient(line):
    """Color each column of the logo along the gradient."""
    result = ""
    for i, ch in enumerate(line):
        color = LOGO_COLORS[i * len(LOGO_COLORS) // len(line)]
        result += f"\033[38;5;{color}m{ch}" if ch != " " else " "
    return result + RESET


def visible_len(text):
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def banner(label):
    name = user_name()
    home = os.path.expanduser("~")
    cwd = os.getcwd().replace(home, "~", 1) if os.getcwd().startswith(home) else os.getcwd()
    n_memory, n_skills = len(memory_files()), len(list_skills())
    width = shutil.get_terminal_size((80, 24)).columns
    max_cwd = max(20, min(width, 80) - 16)
    if len(cwd) > max_cwd:
        cwd = "…" + cwd[-(max_cwd - 1):]  # keep the end of long paths, it's the useful part

    body = [gradient(line) for line in LOGO] + [""]
    body.append(f"{BOLD}Welcome back, {name}!{RESET}" if name else f"{BOLD}Welcome to nanocode!{RESET}")
    body.append("")
    body.append(f"{DIM}model  {RESET}{MODEL} {DIM}· {label}{RESET}")
    body.append(f"{DIM}cwd    {RESET}{cwd}")
    body.append(f"{DIM}memory {RESET}{n_memory} file{'s' if n_memory != 1 else ''} {DIM}·{RESET} {n_skills} skill{'s' if n_skills != 1 else ''}")
    if not name:
        body.append(f"{DIM}tip: type '# my name is …' and I'll greet you by name{RESET}")

    inner = max(visible_len(line) for line in body) + 4
    if width < inner + 4:  # small screen (phones): no box
        box = "\n".join("  " + line for line in body)
    else:
        border = "\033[38;5;240m"
        rows = [f" {border}╭{'─' * inner}╮{RESET}"]
        for line in body:
            pad = inner - 4 - visible_len(line)
            rows.append(f" {border}│{RESET}  {line}{' ' * pad}  {border}│{RESET}")
        rows.append(f" {border}╰{'─' * inner}╯{RESET}")
        box = "\n".join(rows)

    tips = f"  {DIM}/ commands · @ attach file · # remember · /resume chats · /help{RESET}"
    return f"\n{box}\n{tips}\n"


def choose_backend():
    """Ask once at startup (before the full-screen UI) which backend to use."""
    global BACKEND, OPENROUTER_KEY, MODEL
    print("\n" + "\n".join("  " + gradient(line) for line in LOGO) + "\n")
    print(f"  {BOLD}1{RESET}) Claude Code SDK {DIM}(Claude Max subscription){RESET}")
    print(f"  {BOLD}2{RESET}) OpenRouter API {DIM}(needs OPENROUTER_API_KEY){RESET}\n")
    while True:
        try:
            choice = input(f"{BOLD}{BLUE}❯{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)
        if choice in ("1", "2"):
            break
        print(f"{DIM}type 1 or 2{RESET}")

    if choice == "1":
        BACKEND = "sdk"
        MODEL = "claude-opus-5-5"
        return

    BACKEND = "api"
    OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
    if not OPENROUTER_KEY:
        print(f"\n{RED}⏺ OPENROUTER_API_KEY is not set.{RESET}")
        print(f"{DIM}  export OPENROUTER_API_KEY=sk-or-... and run nanocode again.{RESET}")
        sys.exit(1)
    MODEL = os.environ.get("MODEL", "openrouter/auto-code")


async def main():
    global CLIENT
    new_session()
    if BACKEND == "sdk":
        CLIENT = await make_client(MODEL)

    label = "Claude Code SDK" if BACKEND == "sdk" else "OpenRouter API"
    out(banner(label))

    input_box.accept_handler = on_enter
    await app.run_async()
    if CLIENT:
        await CLIENT.disconnect()


if __name__ == "__main__":
    choose_backend()
    asyncio.run(main())
