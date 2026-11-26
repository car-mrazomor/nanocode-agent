"""/compare: one prompt to several models at once, answers side by side. Only the user starts it (it's a command, not a tool), and only what they typed is sent."""
from __future__ import annotations

import asyncio
import dataclasses
import io
import os
import time
from rich.console import Console

import nanocode as nc

__all__ = [
    'COMPARE_SYSTEM',
    'compare_models',
    'compare_target',
    'compare_one',
    'render_compare',
    'cmd_compare',
]


# --- /compare: one prompt to several models, answers side by side. Runs only when the user types the
#     command; it is not a tool, so the model can never start it. Only the prompt (and files the user
#     attached with @) is sent: no chat history, no memory, no project details. ---

COMPARE_SYSTEM = "Answer the user's message directly and concisely. Use Markdown where it helps."


def compare_models():
    chosen = nc.st.config.get("compare_models")
    if chosen:
        return list(chosen)
    others = [m for m in nc.model_choices() if m != nc.st.model and not m.startswith("openrouter/")]
    prefix = "claude:" if nc.st.backend == "sdk" else "ollama:" if nc.st.backend == "ollama" else \
        f"{nc.st.provider}:" if nc.st.provider in nc.API_PROVIDERS else ""
    return [prefix + m for m in [nc.st.model] + others[:2]]


def compare_target(entry):
    """("claude" | "ollama" | "openrouter" | an API provider, model) for 'claude:x', 'ollama:x', 'openai:x' or a
    plain OpenRouter model."""
    head, _, rest = entry.partition(":")
    if (head in ("claude", "ollama", "openrouter") or head in nc.API_PROVIDERS) and rest:
        return head, rest
    return "openrouter", entry


async def compare_one(entry, prompt):
    """One model's answer, without streaming into the chat. {model, text, error, seconds, input, output, cost}"""
    kind, model = compare_target(entry)
    result = {"model": entry, "text": "", "error": None, "input": 0, "output": 0, "cost": 0.0}
    started = time.time()
    nc.QUIET.set(True)
    nc.CALL_MODEL.set(model)
    try:
        if kind == "claude":
            if nc.sdk is None:
                raise RuntimeError("claude-agent-sdk isn't installed")
            options = nc.sdk.ClaudeAgentOptions(model=model, system_prompt=COMPARE_SYSTEM, setting_sources=[],
                                             strict_mcp_config=True, max_turns=1, cwd=os.getcwd(),
                                             disallowed_tools=["Read", "Write", "Edit", "MultiEdit", "Bash", "Glob",
                                                               "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite"])
            if "tools" in {f.name for f in dataclasses.fields(nc.sdk.ClaudeAgentOptions)}:
                options.tools = []
            async for msg in nc.sdk.query(prompt=prompt, options=options):
                if isinstance(msg, nc.sdk.AssistantMessage):
                    result["text"] += "".join(b.text for b in msg.content if isinstance(b, nc.sdk.TextBlock))
                elif isinstance(msg, nc.sdk.ResultMessage):
                    usage = msg.usage or {}
                    result["input"] = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                                       "cache_creation_input_tokens"))
                    result["output"], result["cost"] = usage.get("output_tokens") or 0, msg.total_cost_usd or 0
                    if msg.is_error:
                        raise RuntimeError(msg.result or msg.subtype)
        else:
            messages = [{"role": "user", "content": prompt}]
            nc.CALL_API.set(kind)
            if kind == "ollama":
                message, usage = await nc.ollama_stream(COMPARE_SYSTEM, messages, use_tools=False)
            elif kind in nc.API_PROVIDERS:
                message, usage = await nc.openrouter_stream(COMPARE_SYSTEM, messages, use_tools=False)
            else:
                nc.st.openrouter_key = nc.st.openrouter_key or os.environ.get("OPENROUTER_API_KEY") or \
                    nc.st.config.get("openrouter_api_key")
                if not nc.st.openrouter_key:
                    raise RuntimeError("no OpenRouter key (nanocode setup)")
                message, usage = await nc.openrouter_stream(COMPARE_SYSTEM, messages, use_tools=False)
            result["text"] = message.get("content") or ""
            result.update(input=usage.get("input") or 0, output=usage.get("output") or 0, cost=usage.get("cost") or 0)
    except Exception as e:  # one model failing doesn't stop the others
        result["error"] = str(e) or type(e).__name__
    result["seconds"] = time.time() - started
    return result


def render_compare(results):
    """Columns when they fit (up to 3, at least ~30 characters each), otherwise one under the other."""
    from rich.markdown import Markdown as Md
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    width = max(40, nc.messages_width() - 3)
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=width)

    def cell(r):
        foot = Text(f"{nc.fmt_duration(r['seconds'])} · {nc.fmt_tokens(r['input'])} in · {nc.fmt_tokens(r['output'])} out"
                    + (f" · ${r['cost']:.4f}" if r["cost"] else ""), style=nc.THEME["muted"])
        body = Text(f"error: {r['error']}", style=nc.THEME["error"]) if r["error"] else Md(r["text"] or "(empty)")
        from rich.console import Group
        return Group(body, Text(""), foot)

    side = len(results) <= 3 and width // len(results) >= 30
    if side:
        table = Table(box=None, expand=True, show_edge=False, padding=(0, 2, 0, 0))
        for r in results:
            table.add_column(Text(nc.pretty_model(r["model"]), style=f"bold {nc.THEME['accent']}"), ratio=1)
        table.add_row(*[cell(r) for r in results])
        console.print(table)
    else:
        for r in results:
            console.print(Panel(cell(r), title=Text(nc.pretty_model(r["model"]), style=f"bold {nc.THEME['accent']}"),
                                title_align="left", border_style=nc.THEME["border"]))
    return "\n".join("   " + line for line in buf.getvalue().rstrip("\n").split("\n"))


async def cmd_compare(arg):
    if not arg:
        models = compare_models()
        nc.out(f"{nc.BOLD}Compare{nc.RESET} {nc.DIM}(/compare <prompt>){nc.RESET}\n  models: " + ", ".join(models) +
            f"\n  {nc.DIM}change: /compare models <a>, <b>, <c>  ·  claude:<model> and ollama:<model> work too{nc.RESET}\n"
            f"  {nc.DIM}only your prompt (and files you attach with @) is sent: no chat history or memory{nc.RESET}\n")
        return
    if arg.startswith("models"):
        chosen = [m.strip() for m in arg[len("models"):].replace(",", " ").split() if m.strip()]
        if not chosen:
            nc.st.config.pop("compare_models", None)
        else:
            nc.st.config["compare_models"] = chosen[:6]
        nc.save_config()
        nc.out(f"{nc.GREEN}⏺ Compare models:{nc.RESET} {', '.join(compare_models())}\n")
        return
    models = compare_models()
    if len(models) < 2:
        nc.out(f"{nc.RED}⏺ pick at least two models: /compare models <a>, <b>{nc.RESET}\n")
        return
    nc.out(nc.user_block(arg))
    nc.out(f"   {nc.fg('muted')}comparing {len(models)} models: {', '.join(models)}{nc.RESET}")
    prompt = nc.expand_mentions(nc.expand_chips(arg, []))
    results = await asyncio.gather(*(compare_one(m, prompt) for m in models))
    for r in results:
        nc.st.usage["input"] += r["input"]
        nc.st.usage["output"] += r["output"]
        nc.st.usage["cost"] += r["cost"] or 0
    nc.out(render_compare(results) + "\n")
