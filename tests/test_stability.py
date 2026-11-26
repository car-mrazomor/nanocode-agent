"""Long turns, stuck models, stale files, parallel tools, config written by two nanocodes, and the UI helpers."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from test_nanocode import call, mock_http, plain, run, sse, tool_call_chunk  # noqa: E402


def long_turn(steps, size):
    msgs = [{"role": "user", "content": "go"}]
    for i in range(steps):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * size})
    return msgs


def test_one_long_turn_is_trimmed_to_fit():
    history, trimmed = nc.fit_history(long_turn(60, 20_000), 100_000)
    assert trimmed and nc.estimate_tokens(history) <= 100_000
    assert len(history[-1]["content"]) == 20_000                 # the newest results stay whole
    assert "trimmed" in history[2]["content"]
    assert len(history) == 121                                   # tool calls and results stay paired


def test_repeated_identical_calls_are_stopped(sandbox):
    guard = nc.RepeatGuard()
    read = {"path": "a.txt"}
    for _ in range(2):
        assert guard.check("read", read) is None
        guard.record("read", read, "1│hi")
    assert "exact read call" in guard.check("read", read)       # same call, same result, twice: not again
    guard = nc.RepeatGuard()
    for status in ("queued", "running", "queued", "passed", "passed"):   # polling whose result changes is fine
        assert guard.check("bash", {"command": "gh pr checks"}) is None
        guard.record("bash", {"command": "gh pr checks"}, status)
    assert guard.check("bash", {"command": "gh pr checks"})
    guard = nc.RepeatGuard()
    for _ in range(3):
        assert guard.check("read", read) is None
        guard.record("read", read, "1│hi")
        guard.record("edit", {"path": "a.txt", "old": "hi", "new": "ho"}, "edited")   # a change resets it
    assert all(guard.check("shell", {"action": "read", "name": "dev"}) is None for _ in range(5))


def test_permission_question_shows_while_tools_run_in_parallel(sandbox, monkeypatch):
    nc.st.auto_approve, nc.st.headless = False, False

    async def fetch(args):
        return "ok" if await nc.allowed("web", args["url"]) else nc.DENIED
    monkeypatch.setitem(nc.TOOLS["web_fetch"], "fn", fetch)
    calls = [{"id": str(i), "function": {"name": "web_fetch", "arguments": json.dumps({"url": f"https://x/{i}"})}}
             for i in range(2)]

    async def scenario():
        task = asyncio.ensure_future(nc.run_tool_calls(calls))
        for _ in range(2):
            for _ in range(100):
                await asyncio.sleep(0.01)
                if nc.st.pending_permission is not None:
                    break
            assert "Permission" in plain(nc.st.output)            # the question is on screen, not held back
            nc.answer_permission("y")
            await asyncio.sleep(0.01)
        return await asyncio.wait_for(task, 2)
    assert run(scenario()) == ["ok", "ok"]


def test_a_config_with_a_typo_is_not_wiped(sandbox):
    nc.st.config = nc.load_config()
    nc.st.config.update(model="a", email={"user": "me", "password": "pw"})
    nc.save_config()
    nc.config_file().write_text('{"model": "a", oops')                 # hand-edited, broken
    nc.st.config["theme"] = "nanocode"
    nc.save_config()
    saved = json.loads(nc.config_file().read_text())
    assert saved["email"]["password"] == "pw" and saved["theme"] == "nanocode"


def test_edit_refuses_a_file_changed_since_it_was_read(sandbox):
    Path("a.py").write_text("x = 1\n")
    call("read", path="a.py")
    time.sleep(0.01)
    Path("a.py").write_text("x = 1\ny = 2\n")                    # the user (or a formatter) changed it
    os.utime("a.py", ns=(time.time_ns(), time.time_ns() + 5_000_000))
    text, err = call("edit", path="a.py", old="x = 1", new="x = 3")
    assert err and "changed on disk" in text and Path("a.py").read_text() == "x = 1\ny = 2\n"
    call("read", path="a.py")
    text, err = call("edit", path="a.py", old="x = 1", new="x = 3")
    assert not err and Path("a.py").read_text() == "x = 3\ny = 2\n"
    text, err = call("edit", path="a.py", old="y = 2", new="y = 4")   # its own edits don't count
    assert not err


def test_read_only_calls_run_in_parallel_and_show_in_order(sandbox, monkeypatch):
    Path("a.txt").write_text("A\n")
    Path("b.txt").write_text("B\n")
    order = []
    real = nc.TOOLS["read"]["fn"]

    async def slow_read(args):
        order.append(("start", args["path"]))
        await asyncio.sleep(0.2 if args["path"] == "a.txt" else 0.01)
        order.append(("end", args["path"]))
        return await real(args)
    monkeypatch.setitem(nc.TOOLS["read"], "fn", slow_read)
    calls = [{"id": "1", "function": {"name": "read", "arguments": json.dumps({"path": "a.txt"})}},
             {"id": "2", "function": {"name": "read", "arguments": json.dumps({"path": "b.txt"})}}]
    started = time.time()
    results = run(nc.run_tool_calls(calls))
    assert time.time() - started < 0.35
    assert order[:2] == [("start", "a.txt"), ("start", "b.txt")]         # both started before either ended
    assert "A" in results[0] and "B" in results[1]
    shown = plain(nc.st.output)
    assert shown.index("a.txt") < shown.index("b.txt")                   # shown in the order of the calls


def test_stuck_model_gets_told_and_the_turn_ends(sandbox, monkeypatch):
    Path("a.txt").write_text("hi\n")
    replies = [[tool_call_chunk(f"c{i}", "read", {"path": "a.txt"})] for i in range(3)] + \
        [[{"choices": [{"delta": {"content": "ok, stopping"}}]}]]
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return sse(replies[len(sent) - 1])
    mock_http(monkeypatch, handler)
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "z-ai/glm-5.3", "k"
    nc.st.headless = True
    run(nc.api_query("read it"))
    results = [m["content"] for m in nc.st.messages if m["role"] == "tool"]
    assert len(results) == 3 and "exact read call" in results[2]


def test_claude_on_openrouter_gets_cache_marks(sandbox):
    nc.st.model = "anthropic/claude-sonnet-5.5"
    msgs = nc.with_cache_marks([{"role": "system", "content": "sys"}, {"role": "user", "content": "a"},
                                {"role": "assistant", "content": "b"}, {"role": "tool", "tool_call_id": "1", "content": "r"}])
    assert msgs[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert msgs[1]["content"] == "a"
    nc.st.model = "openai/gpt-5.5"
    assert nc.with_cache_marks([{"role": "system", "content": "sys"}])[0]["content"] == "sys"


def test_cache_marks_are_dropped_if_rejected(sandbox, monkeypatch):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(isinstance(body["messages"][0]["content"], list))
        if seen[-1]:
            return nc.httpx.Response(400, json={"error": {"message": "cache_control is not supported"}})
        return sse([{"choices": [{"delta": {"content": "hi"}}]}])
    mock_http(monkeypatch, handler)
    nc.st.backend, nc.st.model, nc.st.openrouter_key, nc.st.headless = "api", "anthropic/claude-haiku-4.5", "k", True
    message, _ = run(nc.model_call("sys", [{"role": "user", "content": "hi"}], use_tools=False))
    assert message["content"] == "hi" and seen == [True, False]


def test_two_instances_keep_each_others_config(sandbox):
    nc.st.config = nc.load_config()
    nc.st.config["model"] = "a"
    nc.save_config()
    other = json.loads(nc.config_file().read_text())                 # another nanocode sets up email meanwhile
    other["email"] = {"user": "me", "password": "pw"}
    nc.config_file().write_text(json.dumps(other))
    nc.st.config["theme"] = "nanocode"                               # this one changes something else
    nc.save_config()
    saved = json.loads(nc.config_file().read_text())
    assert saved["email"]["password"] == "pw" and saved["theme"] == "nanocode" and saved["model"] == "a"
    del nc.st.config["theme"]
    nc.save_config()
    assert "theme" not in json.loads(nc.config_file().read_text())


def test_config_screen_hides_every_secret(sandbox):
    nc.st.config = {"openrouter_api_key": "sk-or-v1-abcdefghijklmnop",
                    "email": {"user": "me@x.com", "password": "hunter2hunter2"},
                    "mcp_servers": {"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp_secretsecretsecret"}}}}
    shown = nc.show_config()
    assert "hunter2hunter2" not in shown and "ghp_secretsecretsecret" not in shown
    assert "abcdefghijklmnop" not in shown and "me@x.com" in shown


def test_streaming_markdown_renders_finished_blocks_once(sandbox, monkeypatch):
    calls = []
    real = nc.render_markdown
    monkeypatch.setattr(nc, "render_markdown", lambda text: calls.append(text) or real(text))
    nc.st.live_md = ""
    for part in ["# Title\n\n", "Some text.\n\n```py\nx = 1\n\n", "y = 2\n```\n\n", "More"]:
        nc.st.live_md += part
        nc.render_live()
    assert calls.count("# Title") == 1                      # rendered once, then kept
    assert "x = 1" in plain(nc.st.live) and "More" in plain(nc.st.live)
    assert nc.finished_blocks("a\n\n```\nb\n\nc") == 3       # a blank line inside a code block doesn't count


def test_prompt_history_keeps_what_chips_stand_for(sandbox):
    text_chip = nc.add_text_chip("line one\nline two\nline three")
    image_chip = nc.add_image_chip(b"\x89PNG\r\n\x1a\nfake", "image/png")
    entry = nc.history_entry(f"look {text_chip} and {image_chip}")
    assert "line two" in entry and "[Image" not in entry and ".nanocode/images/" in entry.replace(os.sep, "/")


def test_messages_typed_during_a_reply_are_queued(sandbox):
    from prompt_toolkit.buffer import Buffer

    async def scenario():
        nc.st.task = asyncio.ensure_future(asyncio.sleep(10))
        buffer = Buffer()
        buffer.text = "next question"
        kept = nc.on_enter(buffer)
        nc.st.task.cancel()
        return kept
    assert run(scenario()) is False and nc.st.queue == ["next question"]


def test_width_aware_clipping():
    assert nc.text_cells("日本語") == 6
    assert nc.clip_cells("日本語テキスト", 7) == "日本語…"
    assert nc.fit_left("~/projects/日本語", 7) == "…日本語"
    assert nc.clip_cells("short", 10) == "short"
