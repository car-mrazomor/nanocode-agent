"""Custom agents, like OpenCode's: a markdown file per agent, with a prompt and optionally its own model and
tools. The model hands them tasks with `run_agent`; you can call one yourself with `@name task`.

    .nanocode/agents/reviewer.md   (this project; wins over a global one with the same name)
    ~/.nanocode/agents/reviewer.md (every project)

    ---
    description: Reviews a change for bugs
    model: anthropic/claude-sonnet-5.5      (optional)
    tools: read, grep, glob                 (optional: only these tools)
    ---
    You are a careful reviewer...
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import nanocode as nc
from nanokit import command, on

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MENTION_RE = re.compile(r"^@([A-Za-z0-9][\w-]*)\s+(\S.*)$", re.S)
NO_NESTING = {"agent", "run_agent", "todo"}  # a custom agent can't start agents or change the chat's plan
TEMPLATE = """---
description: {description}
# model: anthropic/claude-sonnet-5.5   (optional: another model for this agent)
# tools: read, grep, glob              (optional: only these tools; default: the read-only ones)
---
You are {name}, a specialist agent. Describe here what it does, how it works and what its report should
contain. The main agent only sees that report.
"""


def agent_dirs():
    """Earlier wins when two agents share a name."""
    return [nc.project_dir() / "agents", nc.home_dir() / "agents"]


def frontmatter(text):
    """(meta, body) from a markdown file with a `---` header: `key: value` lines, `# comments`, and lists
    written as `[a, b]`, `a, b` or `- a` lines under the key."""
    meta, body = {}, text
    lines = text.lstrip("﻿").split("\n")
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is not None:
            key = None
            for line in lines[1:end]:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("- ") and key:
                    if not isinstance(meta.get(key), list):
                        meta[key] = []
                    meta[key].append(stripped[2:].strip().strip("\"'"))
                elif ":" in line and not line[0].isspace():
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    meta[key] = value.strip().strip("\"'")
            body = "\n".join(lines[end + 1:])
    return meta, body.strip()


def as_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    value = str(value or "").strip().strip("[]")
    return [v.strip().strip("\"'") for v in re.split(r"[,\s]+", value) if v.strip().strip("\"'")]


def list_agents():
    """name -> {"description", "model", "tools" (a list, or None for the default), "prompt", "path"}."""
    agents = {}
    for root in agent_dirs():
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            try:
                meta, body = frontmatter(nc.read_text(path))
            except (OSError, UnicodeDecodeError):
                continue
            name = (meta.get("name") or path.stem).strip().lower()
            if not NAME_RE.match(name) or name in agents or not body:
                continue
            tools = as_list(meta.get("tools")) if meta.get("tools") else None
            agents[name] = {"description": " ".join(str(meta.get("description") or "").split()),
                            "model": str(meta.get("model") or "").strip() or None,
                            "tools": tools, "prompt": body, "path": path}
    return agents


def tool_filter(allowed):
    """The tools a custom agent gets: its own list (names or patterns like mcp__github__*), or the default
    sub-agent set."""
    if not allowed:
        return lambda tool: tool["name"] not in NO_NESTING and nc.subagent_tools(tool)
    return lambda tool: tool["name"] not in NO_NESTING and any(fnmatch.fnmatchcase(tool["name"], p) for p in allowed)


async def run_agent(name, task):
    """Runs a custom agent on a task; returns (report, totals)."""
    info = list_agents().get(name)
    if info is None:
        raise nc.ToolError(f"no agent named '{name}'. Available: {', '.join(list_agents()) or 'none'}")
    prompt = f"# Agent: {name}\n{info['prompt']}\n\n{nc.SUBAGENT_PROMPT}"
    return await nc.run_subagent(task, name, prompt=prompt, tools=tool_filter(info["tools"]), model=info["model"])


def steps_line(name, totals):
    return (f"     {nc.fg('faint')}⎿  {name}: {totals['steps']} steps · {nc.fmt_tokens(totals['input'])} in · "
            f"{nc.fmt_tokens(totals['output'])} out{nc.RESET}")


@nc.tool("run_agent", "Hand a task to one of the user's custom agents (listed under 'Custom agents' in your "
         "instructions). It works on its own with its own instructions and tools, and returns a short report.",
         {"agent": {"type": "string", "description": "the agent's name"},
          "task": {"type": "string", "description": "what to do and what to report, with all the context it needs"}},
         ["agent", "task"])
async def tool_run_agent(args):
    if nc.AGENT_LABEL.get() is not None:
        raise nc.ToolError("a sub-agent can't start other agents; do the work yourself")
    name = str(args["agent"]).strip().lstrip("@").lower()
    report, totals = await run_agent(name, args["task"])
    nc.out(steps_line(name, totals))
    return report


@on("system_prompt")
def describe_agents():
    agents = list_agents()
    if not agents:
        return None
    lines = [f"- {name}: {info['description'] or '(no description)'}" for name, info in agents.items()]
    return ("# Custom agents\nThe user set up these agents. When a task matches one, hand it over with "
            "`run_agent` (agent, task) instead of doing it yourself:\n" + "\n".join(lines))


async def run_visible(name, task, display):
    """Runs an agent as if it were a chat turn: your message, then its report as the reply."""
    nc.out(nc.user_block(display))
    if not nc.st.session.get("title"):
        nc.st.session["title"] = nc.short_title(display)
    nc.out(f"   {nc.fg('accent2')}⏺{nc.RESET} {nc.fg('text')}{name}{nc.RESET} {nc.fg('muted')}working…{nc.RESET}")
    try:
        report, totals = await run_agent(name, nc.expand_mentions(task))
    except Exception as e:                     # shown here: a failing hook would send the message on instead
        nc.log_error(e)
        nc.out(f"{nc.RED}⏺ {name}: {e}{nc.RESET}\n")
        return
    nc.out(steps_line(name, totals))
    nc.stream_text(report)
    nc.stream_end()
    if nc.st.backend != "sdk":                 # the main chat sees what happened, for follow-up questions
        nc.st.messages.append({"role": "user", "content": display})
        nc.st.messages.append({"role": "assistant", "content": report})
    nc.save_session()
    nc.out()


@on("message")
async def mention(text):
    """`@reviewer check the last commit` runs that agent directly; its report is the reply."""
    match = MENTION_RE.match(text.strip())
    if not match or match.group(1).lower() not in list_agents() or Path(match.group(1)).exists():
        return None
    await run_visible(match.group(1).lower(), match.group(2).strip(), text.strip())
    return ""


@command("/agents", "custom agents: list, or /agents new <name> [--global]")
async def agents_command(arg):
    words = arg.split()
    if words[:1] == ["new"]:
        names = [w for w in words[1:] if not w.startswith("--")]
        name = names[0].lower() if names else ""
        if not NAME_RE.match(name):
            nc.out(f"{nc.RED}⏺ /agents new <name>: lowercase letters, digits, - and _{nc.RESET}\n")
            return
        if "--global" in words:
            root = nc.home_dir() / "agents"
        else:
            nc.ensure_project_dir()
            root = nc.project_dir() / "agents"
        path = root / f"{name}.md"
        if path.exists():
            nc.out(f"{nc.YELLOW}⏺ {nc.tilde(path)} already exists{nc.RESET}\n")
            return
        root.mkdir(parents=True, exist_ok=True)
        nc.write_text(path, TEMPLATE.format(name=name, description=f"What {name} does, and when to use it"))
        nc.out(f"{nc.GREEN}⏺ Created{nc.RESET} {nc.tilde(path)} {nc.DIM}(edit its description and prompt; "
               f"then @{name} <task>){nc.RESET}\n")
        return
    agents = list_agents()
    lines = [f"{nc.BOLD}Agents{nc.RESET} {nc.DIM}(@name <task> runs one; the model calls them with run_agent){nc.RESET}"]
    if not agents:
        lines.append(f"  {nc.DIM}none yet: /agents new <name> creates .nanocode/agents/<name>.md{nc.RESET}")
    for name, info in agents.items():
        extra = " · ".join(x for x in (info["model"], ", ".join(info["tools"] or [])) if x)
        lines.append(f"  {nc.CYAN}@{name:<16}{nc.RESET} {info['description'][:60]}  "
                     f"{nc.DIM}{extra + ' · ' if extra else ''}{nc.tilde(info['path'])}{nc.RESET}")
    nc.out("\n".join(lines) + "\n")
