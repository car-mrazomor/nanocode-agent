"""Permission rules: allow, ask or deny a tool by what it's about to do, like OpenCode's "permission" setting.

    "permission": {
      "bash": {"git status*": "allow", "git push*": "ask", "rm -rf*": "deny", "*": "ask"},
      "edit": "allow",
      "write": {"*.md": "allow"},
      "web_fetch": "allow"
    }

A rule is either one action for the whole tool, or glob patterns matched against what the permission question
would show: the command for bash and shell, the path for edit and write, the URL for web_fetch. When several
patterns match, the longest (most specific) one wins. A "write" without rules of its own follows "edit", and
"*" as a tool name is the fallback for every tool.

A bash command made of several commands (`a && b`, `a; b`, `a | b`) is allowed only if every part is: "git
status*" mustn't let `git status && rm -rf ~` through. A command with redirects (`>`, `<`, `&>`), command or
process substitution (`$(…)`, backticks, `<(…)`), variables or subshells is never allowed by a rule (it asks),
since its parts can do things the patterns don't show; "deny" and "ask" rules still apply to every part. Paths
are matched normalized (`src/../x` is `x`), relative to the project inside it and absolute outside it, so a
relative pattern like "src/*" never matches a file outside the project. The core still asks for commands that
look destructive and for sending email, whatever the rules say.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex

import nanocode as nc
from nanokit import command, on

ACTIONS = ("allow", "ask", "deny")
SHELLS = ("bash", "shell")
SPLIT = re.compile(r"&&|\|\||[;|\r\n&]|`|[$<>]\(|[()]")  # between the commands of a shell line
# Harmless redirects: copying a descriptor (2>&1, >&2) or discarding output. Taken out before splitting, so the
# "&" of 2>&1 isn't a command separator; any other redirect makes the line "not plain".
HARMLESS = re.compile(r"(?<![<>&])[0-9]*>&(?:[0-9]+|-)(?=$|[\s;|&)])|(?<![<>&])[0-9]*>>?\s*/dev/null(?=$|[\s;|&)])")
SPECIAL = set("<>$`(){}")  # redirects, substitution, variables, subshells, brace groups


def plain(line):
    """True when a shell line is only words, quoted words and command separators: nothing a pattern on its
    words can't see (redirects, $(…), `…`, <(…), $VAR, subshells)."""
    quote, escaped = None, False
    for c in line:
        if escaped:
            escaped = False
        elif quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            escaped = True
        elif quote == '"':
            if c == '"':
                quote = None
            elif c in "$`":
                return False
        elif c in "'\"":
            quote = c
        elif c in SPECIAL:
            return False
    return quote is None and not escaped


def rules():
    value = nc.st.config.get("permission")
    return value if isinstance(value, dict) else {}


def _absolute(pattern):
    return pattern.startswith(("/", "~", "\\")) or os.path.isabs(pattern)


def _match(rule, subjects, outside=False):
    """The action for one rule (a string, or {pattern: action}) and these subjects, or None. For a path outside
    the project, a relative pattern ("src/*", "*.py") can't allow it: only "*" and absolute patterns can."""
    if isinstance(rule, str):
        return rule if rule in ACTIONS else None
    if not isinstance(rule, dict):
        return None
    best = None
    for pattern, action in rule.items():
        if not isinstance(pattern, str):
            continue
        if outside and action == "allow" and pattern != "*" and not _absolute(pattern):
            continue
        glob = os.path.expanduser(pattern) if pattern.startswith("~") else pattern
        if action in ACTIONS and any(fnmatch.fnmatchcase(s, glob) for s in subjects):
            if best is None or len(pattern) > len(best[0]):
                best = (pattern, action)
    return best and best[1]


PATH_TOOLS = ("edit", "write", "read")


def _path(preview):
    """(normalized path: relative inside the project, absolute outside it; whether it's outside). Symlinks are
    followed, so a link inside the project to a file outside it counts as outside, and `..` can't escape."""
    path = os.path.realpath(os.path.abspath(nc.resolve(preview.strip())))
    root = os.path.realpath(os.getcwd())
    if os.path.normcase(path) == os.path.normcase(root):
        return ".", False
    prefix = root.rstrip(os.sep) + os.sep
    if os.path.normcase(path).startswith(os.path.normcase(prefix)):
        return os.path.normpath(path[len(prefix):]), False
    return path, True


