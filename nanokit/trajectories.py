"""Saved chats as training data: `nanocode trajectories export` writes one JSON line per chat, with the tool
calls, in the ShareGPT or OpenAI chat format. Chats with a Claude subscription keep their history in Claude
Code, not in nanocode, so only OpenRouter and Ollama chats (which have their full messages) are exported.

    nanocode trajectories export [--format sharegpt|openai] [--out file.jsonl] [--all-projects] [--compress]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import nanocode as nc
from nanokit import cli, on

TRIM = 2000  # --compress: longest tool output kept (head and tail)
SCAN_DEPTH = 3


def projects_file():
    return nc.home_dir() / "projects.json"


def known_projects():
    try:
        data = json.loads(projects_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


@on("turn_end")
def remember_project(text, error):
    """Notes the folders nanocode chats in, so --all-projects can find their sessions."""
    here = str(Path.cwd())
    projects = known_projects()
    if here not in projects:
        try:
            projects_file().parent.mkdir(parents=True, exist_ok=True)
            projects_file().write_text(json.dumps(projects + [here], indent=1), encoding="utf-8")
        except OSError as e:
            nc.log_error(e)
    return None


def scan_home(depth=SCAN_DEPTH):
    """Projects found under your home folder (a few levels deep): folders with .nanocode/sessions."""
    found, home = [], Path.home()

    def walk(folder, level):
        try:
            if (folder / ".nanocode" / "sessions").is_dir():
                found.append(str(folder))
            if level >= depth:
                return
            for child in folder.iterdir():
                if child.is_dir() and not child.name.startswith(".") and child.name not in nc.IGNORE_DIRS \
                        and not child.is_symlink():
                    walk(child, level + 1)
        except OSError:
            pass

    walk(home, 0)
    return found


def session_files(all_projects=False):
    folders = [str(Path.cwd())]
    if all_projects:
        folders += known_projects() + scan_home()
    files, seen = [], set()
    for folder in folders:
        root = Path(folder) / ".nanocode" / "sessions"
        try:
            key = root.resolve()
        except OSError:
            continue
        if key in seen or not root.is_dir():
            continue
        seen.add(key)
        files += sorted(root.glob("*.json"))
    return files


def clean(messages):
    """The messages in plain OpenAI form: complete tool calls only, no nanocode-only fields."""
    result = []
    for msg in nc.repair_history([m for m in messages if isinstance(m, dict) and m.get("role")]):
        role = msg["role"]
        content = msg.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        content = content or ""
        if msg.get("images"):
            content += "".join("\n[image]" for _ in msg["images"])
        item = {"role": role, "content": content}
        if role == "assistant" and msg.get("tool_calls"):
            item["tool_calls"] = [{"id": c["id"], "type": "function",
                                   "function": {"name": c["function"]["name"],
                                                "arguments": c["function"].get("arguments") or "{}"}}
                                  for c in msg["tool_calls"]]
        if role == "tool":
            item["tool_call_id"] = msg.get("tool_call_id")
        result.append(item)
    return result


def compress(messages):
    """Trims long tool output and drops failed calls the model then retried (the same tool, later on)."""
    names = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls") or []}
    results = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    order = [c["id"] for m in messages for c in m.get("tool_calls") or []]
    drop = set()
    for i, call_id in enumerate(order):
        if results.get(call_id, "").startswith("error") and names[call_id] in (names[c] for c in order[i + 1:]):
            drop.add(call_id)
    result = []
    for msg in messages:
        if msg["role"] == "tool":
            if msg["tool_call_id"] in drop:
                continue
            msg = {**msg, "content": nc.cap(msg["content"], TRIM)}
        elif msg.get("tool_calls"):
            calls = [c for c in msg["tool_calls"] if c["id"] not in drop]
            msg = {k: v for k, v in msg.items() if k != "tool_calls"}
            if calls:
                msg["tool_calls"] = calls
            elif not msg["content"].strip():
                continue
        result.append(msg)
    return result


def to_sharegpt(messages):
    """ShareGPT: human / gpt turns, with function_call and observation turns for tools."""
    roles = {"user": "human", "assistant": "gpt", "system": "system"}
    turns = []
    for msg in messages:
        if msg["role"] == "tool":
            turns.append({"from": "observation", "value": msg["content"]})
            continue
        if msg["content"] or not msg.get("tool_calls"):
            turns.append({"from": roles.get(msg["role"], msg["role"]), "value": msg["content"]})
        for call in msg.get("tool_calls") or []:
            try:
                arguments = json.loads(call["function"]["arguments"])
            except json.JSONDecodeError:
                arguments = call["function"]["arguments"]
            turns.append({"from": "function_call",
                          "value": json.dumps({"name": call["function"]["name"], "arguments": arguments})})
    return {"conversations": turns}


def trajectories(files, fmt="sharegpt", squeeze=False):
    """(lines, sessions read): a JSON line per session that has messages."""
    lines, read = [], 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("messages") or [], list):
            continue                                        # not a saved chat
        read += 1
        messages = clean(data.get("messages") or [])
        if squeeze:
            messages = compress(messages)
        if not any(m["role"] == "assistant" for m in messages):
            continue
        record = to_sharegpt(messages) if fmt == "sharegpt" else {"messages": messages}
        lines.append(json.dumps(record, ensure_ascii=False))
    return lines, read


@cli("trajectories", "export saved chats as training data: trajectories export --help")
def trajectories_cli(argv):
    parser = argparse.ArgumentParser(prog="nanocode trajectories",
                                     description="Export saved chats (OpenRouter and Ollama ones) as JSONL.")
    parser.add_argument("action", choices=["export"])
    parser.add_argument("--format", choices=["sharegpt", "openai"], default="sharegpt")
    parser.add_argument("--out", help="file to write (default: standard output)")
    parser.add_argument("--all-projects", action="store_true",
                        help="every project nanocode has chats in, not just this folder")
    parser.add_argument("--compress", action="store_true",
                        help="trim long tool output and drop failed calls that were retried")
    args = parser.parse_args(argv)
    files = session_files(args.all_projects)
    lines, read = trajectories(files, args.format, args.compress)
    text = "".join(line + "\n" for line in lines)
    if args.out:
        out_path = Path(os.path.expanduser(args.out))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        where = nc.tilde(out_path)
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
        where = "standard output"
    skipped = read - len(lines)
    print(f"exported {len(lines)} chat{'s' if len(lines) != 1 else ''} to {where}"
          + (f" ({skipped} without messages skipped: Claude subscription chats keep theirs in Claude Code)"
             if skipped else ""), file=sys.stderr)
    return 0
