"""Plan and build modes, like OpenCode's (Shift+Tab switches).

In plan mode the model explores and plans but can't change anything: tools that write files, send email or
save memory are refused with a note that it's in plan mode, and commands (bash, shell) always ask first, even
with /auto on. The system prompt tells it to finish with a plan. Build mode is the usual. The mode belongs to
the tab (each chat has its own); /plan and /build switch it, and so does Shift+Tab.
"""
from __future__ import annotations

import nanocode as nc
from nanokit import command, on

MODES = ("build", "plan")
COMMANDS_ASK = ("bash", "shell")          # allowed in plan mode, to look around, but never without asking
ALWAYS_OK = {"todo", "diagnostics"}
PLAN_PROMPT = (
    "# Plan mode\nYou are in plan mode: the user wants a plan before any change. Explore the code (read, glob, "
    "grep, read-only commands) and ask questions if something important is unclear, but don't edit or write "
    "files or run commands that change anything; those tools are refused. End with a concise, concrete plan: "
    "the files to change and how, in order, and the risks or open questions. The user switches to build mode "
    "(/build or Shift+Tab) when they want you to carry it out.")
DENIED = ("you're in plan mode, so {tool} isn't available: don't change anything yet. Finish exploring, then "
          "present your plan; the user switches to build mode (/build or Shift+Tab) to carry it out.")


def current_mode(tab=None):
    """'plan' or 'build' for a tab (by default the one whose turn is running, or the one on screen)."""
    mode = getattr(tab or nc.st.current_tab(), "mode", "build")
    return mode if mode in MODES else "build"


def set_mode(mode, tab=None):
    (tab or nc.st.current_tab()).mode = mode
    nc.invalidate()


def _read_only(name):
    if name in ALWAYS_OK:
        return True
    entry = nc.TOOLS.get(name) or nc.MCP_TOOLS.get(name)
    return entry is None or nc.tool_is_read_only(entry)     # sub-agents count when they can't edit


@on("before_tool")
def plan_guard(name, args):
    if current_mode() == "plan" and name not in COMMANDS_ASK and not _read_only(name):
        return {"deny": DENIED.format(tool=name)}
    return None


@on("permission")
def plan_asks(tool, preview):
    return "ask" if current_mode() == "plan" and tool in COMMANDS_ASK else None


@on("system_prompt")
def plan_prompt():
    return PLAN_PROMPT if current_mode() == "plan" else None


def _switched(mode):
    if mode == "plan":
        return (f"{nc.fg('warning')}⏺ Plan mode{nc.RESET} {nc.DIM}the model explores and plans; no edits, and "
                f"commands ask first · /build or Shift+Tab to carry it out{nc.RESET}")
    return f"{nc.fg('success')}⏺ Build mode{nc.RESET} {nc.DIM}the model can edit files and run commands{nc.RESET}"


@command("/plan", "plan mode: the model explores and plans without changing anything (Shift+Tab)")
async def plan(arg):
    set_mode("plan")
    nc.out(_switched("plan") + "\n")
    if arg:
        await nc.run_input(arg)


@command("/build", "build mode: the model edits files and runs commands (Shift+Tab)")
async def build(arg):
    set_mode("build")
    nc.out(_switched("build") + "\n")
    if arg:
        await nc.run_input(arg)


def toggle(tab=None):
    tab = tab or nc.st.active_tab
    mode = "build" if current_mode(tab) == "plan" else "plan"
    set_mode(mode, tab)
    return mode


@on("keybindings")
def keys(kb, ui):
    try:
        from prompt_toolkit.filters import has_completions
    except ImportError:
        return

    @kb.add("s-tab", filter=~has_completions)
    def _(event):
        mode = toggle()
        nc.show_toast("the model explores and plans, no edits" if mode == "plan" else
                      "the model can edit files and run commands", seconds=2,
                      variant="warning" if mode == "plan" else "success", title=f"{mode.capitalize()} mode")


nc.KEYS_HELP.append(("Shift+Tab", "switch between build and plan mode"))