def _subjects(tool, preview):
    """The text(s) a pattern is matched against: for a path, the normalized one (with / and the OS separator)."""
    if tool in PATH_TOOLS:
        path, _ = _path(preview)
        return list(dict.fromkeys([path.replace(os.sep, "/"), path]))
    return [preview.strip()]


def decide(tool, preview):
    """'allow', 'ask', 'deny', or None when no rule applies."""
    table = rules()
    rule = table.get(tool)
    if rule is None and tool == "write":
        rule = table.get("edit")
    if rule is None:
        rule = table.get("*")
    if rule is None:
        return None
    if tool in PATH_TOOLS:
        return _match(rule, _subjects(tool, preview), outside=_path(preview)[1])
    if tool not in SHELLS or isinstance(rule, str):
        return _match(rule, _subjects(tool, preview))
    line = HARMLESS.sub(" ", preview)
    parts = [p.strip() for p in SPLIT.split(line) if p.strip()] or [line.strip()]
    answers = [_match(rule, [part]) for part in parts]
    for action in ("deny", "ask"):
        if action in answers:
            return action
    if not plain(line):
        return None                    # redirects or substitutions: no rule allows it, the usual question
    return "allow" if all(a == "allow" for a in answers) else None   # a part without a rule: the usual way


@on("permission")
def permission(tool, preview):
    return decide(tool, preview)


def _show():
    table = rules()
    lines = [f"{nc.BOLD}Permission rules{nc.RESET}"]
    if not table:
        lines.append(f"  {nc.DIM}none yet: every tool follows /auto (ask, or allow){nc.RESET}")
    color = {"allow": nc.fg("success"), "ask": nc.fg("warning"), "deny": nc.fg("error")}
    for tool, rule in table.items():
        if isinstance(rule, dict):
            lines.append(f"  {nc.CYAN}{tool}{nc.RESET}")
            for pattern, action in sorted(rule.items(), key=lambda kv: -len(kv[0])):
                lines.append(f"    {color.get(action, '')}{action:<6}{nc.RESET} {pattern}")
        else:
            lines.append(f"  {nc.CYAN}{tool}{nc.RESET}  {color.get(rule, '')}{rule}{nc.RESET}")
    lines += ["", f"  {nc.DIM}add: /permissions allow bash \"npm test*\" · /permissions deny edit \"*.lock\" · "
                  f"/permissions allow web_fetch{nc.RESET}",
              f"  {nc.DIM}remove: /permissions remove bash \"npm test*\" · the longest matching pattern wins; "
              f"risky commands and sending email always ask{nc.RESET}"]
    nc.out("\n".join(lines) + "\n")


@command("/permissions", "permission rules: list them, or /permissions allow|ask|deny <tool> [\"pattern\"]")
async def permissions_command(arg):
    if not arg:
        _show()
        return
    try:
        words = shlex.split(arg)
    except ValueError as e:
        nc.out(f"{nc.RED}⏺ {e}{nc.RESET}\n")
        return
    if len(words) not in (2, 3) or words[0] not in ACTIONS + ("remove",):
        nc.out(f"{nc.RED}⏺ usage:{nc.RESET} /permissions allow|ask|deny|remove <tool> [\"pattern\"]\n")
        return
    action, tool = words[0], words[1]
    pattern = words[2] if len(words) == 3 else None
    table = dict(rules())
    rule = table.get(tool)
    if action == "remove":
        if pattern is None or not isinstance(rule, dict):
            table.pop(tool, None)
        else:
            rule = {k: v for k, v in rule.items() if k != pattern}
            if rule:
                table[tool] = rule
            else:
                table.pop(tool)
    elif pattern is None:
        table[tool] = action
    else:
        rule = dict(rule) if isinstance(rule, dict) else ({"*": rule} if isinstance(rule, str) else {})
        rule[pattern] = action
        table[tool] = rule
    nc.st.config["permission"] = table
    nc.save_config()
    what = f"{tool} {pattern}" if pattern else tool
    nc.out(f"{nc.GREEN}⏺ {'Removed' if action == 'remove' else action.capitalize()}:{nc.RESET} {what} "
           f"{nc.DIM}(saved){nc.RESET}\n")
