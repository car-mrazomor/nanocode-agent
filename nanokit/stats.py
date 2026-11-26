"""/stats: how you use nanocode, from your saved chats.

Chats, messages, tool calls by tool, tokens and cost (overall and by model), activity over the last 14 days
and the files changed most. `/stats` covers this project; `/stats all` every project in the search index
(see nanokit/sessions.py, which keeps per-chat totals next to the text).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import nanocode as nc
from nanokit import command
from nanokit.sessions import connect, update_index

DAYS = 14
BAR = 24  # cells for the longest bar


def collect(everywhere=False):
    """Totals from the index: this project's chats, or all of them."""
    update_index(everywhere=everywhere)
    conn = connect()
    try:
        sql = ("SELECT project, model, updated, messages, tokens_in, tokens_out, cost, tools, changed FROM chats "
               "WHERE title IS NOT NULL")
        rows = conn.execute(sql + ("" if everywhere else " AND project=?"),
                            () if everywhere else (str(Path.cwd()),)).fetchall()
    finally:
        conn.close()
    s = {"chats": 0, "messages": 0, "tool_calls": 0, "in": 0, "out": 0, "cost": 0.0, "projects": set(),
         "tools": {}, "models": {}, "days": {}, "files": {}}
    for project, model, updated, messages, tin, tout, cost, tools, changed in rows:
        s["chats"] += 1
        s["messages"] += messages or 0
        s["in"] += tin or 0
        s["out"] += tout or 0
        s["cost"] += cost or 0
        s["projects"].add(project)
        m = s["models"].setdefault(model or "?", {"chats": 0, "in": 0, "out": 0, "cost": 0.0})
        m["chats"] += 1
        m["in"] += tin or 0
        m["out"] += tout or 0
        m["cost"] += cost or 0
        for name, n in (json.loads(tools or "{}") or {}).items():
            s["tools"][name] = s["tools"].get(name, 0) + n
            s["tool_calls"] += n
        day = time.strftime("%Y-%m-%d", time.localtime(updated or 0))
        d = s["days"].setdefault(day, {"chats": 0, "messages": 0})
        d["chats"] += 1
        d["messages"] += messages or 0
        for path, lines in (json.loads(changed or "{}") or {}).items():
            name = path if everywhere or not Path(path).is_absolute() else nc.display_path(Path(path))
            f = s["files"].setdefault(name, 0)
            s["files"][name] = f + sum(n for n in (lines or [])[:2] if isinstance(n, int))
    return s


def bar(value, top, width=BAR):
    if not value or not top:
        return ""
    cells = value / top * width
    full = int(cells)
    part = " ▏▎▍▌▋▊▉"[int((cells - full) * 8)]
    return "█" * full + (part if part != " " else "")


def render(s, everywhere=False):
    D, R, C, B = nc.DIM, nc.RESET, nc.CYAN, nc.BOLD
    scope = f"all projects ({len(s['projects'])})" if everywhere else "this project"
    if not s["chats"]:
        return f"{D}no saved chats in {scope} yet{R}\n"
    lines = [f"{B}Usage{R} {D}· {scope}{R}",
             f"  {s['chats']} chats · {s['messages']} messages · {s['tool_calls']} tool calls · "
             f"{nc.fmt_tokens(s['in'])} in · {nc.fmt_tokens(s['out'])} out"
             + (f" · ${s['cost']:.2f}" if s["cost"] else "")]
    lines += ["", f"{B}By model{R}"]
    for model, m in sorted(s["models"].items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))[:8]:
        lines.append(f"  {nc.clip_cells(nc.pretty_model(model), 28):<28} {D}{m['chats']:>4} chats · "
                     f"{nc.fmt_tokens(m['in']):>6} in · {nc.fmt_tokens(m['out']):>6} out"
                     + (f" · ${m['cost']:.2f}" if m["cost"] else "") + R)
    if s["tools"]:
        lines += ["", f"{B}Tool calls{R}"]
        top = max(s["tools"].values())
        for name, n in sorted(s["tools"].items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"  {nc.clip_cells(name, 16):<16} {n:>5} {C}{bar(n, top, 20)}{R}")
    lines += ["", f"{B}Last {DAYS} days{R} {D}(messages){R}"]
    days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(DAYS - 1, -1, -1)]
    top = max([s["days"].get(d, {}).get("messages", 0) for d in days] + [0])
    for d in days:
        info = s["days"].get(d, {"chats": 0, "messages": 0})
        label = time.strftime("%a %d %b", time.strptime(d, "%Y-%m-%d"))
        lines.append(f"  {D}{label}{R}  {C}{bar(info['messages'], top):<{BAR}}{R} "
                     f"{D}{info['messages'] or ''}{R}")
    if s["files"]:
        lines += ["", f"{B}Most changed files{R} {D}(lines added + removed){R}"]
        for path, n in sorted(s["files"].items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"  {n:>6}  {nc.clip_cells(path, 70)}")
    return "\n".join(lines) + "\n"


@command("/stats", "usage across your saved chats: /stats (this project) or /stats all")
async def cmd_stats(arg):
    everywhere = arg.strip().lower() in ("all", "--all", "everywhere")
    nc.save_session()                          # this chat's latest turn counts too
    try:
        s = await asyncio.get_running_loop().run_in_executor(None, collect, everywhere)
    except Exception as e:
        nc.log_error(e)
        nc.out(f"{nc.RED}⏺ can't read the stats: {e}{nc.RESET}\n")
        return
    nc.out(render(s, everywhere))
