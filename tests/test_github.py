import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import github  # noqa: E402
from test_api import fake_openrouter, text_reply  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def keep_environ():
    """`github run` takes credentials out of os.environ; put the test runner's own back."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    github.TOKENS.clear()


def event_file(tmp_path, monkeypatch, comment="/nanocode please fix the typo", pr=False):
    event = {"action": "created",
             "comment": {"id": 7, "body": comment, "user": {"login": "ivan"}, "author_association": "OWNER"},
             "issue": {"number": 12, "title": "Typo in README", "body": "It says teh.",
                       **({"pull_request": {"url": "x"}} if pr else {})}}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    return event


def fake_commands(monkeypatch, changed="", fork=False):
    calls = []

    def run(cmd, input=None, check=False):
        calls.append((cmd, input))
        if cmd[:2] == ["git", "status"]:
            return 0, changed
        if cmd[:3] == ["gh", "pr", "create"]:
            return 0, "https://github.com/me/repo/pull/13\n"
        if cmd[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"isCrossRepository": fork, "headRefName": "feature"})
        if cmd[:3] == ["gh", "pr", "diff"]:
            return 0, "--- a/x.py\n+++ b/x.py\n+print('hi')\n"
        return 0, ""
    monkeypatch.setattr(github, "run", run)
    return calls


def test_install_writes_the_workflow(sandbox, monkeypatch, capsys):
    fake_commands(monkeypatch)
    assert nc.main(["github", "install"]) == 0
    text = (sandbox / ".github" / "workflows" / "nanocode.yml").read_text()
    assert "issue_comment" in text and "pull_request_review_comment" in text and "/nanocode" in text
    assert "${{ secrets.OPENROUTER_API_KEY }}" in text and "github run" in text
    assert "persist-credentials: false" in text and f'NANOCODE_VERSION: "{nc.__version__}"' in text
    assert "/master/" not in text and "archive/refs/tags/v$NANOCODE_VERSION" in text
    agent_job = text.split("  publish:")[0]
    assert "contents: read" in agent_job and "contents: write" not in agent_job   # the agent never gets write
    assert "OPENROUTER_API_KEY" not in text.split("  publish:")[1]
    assert "OPENROUTER_API_KEY" in capsys.readouterr().out
    (sandbox / ".github" / "workflows" / "nanocode.yml").write_text("mine")
    nc.main(["github", "install"])
    assert (sandbox / ".github" / "workflows" / "nanocode.yml").read_text() == "mine"


def test_answer_as_a_comment(sandbox, tmp_path, monkeypatch):
    event_file(tmp_path, monkeypatch, "/nc what does this repo do?")
    requests = fake_openrouter(monkeypatch, [text_reply("It's a coding agent.")])
    calls = fake_commands(monkeypatch)
    assert nc.main(["github", "run"]) == 0
    prompt = requests[0]["messages"][-1]["content"]
    assert "Typo in README" in prompt and "It says teh." in prompt and "what does this repo do?" in prompt
    assert "/nc" not in prompt.split("request from")[-1]
    assert (["gh", "issue", "comment", "12", "--body-file", "-"], "It's a coding agent.") in calls


def test_changes_become_a_pull_request(sandbox, tmp_path, monkeypatch):
    event_file(tmp_path, monkeypatch)
    fake_openrouter(monkeypatch, [text_reply("Fixed the typo.")])
    calls = fake_commands(monkeypatch, changed=" M README.md\n")
    assert nc.main(["github", "run"]) == 0
    commands = [c for c, _ in calls]
    branch = next(c[3] for c in commands if c[:3] == ["git", "checkout", "-b"])
    assert branch.startswith("nanocode/issue-12-")
    assert any(c[0] == "git" and c[-4:] == ["push", "-u", "origin", branch] for c in commands)
    create = next((c, i) for c, i in calls if c[:3] == ["gh", "pr", "create"])
    assert "Closes #12" in create[1] and "Fixed the typo." in create[1]
    assert any(c[:3] == ["gh", "issue", "comment"] and "pull/13" in i for c, i in calls)


def test_pull_request_comment_works_on_its_branch(sandbox, tmp_path, monkeypatch):
    event_file(tmp_path, monkeypatch, pr=True)
    fake_openrouter(monkeypatch, [text_reply("Done.")])
    calls = fake_commands(monkeypatch, changed=" M a.py\n")
    assert nc.main(["github", "run"]) == 0
    commands = [c for c, _ in calls]
    push = next(i for i, c in enumerate(commands) if c[0] == "git" and c[-1] == "push")
    assert commands.index(["gh", "pr", "checkout", "12"]) < push
    assert any(c[:3] == ["gh", "pr", "comment"] and "Pushed" in i for c, i in calls)
    push = next(c for c in commands if "push" in c)
    assert push[0] == "git" and any(a.startswith("core.hooksPath=") for a in push)


def test_fork_pull_request_is_answered_read_only(sandbox, tmp_path, monkeypatch):
    event_file(tmp_path, monkeypatch, pr=True)
    requests = fake_openrouter(monkeypatch, [text_reply("Looks fine.")])
    calls = fake_commands(monkeypatch, changed=" M a.py\n", fork=True)
    assert nc.main(["github", "run"]) == 0
    commands = [c for c, _ in calls]
    assert ["gh", "pr", "checkout", "12"] not in commands and not any("push" in c for c in commands)
    tools = {t["function"]["name"] for t in requests[0]["tools"]}
    assert "read" in tools and "bash" not in tools and "edit" not in tools and "write" not in tools
    assert "print('hi')" in requests[0]["messages"][-1]["content"]
    assert any(c[:3] == ["gh", "pr", "comment"] and i == "Looks fine." for c, i in calls)


def test_agent_never_sees_credentials_and_answers_are_redacted(sandbox, tmp_path, monkeypatch):
    event_file(tmp_path, monkeypatch, "/nc what's in the environment?")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key-0123456789")
    monkeypatch.setenv("GH_TOKEN", "gh-token-value-0123")
    monkeypatch.setenv("SOME_SERVICE_SECRET", "svc-secret-value-0123")
    seen = {}

    async def fake_headless(prompt):
        seen["env"] = dict(os.environ)
        nc.emit("delta", text="GH is gh-token-value-0123, key sk-or-test-key-0123456789, svc-secret-value-0123")
    monkeypatch.setattr(nc, "run_headless", fake_headless)
    calls = fake_commands(monkeypatch)
    assert nc.main(["github", "run"]) == 0
    for name in ("OPENROUTER_API_KEY", "GH_TOKEN", "SOME_SERVICE_SECRET"):
        assert name not in seen["env"]
    assert nc.st.openrouter_key == "sk-or-test-key-0123456789"     # nanocode itself still has the key
    body = next(i for c, i in calls if c[:3] == ["gh", "issue", "comment"])
    assert "gh-token" not in body and "sk-or-test" not in body and "svc-secret" not in body
    assert body.count("[REDACTED]") == 3


def test_run_gives_tokens_only_to_gh_and_push(monkeypatch):
    seen = []
    monkeypatch.setattr(github.subprocess, "run",
                        lambda cmd, env=None, **kw: seen.append((cmd, env)) or
                        github.subprocess.CompletedProcess(cmd, 0, "", ""))
    github.TOKENS.update({"GH_TOKEN": "tok"})
    github.run(["gh", "pr", "view"])
    github.run(["git", "status"])
    github.run(["git", "-c", "x=y", "push"])
    assert seen[0][1]["GH_TOKEN"] == "tok" and seen[1][1] is None and seen[2][1]["GH_TOKEN"] == "tok"


def test_two_jobs_agent_then_publish(sandbox, tmp_path, monkeypatch):
    event_file(tmp_path, monkeypatch)
    fake_openrouter(monkeypatch, [text_reply("Fixed the typo.")])
    calls = fake_commands(monkeypatch, changed=" M README.md\n")
    out = tmp_path / "out"

    real_run = calls.append
    def diff_writes(cmd, input=None, check=False):     # `git diff --output=` writes the patch
        real_run((cmd, input))
        if cmd[:2] == ["git", "diff"]:
            Path(cmd[-1].split("=", 1)[1]).write_text("diff --git a/README.md b/README.md\n")
        if cmd[:2] == ["git", "status"]:
            return 0, " M README.md\n"
        return 0, "https://github.com/me/repo/pull/13\n" if cmd[:3] == ["gh", "pr", "create"] else ""
    monkeypatch.setattr(github, "run", diff_writes)
    assert nc.main(["github", "run", "--agent-only", str(out)]) == 0
    assert (out / "answer.md").read_text() == "Fixed the typo." and (out / "changes.patch").read_text()
    assert not any(c[:2] == ["gh", "issue"] or "push" in c for c, _ in calls)   # the agent job posts nothing
    calls.clear()
    assert nc.main(["github", "run", "--publish", str(out)]) == 0
    commands = [c for c, _ in calls]
    assert ["git", "apply", "--index", str(out / "changes.patch")] in commands
    assert any("push" in c for c in commands) and any(c[:3] == ["gh", "pr", "create"] for c in commands)


def test_no_trigger_or_no_event(sandbox, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert nc.main(["github", "run"]) == 2
    event_file(tmp_path, monkeypatch, "just a comment about /nca")
    calls = fake_commands(monkeypatch)
    assert nc.main(["github", "run"]) == 0 and calls == []
