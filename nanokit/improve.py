"""Self-improvement, like Hermes: after a hard turn (many tool calls, or tools that failed), a quiet review in
the background saves what's worth remembering, as a memory line or a skill (new, or an improved existing one).
On with "self_improve": true in the config. It never blocks you: the review runs after the turn, without
showing its work, and at most once every few minutes; one dim line says what it learned. Saving a lesson or a
skill goes through the usual permission check (it asks, unless /auto or your rules allow `remember` and
`save_skill`).
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import nanocode as nc
from nanokit import on

MIN_CALLS = 15
MIN_FAILURES = 2
EVERY = 10 * 60  # seconds between reviews
REVIEW_PROMPT = """# Review
You review one finished turn of a coding agent's work, to make it better next time. You are given the user's
request, the tool calls it made (with failures) and its final answer. Look for: mistakes and how they were
fixed, things that took several attempts, a better approach it found, and multi-step procedures likely to come
up again in this project. Ignore anything trivial or one-off. You may read an existing skill with `skill` to
improve it, or look at files with read/glob/grep, but keep it short.

Reply with only a JSON object, no other text:
{"lessons": ["one short specific sentence", ...],
 "skill": null or {"name": "short-kebab-case", "description": "one line: when to use it",
                   "instructions": "markdown steps, commands, gotchas"}}
Use at most 3 lessons, and a skill only for a reusable procedure (reuse an existing skill's name to improve it).
Empty lists and null are fine, and usual: most turns teach nothing new. Never include secrets."""
REVIEW_TOOLS = {"skill", "read", "glob", "grep"}

TURNS = {}  # tab -> {"calls": [(name, args, result, is_error)], "failed": int}
STATE = {"last": 0.0, "running": None}


def enabled():
    return nc.st.config.get("self_improve") is True


def prune():
    """Forgets the turns of tabs that were closed (their turn_end never comes)."""
    tabs = getattr(nc.st, "tabs", None) or []
    for tab in list(TURNS):
        if not any(tab is t for t in tabs):
            TURNS.pop(tab, None)


@on("turn_start")
def start(text):
    prune()
    if enabled():
        TURNS[nc.st.current_tab()] = {"calls": [], "failed": 0}
    return None


@on("after_tool")
def count(name, args, result, is_error):
    turn = TURNS.get(nc.st.current_tab()) if enabled() else None
    if turn is not None and nc.AGENT_LABEL.get() is None:   # the main agent's calls, not a sub-agent's
        turn["calls"].append((name, args, result, is_error))
        turn["failed"] += bool(is_error)
    return None


@on("turn_end")
def end(text, error):
    tab = nc.st.current_tab()
    turn = TURNS.pop(tab, None)
    prune()
    if not enabled() or turn is None or error or text == nc.LEARN_PROMPT:
        return None
    if len(turn["calls"]) < MIN_CALLS and turn["failed"] < MIN_FAILURES:
        return None
    if (STATE["running"] and not STATE["running"].done()) or time.time() - STATE["last"] < EVERY:
        return None
    STATE["last"] = time.time()
    STATE["running"] = nc.in_tab(tab, review(text, turn["calls"], nc.st.last_reply or ""))
    return None


def describe_turn(text, calls, reply):
    lines = [f"## Request\n{nc.cap(text, 3000)}", "## Tool calls"]
    for i, (name, args, result, is_error) in enumerate(calls[-60:], 1):
        args_text = json.dumps(args, default=str)
        lines.append(f"{i}. {name} {args_text[:300]}{' → FAILED' if is_error else ''}\n   "
                     + " ".join(str(result).split())[:400 if is_error else 160])
    skills = nc.list_skills()
    if skills:
        lines.append("## Existing skills\n" + "\n".join(f"- {n}: {i['description']}" for n, i in skills.items()))
    lines.append(f"## Final answer\n{nc.cap(reply, 2000)}")
    return "\n\n".join(lines)


def parse(report):
    match = re.search(r"\{.*\}", report, re.S)
    if not match:
        return [], None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return [], None
    lessons = [" ".join(str(x).split()) for x in data.get("lessons") or [] if str(x).strip()][:3]
    skill = data.get("skill")
    if not (isinstance(skill, dict) and all(str(skill.get(k) or "").strip()
                                            for k in ("name", "description", "instructions"))):
        skill = None
    return lessons, skill


async def review(text, calls, reply):
    """The background review: a sub-agent reads the turn and answers with lessons and maybe a skill."""
    try:
        report, _ = await nc.run_subagent(describe_turn(text, calls, reply), "self-improve", prompt=REVIEW_PROMPT,
                                          tools=lambda tool: tool["name"] in REVIEW_TOOLS)
        lessons, skill = parse(report)
        kept = []
        for lesson in lessons:                   # each one asks (or follows /auto and your rules), like remember
            if await nc.allowed("remember", f"(self-improve) {lesson}"):
                nc.add_memory(lesson)
                kept.append(lesson)
        lessons = kept
        saved = None
        if skill:
            result = await nc.TOOLS["save_skill"]["fn"]({**skill, "scope": "project"})
            saved = skill["name"] if result != nc.DENIED else None
        if lessons or saved:
            parts = ([f"{len(lessons)} lesson{'s' if len(lessons) != 1 else ''}"] if lessons else []) + \
                ([f"skill {saved}"] if saved else [])
            first = f": {lessons[0][:80]}" if lessons else ""
            nc.out(f"  {nc.DIM}⎿  learned from that turn ({', '.join(parts)}){first}{nc.RESET}")
            # The next message picks them up: memory is part of the system prompt (Claude reconnects for it).
    except asyncio.CancelledError:
        raise
    except Exception as e:                     # learning is a bonus: never an error in your chat
        nc.log_error(e)
