import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nanokit import trajectories  # noqa: E402

MESSAGES = [
    {"role": "user", "content": "fix the test", "images": ["a.png"]},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "nope.py"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "error: no such file"},
    {"role": "assistant", "content": "Reading.", "tool_calls": [
        {"id": "c2", "type": "function", "function": {"name": "read", "arguments": '{"path": "test.py"}'}}]},
    {"role": "tool", "tool_call_id": "c2", "content": "x" * 5000},
    {"role": "assistant", "content": "Fixed it."},
    {"role": "assistant", "content": "", "tool_calls": [      # unfinished: dropped
        {"id": "c3", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
]


def save(folder, sid, messages, backend="api"):
    root = Path(folder) / ".nanocode" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{sid}.json").write_text(json.dumps({"id": sid, "title": "t", "backend": backend, "messages": messages}))


def test_export_sharegpt_skips_sessions_without_messages(sandbox, tmp_path, capsys):
    save(".", "a", MESSAGES)
    save(".", "b", [], backend="sdk")
    out = tmp_path / "out" / "data.jsonl"
    assert trajectories.trajectories_cli(["export", "--out", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 1
    turns = json.loads(lines[0])["conversations"]
    assert [t["from"] for t in turns] == ["human", "function_call", "observation", "gpt", "function_call",
                                          "observation", "gpt"]
    assert turns[0]["value"] == "fix the test\n[image]"
    assert json.loads(turns[1]["value"]) == {"name": "read", "arguments": {"path": "nope.py"}}
    assert "exported 1 chat" in capsys.readouterr().err


def test_export_openai_compressed(sandbox, capsys):
    save(".", "a", MESSAGES)
    assert trajectories.trajectories_cli(["export", "--format", "openai", "--compress"]) == 0
    record = json.loads(capsys.readouterr().out.strip())
    messages = record["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]   # failed try dropped
    assert messages[1]["tool_calls"][0]["id"] == "c2"
    assert len(messages[2]["content"]) < 2500 and "characters cut" in messages[2]["content"]
    assert set(messages[0]) == {"role", "content"}


def test_all_projects_uses_the_known_projects(sandbox, tmp_path, capsys):
    other = tmp_path / "elsewhere"
    save(other, "x", MESSAGES[:1] + [{"role": "assistant", "content": "hi"}])
    save(".", "a", MESSAGES)
    assert trajectories.trajectories_cli(["export"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    trajectories.remember_project("t", None)
    data = json.loads(trajectories.projects_file().read_text())
    trajectories.projects_file().write_text(json.dumps(data + [str(other)]))
    assert trajectories.trajectories_cli(["export", "--all-projects"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 2


def test_scan_home_finds_projects(sandbox):
    save(Path.home() / "code" / "app", "a", MESSAGES)
    assert str(Path.home() / "code" / "app") in trajectories.scan_home()


def test_files_that_arent_chats_are_skipped(sandbox, tmp_path):
    files = []
    for i, text in enumerate(["[1, 2]", '"text"', "null", '{"messages": "nope"}']):
        path = tmp_path / f"{i}.json"
        path.write_text(text)
        files.append(path)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"messages": MESSAGES}))
    lines, read = trajectories.trajectories(files + [good])
    assert len(lines) == 1 and read == 1
