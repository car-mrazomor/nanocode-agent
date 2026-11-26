import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import agents  # noqa: E402
from test_nanocode import call, mock_http, plain, run, sse, tool_call_chunk  # noqa: E402

REVIEWER = """---
description: Reviews code for bugs
model: x/reviewer-model
tools:
  - read
  - grep
---
You are a strict reviewer. Say REVIEWED.
"""


def write_agent(root, name, text):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(text)


def api_backend(monkeypatch, replies):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "main/model", "k"
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return sse(replies[min(len(requests), len(replies)) - 1])

    mock_http(monkeypatch, handler)
    return requests


def test_list_agents_project_wins_and_frontmatter(sandbox):
    write_agent(nc.home_dir() / "agents", "reviewer", "---\ndescription: global one\n---\nglobal prompt")
    write_agent(nc.project_dir() / "agents", "reviewer", REVIEWER)
    write_agent(nc.project_dir() / "agents", "Bad Name", "---\ndescription: x\n---\nbody")
    write_agent(nc.project_dir() / "agents", "empty", "---\ndescription: no body\n---\n")
    found = agents.list_agents()
    assert list(found) == ["reviewer"]
    info = found["reviewer"]
    assert info["description"] == "Reviews code for bugs" and info["model"] == "x/reviewer-model"
    assert info["tools"] == ["read", "grep"] and "strict reviewer" in info["prompt"]
    assert agents.as_list("[read, grep glob]") == ["read", "grep", "glob"]


def test_tool_filter_limits_tools_and_blocks_nesting():
    only = agents.tool_filter(["read", "mcp__gh__*", "agent"])
    assert only({"name": "read"}) and only({"name": "mcp__gh__issues"})
    assert not only({"name": "bash"}) and not only({"name": "agent"})
    default = agents.tool_filter(None)
    assert default({"name": "grep"}) and not default({"name": "run_agent"}) and not default({"name": "edit"})


def test_system_prompt_lists_agents(sandbox):
    assert "Custom agents" not in nc.build_system_prompt()
    write_agent(nc.project_dir() / "agents", "reviewer", REVIEWER)
    assert "- reviewer: Reviews code for bugs" in nc.build_system_prompt()


def test_run_agent_tool_uses_its_prompt_model_and_tools(sandbox, monkeypatch):
    write_agent(nc.project_dir() / "agents", "reviewer", REVIEWER)
    requests = api_backend(monkeypatch, [[{"choices": [{"delta": {"content": "REVIEWED: all good"}}]}]])
    text, err = call("run_agent", agent="reviewer", task="check app.py")
    assert not err and text == "REVIEWED: all good"
    body = requests[0]
    assert body["model"] == "x/reviewer-model"
    assert "strict reviewer" in body["messages"][0]["content"]
    assert sorted(t["function"]["name"] for t in body["tools"]) == ["grep", "read"]
    text, err = call("run_agent", agent="nobody", task="x")
    assert err and "no agent named 'nobody'" in text


def test_at_mention_runs_the_agent_directly(sandbox, monkeypatch):
    write_agent(nc.project_dir() / "agents", "reviewer", REVIEWER)
    Path("app.py").write_text("print(1)\n")
    requests = api_backend(monkeypatch, [
        [tool_call_chunk("c1", "read", {"path": "app.py"})],
        [{"choices": [{"delta": {"content": "REVIEWED app.py"}}]}]])
    run(nc.run_input("@reviewer look at app.py"))
    screen = plain(nc.st.output)
    assert "REVIEWED app.py" in screen and "reviewer: 2 steps" in screen
    assert requests[0]["messages"][-1]["content"] == "look at app.py"
    assert [m["role"] for m in nc.st.messages] == ["user", "assistant"]   # the main chat knows
    assert nc.st.session["title"].startswith("@reviewer")


def test_at_mention_of_unknown_agent_is_a_normal_message(sandbox):
    assert run(agents.mention("@nobody hello")) is None
    write_agent(nc.project_dir() / "agents", "src", REVIEWER)
    Path("src").mkdir()
    assert run(agents.mention("@src fix it")) is None             # a folder, not the agent


def test_agents_command_new_and_list(sandbox):
    run(agents.agents_command("new reviewer"))
    path = nc.project_dir() / "agents" / "reviewer.md"
    assert path.exists() and "description:" in path.read_text()
    run(agents.agents_command("new reviewer"))
    assert "already exists" in plain(nc.st.output)
    run(agents.agents_command("new other --global"))
    assert (nc.home_dir() / "agents" / "other.md").exists()
    run(agents.agents_command(""))
    screen = plain(nc.st.output)
    assert "@reviewer" in screen and "@other" in screen
    assert set(agents.list_agents()) == {"reviewer", "other"}
