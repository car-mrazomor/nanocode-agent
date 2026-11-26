import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import improve  # noqa: E402
from test_nanocode import plain, run  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    improve.TURNS.clear()
    improve.STATE.update(last=0.0, running=None)
    nc.st.config = {"self_improve": True}
    seen = []

    async def fake_subagent(task, label, prompt=None, tools=None, model=None):
        seen.append({"task": task, "prompt": prompt, "tools": tools})
        report = {"lessons": ["Run the tests with -q to keep output short"],
                  "skill": {"name": "release", "description": "Cut a release", "instructions": "1. bump"}}
        return "Here you go:\n" + json.dumps(report), {}

    monkeypatch.setattr(nc, "run_subagent", fake_subagent)
    return seen


def turn(calls=0, failures=0, text="do the thing"):
    """Runs the hooks of one turn and waits for the review, if one started."""
    async def go():
        improve.start(text)
        for i in range(calls):
            improve.count("bash", {"command": f"step {i}"}, "error: boom" if i < failures else "ok", i < failures)
        improve.end(text, None)
        if improve.STATE["running"]:
            await improve.STATE["running"]
    run(go())


def test_a_hard_turn_saves_lessons_and_a_skill(sandbox, fresh):
    turn(calls=15)
    assert len(fresh) == 1 and "## Tool calls" in fresh[0]["task"] and "step 14" in fresh[0]["task"]
    assert fresh[0]["tools"]({"name": "skill"}) and not fresh[0]["tools"]({"name": "bash"})
    assert "Run the tests with -q" in nc.project_memory_file().read_text()
    assert "release" in nc.list_skills()
    assert "learned from that turn (1 lesson, skill release)" in plain(nc.st.output)


def test_failures_trigger_and_easy_turns_dont(sandbox, fresh):
    turn(calls=3)
    assert fresh == []
    turn(calls=3, failures=2)
    assert len(fresh) == 1 and "FAILED" in fresh[0]["task"]


def test_off_by_default_and_rate_limited(sandbox, fresh):
    nc.st.config = {}
    turn(calls=20)
    assert fresh == []
    nc.st.config = {"self_improve": True}
    turn(calls=20)
    turn(calls=20)                                  # too soon after the last one
    assert len(fresh) == 1


def test_sub_agent_calls_dont_count(sandbox, fresh):
    async def go():
        improve.start("x")
        token = nc.AGENT_LABEL.set("helper")
        for _ in range(20):
            improve.count("read", {}, "ok", False)
        nc.AGENT_LABEL.reset(token)
        improve.end("x", None)
    run(go())
    assert fresh == [] and improve.STATE["running"] is None


def test_parse_bad_reports():
    assert improve.parse("no json here") == ([], None)
    assert improve.parse('{"lessons": [], "skill": {"name": "x"}}') == ([], None)


def test_saving_asks_first(sandbox, fresh, monkeypatch):
    asked = []

    async def deny(tool, preview):
        asked.append(tool)
        return False

    monkeypatch.setattr(nc, "allowed", deny)
    turn(calls=15)
    assert asked == ["remember", "save_skill"]
    assert not nc.project_memory_file().exists() and "release" not in nc.list_skills()
    assert "learned from that turn" not in plain(nc.st.output)


def test_closed_tabs_are_forgotten(sandbox, fresh):
    closed = object()
    improve.TURNS[closed] = {"calls": [], "failed": 0}
    improve.start("x")
    assert closed not in improve.TURNS and nc.st.current_tab() in improve.TURNS
