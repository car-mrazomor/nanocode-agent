"""nanocode as a GitHub agent: mention /nanocode (or /nc) in an issue or pull request comment and it does the work.

`nanocode github install` writes .github/workflows/nanocode.yml. On a comment with /nanocode, GitHub Actions
installs nanocode and runs `nanocode github run`, which reads the event, gives the issue (or pull request) and the
comment to the agent, and then:
- on an issue, if files changed: pushes a new branch and opens a pull request; otherwise answers in a comment;
- on a pull request: commits to its branch (when it's in this repository) and answers in a comment.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import time
from pathlib import Path

import nanocode as nc
from nanokit import cli

TRIGGER = re.compile(r"(?<![\w/])/(?:nanocode|nc)\b", re.I)
MAX_COMMENT = 60_000
MAX_DIFF = 40_000
BOT_NAME, BOT_EMAIL = "nanocode[bot]", "41898282+github-actions[bot]@users.noreply.github.com"
REPO = "car-mrazomor/nanocode-agent"
# Environment variables that are credentials. The agent's commands never see them (see scrub_env).
SECRET_ENV = re.compile(r"^(?:GH_TOKEN|GITHUB_TOKEN)$|(?:_API_KEY|_TOKEN|_SECRET|_PASSWORD)$", re.I)
TOKENS = {}   # the credentials taken out of os.environ: only nanocode's own `gh` and `git push` get GitHub's back
GITHUB_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
TOOLS = {}    # "git"/"gh" -> full path, found before the agent runs (it could put its own `gh` on the PATH)

# Two jobs, so the agent never runs next to a token that can write: `agent` (read-only token, the model's key)
# runs nanocode and uploads its answer and its changes as a patch; `publish` (write token, no model, no agent)
# applies the patch, pushes and comments.
WORKFLOW = """\
# nanocode: comment /nanocode (or /nc) on an issue or a pull request to have nanocode work on it.
# Made by `nanocode github install`. Needs the OPENROUTER_API_KEY secret (Settings → Secrets → Actions).
# nanocode is pinned to v{version}: change NANOCODE_VERSION to upgrade.
name: nanocode

on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

permissions: {{}}

env:
  NANOCODE_VERSION: "{version}"

jobs:
  agent:
    # Only people who can push to the repository can start it.
    if: >-
      (contains(github.event.comment.body, '/nanocode') || contains(github.event.comment.body, '/nc')) &&
      contains(fromJSON('["OWNER", "MEMBER", "COLLABORATOR"]'), github.event.comment.author_association)
    runs-on: ubuntu-latest
    timeout-minutes: 30
    permissions:
      contents: read
      pull-requests: read
      issues: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          persist-credentials: false
      - name: Install nanocode
        run: |
          mkdir -p "$RUNNER_TEMP/nanocode"
          curl -fsSL "https://github.com/{repo}/archive/refs/tags/v$NANOCODE_VERSION.tar.gz" | tar -xz --strip-components=1 -C "$RUNNER_TEMP/nanocode"
          python3 -m venv "$RUNNER_TEMP/venv"
          "$RUNNER_TEMP/venv/bin/pip" install --quiet "httpx>=0.27" "rich>=13" "prompt_toolkit>=3.0.40" "websockets>=12"
      - name: Run nanocode
        env:
          OPENROUTER_API_KEY: ${{{{ secrets.OPENROUTER_API_KEY }}}}
          NANOCODE_MODEL: ${{{{ vars.NANOCODE_MODEL }}}}
          GH_TOKEN: ${{{{ github.token }}}}
        run: '"$RUNNER_TEMP/venv/bin/python" "$RUNNER_TEMP/nanocode/nanocode.py" github run --agent-only "$RUNNER_TEMP/nanocode-out"'
      - uses: actions/upload-artifact@v4
        with:
          name: nanocode-out
          path: ${{{{ runner.temp }}}}/nanocode-out
          include-hidden-files: true

  publish:
    needs: agent
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          persist-credentials: false
      - name: Install nanocode
        run: |
          mkdir -p "$RUNNER_TEMP/nanocode"
          curl -fsSL "https://github.com/{repo}/archive/refs/tags/v$NANOCODE_VERSION.tar.gz" | tar -xz --strip-components=1 -C "$RUNNER_TEMP/nanocode"
          python3 -m venv "$RUNNER_TEMP/venv"
          "$RUNNER_TEMP/venv/bin/pip" install --quiet "httpx>=0.27" "rich>=13" "prompt_toolkit>=3.0.40" "websockets>=12"
      - uses: actions/download-artifact@v4
        with:
          name: nanocode-out
          path: ${{{{ runner.temp }}}}/nanocode-out
      - name: Publish
        env:
          GH_TOKEN: ${{{{ github.token }}}}
        run: '"$RUNNER_TEMP/venv/bin/python" "$RUNNER_TEMP/nanocode/nanocode.py" github run --publish "$RUNNER_TEMP/nanocode-out"'
