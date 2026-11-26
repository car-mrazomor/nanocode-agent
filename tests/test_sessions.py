import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import sessions  # noqa: E402
from test_nanocode import mock_http, plain, run, sse  # noqa: E402


def write_chat(folder, sid, title, messages=None, screen="", backend="api", **extra):
    path = Path(folder) / ".nanocode" / "sessions" / f"{sid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"id": sid, "title": title, "created": time.time(), "updated": time.time(), "backend": backend,
            "model": "m", "messages": messages or [], "usage": {"input": 100, "output": 20, "cost": 0.01},
            "activity": [], "changed": {}, "screen": screen, "sdk_session_id": None, **extra}
    path.write_text(json.dumps(data))
    return path


def test_index_and_search_across_projects(sandbox, tmp_path):
    write_chat(sandbox, "a1", "Fix the parser", [
        {"role": "user", "content": "the tokenizer breaks on unicode quotes"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": '{"pattern": "lexer"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "secret tool output zebra"},
        {"role": "assistant", "content": "Fixed the tokenizer."}])
    other = tmp_path / "other"
    write_chat(other, "b1", "Deploy notes", screen=f"{nc.USER_MARK}\n{nc.USER_MARK}how do we deploy kubernetes\n"
                                                   f"{nc.USER_MARK}\nUse helm.", backend="sdk")
    conn = sessions.connect()
    conn.execute("INSERT INTO projects VALUES (?, ?)", (str(other), time.time()))
    conn.commit()
    conn.close()
    assert sessions.update_index() == 2
    assert sessions.update_index() == 0                      # nothing changed: nothing read again
    hits = sessions.search("tokenizer")
    assert [h["id"] for h in hits] == ["a1"] and "tokenizer" in hits[0]["snippet"]
    assert sessions.search("lexer")[0]["id"] == "a1"         # tool calls are indexed
    assert sessions.search("zebra") == []                    # tool output isn't
    assert sessions.search("kubernetes")[0]["project"] == str(other)   # the screen of a subscription chat
    assert sessions.search("tokenizer", project=str(other)) == []
    assert sessions.search('"; DROP') == []                  # odd input is just words
    os.remove(sandbox / ".nanocode" / "sessions" / "a1.json")
    sessions.update_index()
    assert sessions.search("tokenizer") == []


def test_like_fallback_without_fts5(sandbox, monkeypatch):
    real = sessions.has_fts5
    monkeypatch.setattr(sessions, "has_fts5", lambda conn: False)
    write_chat(sandbox, "a1", "Fix the parser", [{"role": "user", "content": "the tokenizer breaks on quotes"}])
    sessions.update_index()
    hits = sessions.search("TOKENIZER quotes")
    assert hits and hits[0]["id"] == "a1" and "tokenizer" in hits[0]["snippet"]
    monkeypatch.setattr(sessions, "has_fts5", real)          # this SQLite's own FTS5 again: rebuilt, then found
    sessions.update_index()
    assert sessions.search("tokenizer")[0]["id"] == "a1"


def test_search_command_and_open(sandbox, tmp_path):
    nc.st.backend = "api"
    write_chat(sandbox, "a1", "Fix the parser", [{"role": "user", "content": "the tokenizer breaks"},
                                                  {"role": "assistant", "content": "ok"}])
    write_chat(tmp_path / "other", "b1", "Other tokenizer chat", [{"role": "user", "content": "tokenizer"}])
    conn = sessions.connect()
    conn.execute("INSERT INTO projects VALUES (?, ?)", (str(tmp_path / "other"), time.time()))
    conn.commit()
    conn.close()
    run(nc.run_input("/search tokenizer"))
    text = plain(nc.st.output)
    assert "Fix the parser" in text and "this project" in text and "Other tokenizer chat" in text
    first = next(i for i, r in enumerate(sessions.LAST["results"], 1) if r["id"] == "a1")
    other = next(i for i, r in enumerate(sessions.LAST["results"], 1) if r["id"] == "b1")
    run(nc.run_input(f"/search open {other}"))
    assert "/resume b1" in plain(nc.st.output)
    run(nc.run_input(f"/search open {first}"))
    assert [m["role"] for m in nc.st.messages] == ["user", "assistant"] and "Resumed" in plain(nc.st.output)


def test_session_search_tool(sandbox):
    write_chat(sandbox, "a1", "Fix the parser", [{"role": "user", "content": "we chose the recursive descent"},
                                                  {"role": "assistant", "content": "Good choice."}])
    text, err = run(nc.run_tool("session_search", {"query": "recursive descent"}))
    assert not err and "a1" in text and "Fix the parser" in text
    text, err = run(nc.run_tool("session_search", {"id": "a1"}))
    assert not err and "Good choice." in text
    text, err = run(nc.run_tool("session_search", {"id": "nope"}))
    assert err
    assert nc.tool_is_read_only(nc.TOOLS["session_search"])


def test_session_search_tool_stays_in_this_project(sandbox, tmp_path, monkeypatch):
    write_chat(sandbox, "a1", "Here", [{"role": "user", "content": "walrus here"}])
    write_chat(tmp_path / "other", "b1", "Elsewhere", [{"role": "user", "content": "walrus elsewhere secret"}])
    conn = sessions.connect()
    conn.execute("INSERT INTO projects VALUES (?, ?)", (str(tmp_path / "other"), time.time()))
    conn.commit()
    conn.close()
    sessions.update_index()
    text, err = run(nc.run_tool("session_search", {"query": "walrus"}))
    assert not err and "a1" in text and "b1" not in text
    text, err = run(nc.run_tool("session_search", {"id": "b1"}))
    assert err and "in this project" in text
    asked = []

    async def deny(tool, preview):
        asked.append((tool, preview))
        return False
    monkeypatch.setattr(nc, "allowed", deny)
    text, _ = run(nc.run_tool("session_search", {"query": "walrus", "all_projects": True}))
    assert text == nc.DENIED and asked[0][0] == "session_search" and "every project" in asked[0][1]
    nc.st.config["session_search_all_projects"] = True       # allowed by the config: no question
    text, err = run(nc.run_tool("session_search", {"query": "walrus", "all_projects": True}))
    assert not err and "b1" in text and len(asked) == 1
    text, err = run(nc.run_tool("session_search", {"id": "b1", "all_projects": True}))
    assert not err and "walrus elsewhere" in text


def test_session_search_runs_sqlite_off_the_event_loop(sandbox, monkeypatch):
    import threading
    write_chat(sandbox, "a1", "Here", [{"role": "user", "content": "walrus"}])
    threads = []
    real = sessions.search
    monkeypatch.setattr(sessions, "search", lambda *a: threads.append(threading.get_ident()) or real(*a))

    async def go():
        return threading.get_ident(), await nc.run_tool("session_search", {"query": "walrus"})
    loop_thread, (text, err) = run(go())
    assert not err and threads and threads[0] != loop_thread


def test_turn_end_indexes_the_chat(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "m", "k"
    mock_http(monkeypatch, lambda req: sse([{"choices": [{"delta": {"content": "Hello."}}]}]))
    nc.new_session()
    run(nc.run_input("remember the walrus protocol"))
    assert sessions.search("walrus")[0]["id"] == nc.st.session["id"]


def test_fork_copies_the_chat_into_a_new_tab(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "m", "k"
    mock_http(monkeypatch, lambda req: sse([{"choices": [{"delta": {"content": "Hello."}}]}]))
    nc.new_session()
    run(nc.run_input("first message"))
    original = nc.st.session["id"]
    run(nc.run_input("/fork"))
    assert len(nc.st.tabs) == 2 and nc.st.active == 1
    tab = nc.st.tabs[1]
    assert tab.session["id"] != original and tab.session["title"].startswith("fork of")
    assert [m["role"] for m in tab.messages] == ["user", "assistant"]
    tab.messages.append({"role": "user", "content": "x"})
    assert len(nc.st.tabs[0].messages) == 2                 # a real copy
    assert (nc.sessions_dir() / f"{tab.session['id']}.json").exists()


def test_fork_on_the_claude_subscription_sends_a_note(sandbox, monkeypatch):
    nc.st.backend = "sdk"
    nc.new_session()
    nc.st.session["title"] = "chat"
    nc.st.output = nc.user_block("what's the plan") + "\nThe plan is X.\n"
    run(nc.run_input("/fork"))
    sent = []

    async def fake_send(text, display=None, images=None):
        sent.append((text, display))
    monkeypatch.setattr(nc, "send", fake_send)
    with nc.for_tab(nc.st.tabs[1]):
        run(nc.run_input("go on"))
        run(nc.run_input("again"))
    assert "The plan is X." in sent[0][0] and "> what's the plan" in sent[0][0] and sent[0][1] == "go on"
    assert sent[1] == ("again", "again")
