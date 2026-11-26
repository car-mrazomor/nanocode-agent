"""Search across all your past chats, and /fork.

Every project keeps its chats in .nanocode/sessions/<id>.json. This module indexes them, from every project
you've used nanocode in, into one SQLite database (~/.nanocode/search.db, with full-text search when SQLite has
FTS5, else a plain LIKE search), so you can find an old conversation with /search and the model can recall one
with the session_search tool. The index is brought up to date on demand and after each turn, by file mtime:
only chats that changed are read again.

/fork copies the current chat into a new tab, to try another direction without losing this one.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import nanocode as nc
from nanokit import command, on

MAX_BODY = 400_000  # characters of one chat that are indexed
SNIPPET = 160
LAST = {"results": []}  # what /search showed last, for /search open <n>


# --- Reading saved chats ---

def content_text(content):
    """Text of a message's content: a string, or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") in
                         ("text", "input_text", "output_text"))
    return ""


def screen_text(screen):
    """The saved screen as plain text: your messages (marked lines) become "> " lines."""
    lines = []
    for line in nc.ANSI_RE.sub("", screen or "").split("\n"):
        if line.startswith(nc.USER_MARK):
            line = line.lstrip(nc.USER_MARK)
            if not line.strip():
                continue
            line = "> " + line
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chat_turns(data):
    """[(role, text)] of a saved chat: "user", "assistant" and "tool" (a call, with its arguments). A chat from
    the Claude subscription has no messages (Claude Code keeps them), so its saved screen is used instead."""
    turns = []
    for m in data.get("messages") or []:
        role = m.get("role")
        if role in ("user", "assistant"):
            text = content_text(m.get("content")).strip()
            if text:
                turns.append((role, text))
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                turns.append(("tool", f"{fn.get('name')} {fn.get('arguments') or ''}".strip()))
    if not turns:                              # Claude subscription: nanocode's own copy of the chat
        for t in data.get("turns") or []:
            if isinstance(t, dict) and t.get("role") in ("user", "assistant") and t.get("text"):
                turns.append((t["role"], t["text"]))
            elif isinstance(t, dict) and t.get("role") == "tool":
                turns.append(("tool", f"{t.get('name')} {json.dumps(t.get('arguments') or {})}"))
    if not turns and data.get("screen"):
        turns.append(("screen", screen_text(data["screen"])))
    return turns


def chat_body(data):
    return "\n".join(text for _, text in chat_turns(data))[:MAX_BODY]