"""


def workflow():
    return WORKFLOW.format(version=nc.__version__, repo=REPO)


def run(cmd, input=None, check=False):
    """(exit code, output) of a command; the output has stderr too. `gh` and `git push` get the GitHub token that
    scrub_env took out of the environment; nothing else does."""
    cmd = [TOOLS.get(cmd[0], cmd[0]), *cmd[1:]]
    name = os.path.basename(cmd[0]).lower().removesuffix(".exe")
    env = None
    if TOKENS and (name == "gh" or (name == "git" and "push" in cmd)):
        env = {**os.environ, **{k: v for k, v in TOKENS.items() if k in GITHUB_VARS}}
    try:
        proc = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=300,
                              encoding="utf-8", errors="replace", env=env)
    except (OSError, subprocess.SubprocessError) as e:
        if check:
            raise RuntimeError(f"{name} failed: {e}")
        return 1, str(e)
    output = (proc.stdout or "") + (proc.stderr or "")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{name} {' '.join(cmd[1:3])} failed: {redact(output.strip()[-500:])}")
    return proc.returncode, output


def scrub_env():
    """Takes every credential out of os.environ, so the agent's commands (bash, shells, MCP servers) can't read
    them. The model's key moves into the in-memory config (never saved), which only nanocode itself reads."""
    for name in list(os.environ):
        if SECRET_ENV.search(name):
            TOKENS[name] = os.environ.pop(name)
    config = nc.st.config
    if TOKENS.get("OPENROUTER_API_KEY"):
        config["openrouter_api_key"] = TOKENS["OPENROUTER_API_KEY"]
    for provider in nc.API_PROVIDERS.values():
        value = next((TOKENS[v] for v in getattr(provider, "env", ()) if TOKENS.get(v)), None)
        if value:
            config.setdefault("api_keys", {}).setdefault(provider.name, value)


def redact(text):
    """Text that goes to GitHub, without any credential nanocode knows (environment and config)."""
    from nanokit.share import redact_text
    return redact_text(text, extra=[*TOKENS.values(), nc.st.openrouter_key or ""])[0]


def resolve_tools():
    for name in ("git", "gh"):
        TOOLS[name] = shutil.which(name) or name


# --- nanocode github install ---

def install(argv):
    root = Path.cwd()
    code, top = run(["git", "rev-parse", "--show-toplevel"])
    if code == 0 and top.strip():
        root = Path(top.strip())
    path = root / ".github" / "workflows" / "nanocode.yml"
    if path.exists() and "--force" not in argv:
        print(f"{nc.tilde(path)} already exists (nanocode github install --force replaces it)")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(workflow(), encoding="utf-8")
        print(f"wrote {path}")
    print("\nNext:\n"
          "  1. Add the secret OPENROUTER_API_KEY to the repository: Settings → Secrets and variables → Actions\n"
          "     (or: gh secret set OPENROUTER_API_KEY)\n"
          "  2. Optional: the variable NANOCODE_MODEL picks the model (e.g. anthropic/claude-sonnet-5)\n"
          "  3. Commit and push the workflow, then comment \"/nanocode fix this\" on an issue or pull request")
    return 0


