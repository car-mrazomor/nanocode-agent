import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import sessions, stats  # noqa: E402
from test_nanocode import plain, run  # noqa: E402
from test_sessions import write_chat  # noqa: E402


def call(name, args):
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_stats_this_project_and_all(sandbox, tmp_path):
    write_chat(sandbox, "a1", "one", [{"role": "user", "content": "hi"},
                                      {"role": "assistant", "content": "", "tool_calls": [call("edit", {}), call("read", {})]},
                                      {"role": "tool", "content": "ok"}, {"role": "assistant", "content": "done"}],
               changed={"src/app.py": [10, 2]})
    write_chat(sandbox, "a2", "two", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
               model="anthropic/claude-sonnet-5", updated=time.time() - 3 * 86400)
    write_chat(tmp_path / "other", "b1", "three", [], screen="x", backend="sdk",
               activity=[["bash", "ls", False], ["bash", "pwd", False]])
    conn = sessions.connect()
    conn.execute("INSERT INTO projects VALUES (?, ?)", (str(tmp_path / "other"), time.time()))
    conn.commit()
    conn.close()

    s = stats.collect()
    assert s["chats"] == 2 and s["messages"] == 5 and s["tools"] == {"edit": 1, "read": 1}
    assert s["in"] == 200 and s["files"] == {"src/app.py": 12} and len(s["models"]) == 2
    s = stats.collect(everywhere=True)
    assert s["chats"] == 3 and s["tools"]["bash"] == 2 and len(s["projects"]) == 2

    run(nc.run_input("/stats"))
    text = plain(nc.st.output)
    assert "2 chats" in text and "Tool calls" in text and "src/app.py" in text and "Last 14 days" in text
    assert "█" in text
    run(nc.run_input("/stats all"))
    assert "all projects (2)" in plain(nc.st.output)


def test_stats_with_no_chats(sandbox):
    run(nc.run_input("/stats"))
    assert "no saved chats" in plain(nc.st.output)


def test_bar():
    assert stats.bar(0, 10) == "" and stats.bar(10, 10, 4) == "████" and stats.bar(5, 10, 3).startswith("█")