def read_session(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def tool_counts(data):
    counts = {}
    for m in data.get("messages") or []:
        for call in m.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name") or "?"
            counts[name] = counts.get(name, 0) + 1
    if not counts:                             # the Claude subscription: the activity log has the calls
        for item in data.get("activity") or []:
            if item:
                counts[item[0]] = counts.get(item[0], 0) + 1
    return counts


# --- The index ---

def db_path():
    return nc.home_dir() / "search.db"


def has_fts5(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts5_probe")
        return True
    except sqlite3.Error:
        return False


def connect():
    """The index database, made if needed. The text table is FTS5 when this SQLite has it."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        fts = has_fts5(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key='fts'").fetchone()
        if row is not None and row[0] != ("1" if fts else "0"):
            conn.close()                       # made by a Python with the other kind of SQLite: start over
            path.unlink()
            return connect()
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('fts', ?)", ("1" if fts else "0",))
        conn.execute("CREATE TABLE IF NOT EXISTS projects (path TEXT PRIMARY KEY, seen REAL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS chats (
            file TEXT PRIMARY KEY, mtime REAL, project TEXT, id TEXT, title TEXT, created REAL, updated REAL,
            backend TEXT, model TEXT, messages INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost REAL,
            tools TEXT, changed TEXT)""")
        if fts:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(file UNINDEXED, title, body)")
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS docs (file TEXT PRIMARY KEY, title TEXT, body TEXT)")
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def uses_fts(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='fts'").fetchone()
    return bool(row and row[0] == "1")


def project_sessions(project):
    folder = Path(project) / ".nanocode" / "sessions"
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def index_file(conn, path, project, mtime):
    data = read_session(path)
    file = str(path)
    conn.execute("DELETE FROM docs WHERE file=?", (file,))
    conn.execute("DELETE FROM chats WHERE file=?", (file,))
    if not data or not data.get("title"):
        conn.execute("INSERT INTO chats (file, mtime, project) VALUES (?, ?, ?)", (file, mtime, project))
        return
    usage = data.get("usage") or {}
    messages = sum(1 for m in data.get("messages") or [] if m.get("role") in ("user", "assistant"))
    if not data.get("messages"):               # Claude subscription: your messages (and replies) on the screen
        messages = sum(1 for line in (data.get("screen") or "").split("\n") if line == nc.USER_MARK)
    conn.execute("INSERT INTO chats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        file, mtime, project, data.get("id") or path.stem, data.get("title"), data.get("created") or mtime,
        data.get("updated") or mtime, data.get("backend"), data.get("model"), messages,
        int(usage.get("input") or 0), int(usage.get("output") or 0), float(usage.get("cost") or 0),
        json.dumps(tool_counts(data)), json.dumps(data.get("changed") or {})))
    conn.execute("INSERT INTO docs (file, title, body) VALUES (?, ?, ?)", (file, data.get("title"), chat_body(data)))


def index_project(conn, project):
    """Brings one project's chats up to date; returns how many were (re)read."""
    project = str(project)
    known = {f: m for f, m in conn.execute("SELECT file, mtime FROM chats WHERE project=?", (project,))}
    count = 0
    for path in project_sessions(project):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        known_mtime = known.pop(str(path), None)
        if known_mtime is not None and abs(known_mtime - mtime) < 1e-6:
            continue
        index_file(conn, path, project, mtime)
        count += 1
    for gone in known:                         # deleted chats
        conn.execute("DELETE FROM docs WHERE file=?", (gone,))
        conn.execute("DELETE FROM chats WHERE file=?", (gone,))
    return count


def update_index(everywhere=True):
    """Indexes this project (and remembers it), then every other project seen before. Returns the count."""
    conn = connect()
    try:
        here = str(Path.cwd())
        if project_sessions(here):
            conn.execute("INSERT OR REPLACE INTO projects VALUES (?, ?)", (here, time.time()))
        projects = [here] + ([p for (p,) in conn.execute("SELECT path FROM projects") if p != here]
                             if everywhere else [])
        count = 0
        for project in projects:
            if project != here and not Path(project).is_dir():
                conn.execute("DELETE FROM projects WHERE path=?", (project,))
                conn.execute("DELETE FROM docs WHERE file IN (SELECT file FROM chats WHERE project=?)", (project,))
                conn.execute("DELETE FROM chats WHERE project=?", (project,))
                continue
            count += index_project(conn, project)
        conn.commit()
        return count
    finally:
        conn.close()


def fts_query(text):
    """The words of a search as an FTS5 query: each word quoted (no syntax errors), all must match."""
    words = re.findall(r"\w[\w.\-']*", text)
    return " ".join('"' + w.replace('"', '""') + '"' for w in words)


def make_snippet(body, words, size=SNIPPET):
    low = body.lower()
    pos = min([i for i in (low.find(w.lower()) for w in words) if i >= 0] or [0])
    start = max(0, pos - size // 3)
    text = " ".join(body[start:start + size].split())
    return ("…" if start else "") + text + ("…" if start + size < len(body) else "")


def search(query, limit=10, project=None):
    """Chats matching the query, best first: [{project, id, title, updated, snippet, file, backend}]."""
    words = re.findall(r"\w[\w.\-']*", query)
    if not words:
        return []
    conn = connect()
    try:
        where, params = "", []
        if project:
            where, params = " AND c.project=?", [str(project)]
        if uses_fts(conn):
            rows = conn.execute(
                "SELECT c.project, c.id, c.title, c.updated, snippet(docs, 2, '[', ']', '…', 24), c.file, c.backend "
                "FROM docs JOIN chats c ON c.file = docs.file WHERE docs MATCH ?" + where +
                " ORDER BY bm25(docs, 0, 5.0, 1.0), c.updated DESC LIMIT ?", [fts_query(query), *params, limit]
            ).fetchall()
        else:
            like = " AND ".join("(d.title LIKE ? OR d.body LIKE ?)" for _ in words)
            values = [v for w in words for v in (f"%{w}%", f"%{w}%")]
            rows = conn.execute(
                "SELECT c.project, c.id, c.title, c.updated, d.body, c.file, c.backend FROM docs d "
                "JOIN chats c ON c.file = d.file WHERE " + like + where + " ORDER BY c.updated DESC LIMIT ?",
                [*values, *params, limit]).fetchall()
            rows = [(*r[:4], make_snippet(r[4] or "", words), *r[5:]) for r in rows]
    finally:
        conn.close()
    return [{"project": r[0], "id": r[1], "title": r[2], "updated": r[3] or 0,
             "snippet": " ".join((r[4] or "").split()), "file": r[5], "backend": r[6]} for r in rows]


async def refresh(everywhere=True):
    try:
        return await asyncio.get_running_loop().run_in_executor(None, update_index, everywhere)
    except (sqlite3.Error, OSError) as e:
        nc.log_error(e)
        return 0


@on("turn_end")
async def index_after_turn(text, error):
    if os.environ.get("NANOCODE_NO_SEARCH_INDEX"):
        return None
    await refresh(everywhere=False)
    return None


# --- /search ---

def when(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def where(project):
    return "this project" if project == str(Path.cwd()) else nc.tilde(Path(project))


@command("/search", "search all your past chats: /search <words>, /search open <n>")
async def cmd_search(arg):
    if not arg:
        nc.out(f"{nc.DIM}usage: /search <words> · /search open <n> resumes a result{nc.RESET}\n")
        return
    word, _, rest = arg.partition(" ")
    if word == "open" and rest.strip().isdigit():
        await open_result(int(rest.strip()))
        return
    await refresh()
    try:
        results = await asyncio.get_running_loop().run_in_executor(None, search, arg, 15)
    except (sqlite3.Error, OSError) as e:
        nc.out(f"{nc.RED}⏺ search failed: {e}{nc.RESET}\n")
        return
    LAST["results"] = results
    if not results:
        nc.out(f"{nc.DIM}no chat mentions {arg!r}{nc.RESET}\n")
        return
    lines = [f"{nc.BOLD}Chats matching {arg!r}{nc.RESET} {nc.DIM}(/search open <n>){nc.RESET}"]
    for i, r in enumerate(results, 1):
        lines.append(f"  {nc.CYAN}{i:>2}{nc.RESET}. {r['title']}  {nc.DIM}{where(r['project'])} · "
                     f"{when(r['updated'])}{nc.RESET}")
        if r["snippet"]:
            lines.append(f"      {nc.DIM}{nc.clip_cells(r['snippet'], 110)}{nc.RESET}")
    nc.out("\n".join(lines) + "\n")


async def open_result(n):
    results = LAST["results"]
    if not 1 <= n <= len(results):
        nc.out(f"{nc.RED}⏺ no result {n} (search first: /search <words>){nc.RESET}\n")
        return
    r = results[n - 1]
    if r["project"] != str(Path.cwd()):
        nc.out(f"⏺ That chat is in {nc.tilde(Path(r['project']))}. Open nanocode there and run "
               f"{nc.CYAN}/resume {r['id']}{nc.RESET} (or /search again).\n")
        return
    data = read_session(r["file"])
    if not data:
        nc.out(f"{nc.RED}⏺ that chat's file is gone{nc.RESET}\n")
        return
    await resume_data(data)


async def resume_data(data):
    """Opens a saved chat of this project in the current tab (like /resume)."""
    if data.get("backend") != nc.st.backend:
        nc.out(f"{nc.RED}⏺ that chat was made with another provider ({data.get('backend')}); switch with "
               f"/provider first{nc.RESET}\n")
        return
    if nc.st.backend == "sdk" and not data.get("sdk_session_id"):
        nc.out(f"{nc.RED}⏺ that chat has no Claude session id, so it can't be resumed{nc.RESET}\n")
        return
    here = nc.st.current_tab()
    other = next((t for t in nc.st.tabs if t is not here and (t.session or {}).get("id") == data.get("id")), None)
    if other is not None:
        nc.switch_tab(nc.st.tabs.index(other))
        return
    if data.get("id") != nc.st.session.get("id"):
        nc.leave_current_chat()
    await nc.open_session(data)
    nc.out(f"{nc.GREEN}⏺ Resumed:{nc.RESET} {data.get('title')} {nc.DIM}({nc.time_ago(data.get('updated') or 0)})"
           f"{nc.RESET}\n")


# --- The session_search tool ---

nc.READ_ONLY_TOOLS.add("session_search")
nc.AGENT_READ_ONLY.add("session_search")
nc.TOOL_PREVIEW["session_search"] = "query"


@nc.tool("session_search", "Search the user's earlier nanocode chats in this project to recall what was said or "
         "decided before. Returns matching chats with an id and a snippet; call again with `id` to read one chat. "
         "all_projects=true searches the chats of every project (asks the user first).",
         {"query": {"type": "string", "description": "words to look for"},
          "limit": {"type": "integer", "description": "how many chats (default 5, at most 20)"},
          "id": {"type": "string", "description": "a chat id from the results: returns that chat's text"},
          "all_projects": {"type": "boolean", "description": "also other projects' chats (default: this one only)"}})
async def tool_session_search(args):
    everywhere = bool(args.get("all_projects"))
    if everywhere and not nc.st.config.get("session_search_all_projects"):
        what = f"read chat {args['id']}" if args.get("id") else f"search for {str(args.get('query') or '')!r}"
        if not await nc.allowed("session_search", f"{what} in the chats of every project"):
            return nc.DENIED
    project = None if everywhere else str(Path.cwd())
    await refresh(everywhere)
    if args.get("id"):
        found = await asyncio.to_thread(read_chat, str(args["id"]), project)
        if not found:
            raise nc.ToolError(f"no chat with id {args['id']!r}" + ("" if everywhere else " in this project"))
        data, where_from = found
        lines = [f"# {data.get('title')} ({where_from}, {when(data.get('updated'))})"]
        lines += [f"{role}: {text}" for role, text in chat_turns(data)]
        return nc.cap("\n\n".join(lines), 20_000)
    query = str(args.get("query") or "").strip()
    if not query:
        raise nc.ToolError("give a query (words to look for) or an id")
    try:
        limit = max(1, min(20, int(args.get("limit") or 5)))
    except (TypeError, ValueError):
        limit = 5
    try:
        results = await asyncio.to_thread(search, query, limit, project)
    except sqlite3.Error as e:
        raise nc.ToolError(f"couldn't search the chats: {e}")
    if not results:
        return f"no earlier chat matches {query!r}" + ("" if everywhere else " in this project (all_projects=true "
                                                                             "looks in the others)")
    return "\n\n".join(f"{i}. {r['title']} (id {r['id']}; {r['project']}; {when(r['updated'])})\n   {r['snippet']}"
                       for i, r in enumerate(results, 1))


def read_chat(sid, project=None):
    """(session data, its project) of a saved chat by id, only in `project` when given; or None. Blocks."""
    conn = connect()
    try:
        sql, params = "SELECT c.file, c.project FROM chats c WHERE c.id=?", [sid]
        if project:
            sql, params = sql + " AND c.project=?", [sid, project]
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    data = read_session(row[0]) if row else None
    return (data, row[1]) if data else None


# --- /fork ---

FORK_NOTE_LIMIT = 30_000
FORK_NOTES = {}  # id(tab) -> the earlier chat, sent with the first message of a forked Claude subscription chat


@command("/fork", "copy this chat into a new tab, to try another direction")
async def cmd_fork(arg):
    src = nc.st.current_tab()
    title = (src.session or {}).get("title")
    if not title:
        nc.out(f"{nc.DIM}nothing to fork yet: send a message first{nc.RESET}\n")
        return
    nc.save_session()
    tab = nc.new_tab(announce=False)
    if tab is None:
        return
    fork_into(src, tab, arg.strip() or f"fork of {title}")
    nc.invalidate()


def fork_into(src, tab, title):
    tab.model = src.model
    tab.messages = copy.deepcopy(src.messages)
    tab.activity = list(src.activity)
    tab.changed = copy.deepcopy(src.changed)
    tab.todos = copy.deepcopy(src.todos)
    tab.usage = {"input": 0, "output": 0, "cost": 0.0}
    tab.session = {"id": nc.make_session_id(), "title": nc.short_title(title), "created": time.time(),
                   "sdk_session_id": None}
    tab.output = src.output
    tab.scroll_top = tab.selection = None
    with nc.for_tab(tab):
        if nc.st.backend == "sdk":
            # Claude Code's conversation can't be copied: the new one gets the old one as a note with your first
            # message in this tab.
            FORK_NOTES[id(tab)] = screen_text(src.output)[-FORK_NOTE_LIMIT:]
            nc.out(f"\n{nc.GREEN}⏺ Forked{nc.RESET} {nc.DIM}into a new tab: a fresh Claude conversation that gets "
                   f"this chat as context with your next message{nc.RESET}\n")
        else:
            nc.out(f"\n{nc.GREEN}⏺ Forked{nc.RESET} {nc.DIM}into a new tab: \"{tab.session['title']}\"; the "
                   f"original stays in its tab{nc.RESET}\n")
            nc.save_session()


@on("message")
async def send_fork_note(text):
    tab = nc.st.current_tab()
    note = FORK_NOTES.get(id(tab))
    if note is None or text.startswith(("/", "#")) or nc.st.backend != "sdk":
        return None
    FORK_NOTES.pop(id(tab), None)
    images = []
    body = nc.expand_mentions(nc.expand_chips(text, images), images)
    prompt = ("This chat continues an earlier conversation (a fork of it). This is what was on screen there, "
              f"most recent last:\n\n<earlier_chat>\n{note}\n</earlier_chat>\n\n---\n\n{body}")
    try:
        await nc.send(prompt, display=nc.expand_chips(text, []), images=images)
        nc.out()
    except Exception as e:
        nc.stream_end()
        nc.st.last_error = str(e)
        nc.log_error(e)
        nc.out(f"{nc.RED}⏺ Error: {e}{nc.RESET}\n")
    return ""