# --- nanocode github run ---

def load_event():
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise RuntimeError("GITHUB_EVENT_PATH isn't set: this runs inside GitHub Actions")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def describe(event):
    """What the comment is on: {"number", "is_pr", "title", "body", "comment", "author", "path", "hunk"}."""
    comment = event.get("comment") or {}
    item = event.get("pull_request") or event.get("issue") or {}
    is_pr = bool(event.get("pull_request") or (event.get("issue") or {}).get("pull_request"))
    return {"number": item.get("number"), "is_pr": is_pr, "title": item.get("title") or "",
            "body": item.get("body") or "", "comment": comment.get("body") or "",
            "author": (comment.get("user") or {}).get("login") or "someone",
            "path": comment.get("path"), "hunk": comment.get("diff_hunk")}


def build_prompt(info, repo, read_only=False, diff=""):
    kind = "pull request" if info["is_pr"] else "issue"
    request = TRIGGER.sub("", info["comment"]).strip() or f"Work on this {kind}."
    if read_only:
        task = ("This pull request comes from a fork, so you're read-only: you can't run commands or change files. "
                "Its changes are below; the checkout is the base branch. Answer their request in a short comment.")
    else:
        task = ("Do what they ask. If it needs code changes, make them (they'll be committed and pushed for you: "
                "don't run git commit or push). Finish with a short answer for the comment: what you did or found.")
    parts = [f"You're running in GitHub Actions on a checkout of {repo or 'the repository'}, asked by "
             f"@{info['author']} in a comment on {kind} #{info['number']}. {task}",
             f"# {kind.capitalize()} #{info['number']}: {info['title']}", info["body"].strip() or "(no description)"]
    if diff:
        cut = diff if len(diff) <= MAX_DIFF else diff[:MAX_DIFF] + "\n… (cut)"
        parts.append(f"The pull request's changes:\n```diff\n{cut}\n```")
    if info["path"]:
        parts.append(f"The comment is on {info['path']}:\n```diff\n{info['hunk'] or ''}\n```")
    parts.append(f"# The request from @{info['author']}\n{request}")
    return "\n\n".join(parts)


def pick_provider():
    """The provider for CI from the environment: OpenRouter with OPENROUTER_API_KEY, else NANOCODE_PROVIDER."""
    provider = os.environ.get("NANOCODE_PROVIDER")
    if not provider:
        provider = "openrouter" if os.environ.get("OPENROUTER_API_KEY") else \
            "ollama" if os.environ.get("OLLAMA_HOST") else None
    if os.environ.get("NANOCODE_MODEL"):
        os.environ["MODEL"] = os.environ["NANOCODE_MODEL"]
    return provider


def changed_files():
    code, output = run(["git", "status", "--porcelain", "--", ".", ":(exclude).nanocode"])
    return [line[3:] for line in output.splitlines() if line.strip()] if code == 0 else []


def pr_details(number):
    """{"isCrossRepository", "headRefName"} of a pull request; a fork's pull request when unsure."""
    code, output = run(["gh", "pr", "view", str(number), "--json", "isCrossRepository,headRefName"])
    try:
        data = json.loads(output[output.index("{"):output.rindex("}") + 1]) if code == 0 else {}
    except ValueError:
        data = {}
    return {"isCrossRepository": data.get("isCrossRepository", True), "headRefName": data.get("headRefName")}


def checkout_pr(info):
    """Before the agent runs: (read_only, diff). A pull request from this repository is checked out to work on;
    one from a fork isn't (its code isn't run): the agent only reads its diff and answers."""
    if not info["is_pr"]:
        return False, ""
    if pr_details(info["number"])["isCrossRepository"]:
        _, diff = run(["gh", "pr", "diff", str(info["number"])])
        return True, diff
    run(["gh", "pr", "checkout", str(info["number"])], check=True)
    return False, ""


async def agent_answer(prompt, read_only=False):
    """Runs one headless turn (edits and commands allowed unless read_only); returns (answer, error)."""
    texts = [""]

    def sink(kind, data):
        if kind == "delta":
            texts[-1] += data["text"]
        elif kind in ("text_end", "tool.started"):
            texts.append("")
    token = nc.EVENTS.set(sink)
    if read_only:
        nc.TOOL_FILTER.set(lambda t: nc.tool_is_read_only(t) and not t.get("mcp"))
    try:
        await nc.run_headless(prompt)
    finally:
        nc.EVENTS.reset(token)
    answer = next((t.strip() for t in reversed(texts) if t.strip()), "")
    return answer, nc.st.last_error


def comment(info, body):
    body = redact(body)
    body = body if len(body) <= MAX_COMMENT else body[:MAX_COMMENT] + "\n\n… (cut)"
    which = "pr" if info["is_pr"] else "issue"
    run(["gh", which, "comment", str(info["number"]), "--body-file", "-"], input=body, check=True)


def git_push(*args):
    """git push with the job's token through gh, ignoring any hooks and credential helpers in the checkout."""
    empty = tempfile.mkdtemp(prefix="nanocode-nohooks-")
    gh = TOOLS.get("gh", "gh").replace("\\", "/")
    return run(["git", "-c", f"core.hooksPath={empty}", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
                "-c", f'credential.helper=!"{gh}" auth git-credential', "push", *args])


def publish(info, answer, files):
    """Commits the changes: to the pull request's branch, or a new branch with a new pull request."""
    answer = redact(answer)
    run(["git", "config", "user.name", BOT_NAME])
    run(["git", "config", "user.email", BOT_EMAIL])
    title = f"nanocode: {info['title']}"[:72] if info["title"] else f"nanocode: #{info['number']}"
    no_hooks = ["-c", f"core.hooksPath={tempfile.mkdtemp(prefix='nanocode-nohooks-')}"]
    if info["is_pr"]:
        run(["git", "add", "-A", "--", ".", ":(exclude).nanocode"], check=True)
        run(["git", *no_hooks, "commit", "-m", title, "-m", f"Requested by @{info['author']} in #{info['number']}."],
            check=True)
        code, output = git_push()
        note = "Pushed to this pull request" if code == 0 else \
            f"Couldn't push to this pull request's branch: {output.strip()[-300:]}"
        comment(info, f"{answer}\n\n---\n{note}: {', '.join(f'`{f}`' for f in files[:20])}")
        return None
    branch = f"nanocode/issue-{info['number']}-{time.strftime('%Y%m%d%H%M%S')}"
    run(["git", "checkout", "-b", branch], check=True)
    run(["git", "add", "-A", "--", ".", ":(exclude).nanocode"], check=True)
    run(["git", *no_hooks, "commit", "-m", title, "-m", f"Closes #{info['number']}"], check=True)
    code, output = git_push("-u", "origin", branch)
    if code != 0:
        raise RuntimeError(f"git push failed: {redact(output.strip()[-500:])}")
    body = redact(f"{answer}\n\nCloses #{info['number']} (requested by @{info['author']}).")
    _, output = run(["gh", "pr", "create", "--head", branch, "--title", title, "--body-file", "-"],
                    input=body[:MAX_COMMENT], check=True)
    url = next(iter(re.findall(r"https://github\.com/\S+/pull/\d+", output)), "")
    comment(info, f"Opened a pull request: {url or branch}\n\n{answer}")
    return url


def work(info):
    """Checks out, runs the agent; returns (answer, failure, read_only). Credentials leave os.environ first."""
    resolve_tools()
    run(["gh", "auth", "setup-git"])              # fetches (gh pr checkout) go through gh, with the token
    read_only, diff = checkout_pr(info)
    error = nc.apply_config(pick_provider())
    if error:
        raise RuntimeError(f"{error}\n(add the OPENROUTER_API_KEY secret, see `nanocode github install`)")
    scrub_env()
    nc.st.auto_approve = not read_only         # a throwaway checkout; dangerous commands are still refused
    prompt = build_prompt(info, os.environ.get("GITHUB_REPOSITORY"), read_only, diff)
    try:
        answer, failure = asyncio.run(agent_answer(prompt, read_only))
    except Exception as e:
        answer, failure = "", str(e)
    if failure:
        answer = f"{answer}\n\nnanocode hit an error: {failure}".strip()
    return redact(answer or "Done."), failure, read_only


def agent_only(info, out):
    """The `agent` job: writes answer.md and changes.patch to `out` for the `publish` job."""
    answer, failure, read_only = work(info)
    out.mkdir(parents=True, exist_ok=True)
    (out / "answer.md").write_text(answer, encoding="utf-8")
    patch = out / "changes.patch"
    patch.write_text("", encoding="utf-8")
    if not read_only and changed_files():
        run(["git", "add", "-A", "--", ".", ":(exclude).nanocode"], check=True)
        run(["git", "diff", "--cached", "--binary", f"--output={patch}"], check=True)
    print(f"wrote {out}" + (" (with changes)" if patch.stat().st_size else ""))
    return 0


def publish_only(info, out):
    """The `publish` job: no agent runs here. Applies the agent's patch, pushes, comments."""
    resolve_tools()
    run(["gh", "auth", "setup-git"])
    scrub_env()
    try:
        answer = (out / "answer.md").read_text(encoding="utf-8").strip() or "Done."
    except OSError as e:
        raise RuntimeError(f"no answer from the agent job: {e}")
    patch = out / "changes.patch"
    has_patch = patch.is_file() and patch.stat().st_size > 0
    if info["is_pr"] and pr_details(info["number"])["isCrossRepository"]:
        has_patch = False                      # a fork's pull request: answer only, nothing to push to
    elif info["is_pr"] and has_patch:
        run(["gh", "pr", "checkout", str(info["number"])], check=True)
    if has_patch:
        run(["git", "apply", "--index", str(patch)], check=True)
    files = changed_files() if has_patch else []
    return answer, files


def run_cli(argv):
    TOKENS.clear()
    try:
        event = load_event()
    except (RuntimeError, OSError, ValueError) as e:
        print(f"nanocode github run: {e}", file=sys.stderr)
        return 2
    info = describe(event)
    if not info["number"] or not TRIGGER.search(info["comment"]):
        print("nothing to do: the comment doesn't mention /nanocode or /nc")
        return 0
    mode = argv[0] if argv[:1] in (["--agent-only"], ["--publish"]) else None
    if mode and len(argv) < 2:
        print(f"usage: nanocode github run {mode} <dir>", file=sys.stderr)
        return 2
    failure = None
    try:
        if mode == "--agent-only":
            return agent_only(info, Path(argv[1]))
        if mode == "--publish":
            answer, files = publish_only(info, Path(argv[1]))
        else:
            answer, failure, read_only = work(info)
            files = [] if read_only else changed_files()
        if files:
            url = publish(info, answer, files)
            print(f"changed {len(files)} file(s)" + (f": {url}" if url else ""))
        else:
            comment(info, answer)
            print("answered in a comment")
    except RuntimeError as e:
        print(f"nanocode github run: {redact(str(e))}", file=sys.stderr)
        return 1
    return 1 if failure else 0


@cli("github", "GitHub agent: `github install` adds the workflow, `github run` runs it in Actions")
def github_cli(argv):
    if argv[:1] == ["install"]:
        return install(argv[1:])
    if argv[:1] == ["run"]:
        return run_cli(argv[1:])
    print("usage: nanocode github install [--force] | nanocode github run [--agent-only DIR | --publish DIR]",
          file=sys.stderr)
    return 2
