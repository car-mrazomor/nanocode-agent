"""Tabs (several chats at once, each with its own history and turn) and OpenCode-style toasts."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from test_nanocode import mock_http, plain, run, sse  # noqa: E402


def test_chat_fields_belong_to_the_tab_the_code_runs_for(sandbox):
    first = nc.st.active_tab
    nc.out("in the first tab")
    second = nc.Tab(model="m2")
    nc.st.tabs.append(second)
    with nc.for_tab(second):
        nc.out("in the second tab")
        nc.st.messages.append({"role": "user", "content": "hi"})
        assert nc.st.model == "m2"
    assert "in the first tab" in first.output and "second" not in first.output
    assert "in the second tab" in second.output and not first.messages
    nc.st.config["x"] = 1                                   # shared fields stay shared
    with nc.for_tab(second):
        assert nc.st.config["x"] == 1


def test_two_tabs_run_turns_at_the_same_time(sandbox, monkeypatch):
    gate = asyncio.Event()

    async def handler(req):
        body = json.loads(req.content)
        question = body["messages"][-1]["content"]
        if question == "slow":
            await gate.wait()
        return sse([{"choices": [{"delta": {"content": f"answer to {question}"}}]}])

    mock_http(monkeypatch, handler)
    nc.st.backend, nc.st.model, nc.st.openrouter_key, nc.st.headless = "api", "z-ai/glm-5.3", "k", True

    async def scenario():
        first = nc.st.active_tab
        second = nc.Tab(model=nc.st.model)
        nc.st.tabs.append(second)
        slow = nc.in_tab(first, nc.api_query("slow"))
        fast = nc.in_tab(second, nc.api_query("fast"))
        await asyncio.wait_for(fast, 5)                     # the second finishes while the first still waits
        assert not slow.done()
        gate.set()
        await asyncio.wait_for(slow, 5)
        return first, second
    first, second = run(scenario())
    assert [m["content"] for m in first.messages] == ["slow", "answer to slow"]
    assert [m["content"] for m in second.messages] == ["fast", "answer to fast"]


def test_background_tab_finishing_shows_a_toast_and_a_mark(sandbox):
    nc.st.headless = False
    background = nc.Tab()
    background.session = {"title": "fix the tests"}
    nc.st.tabs.append(background)

    async def scenario():
        task = asyncio.ensure_future(asyncio.sleep(0))
        await task
        nc.turn_finished(task, background)
    run(scenario())
    assert background.attention == "done" and nc.tab_status(background) == "done"
    toast = nc.st.toasts[-1]
    assert toast["title"] == "Tab 2 finished" and toast["message"] == "fix the tests" and toast["variant"] == "success"
    toast["action"]()                                       # clicking it opens that tab
    assert nc.st.active_tab is background and background.attention is None


def test_closing_a_tab_stops_its_turn_and_saves_it(sandbox):
    nc.st.headless = False
    nc.new_session()

    async def scenario():
        tab = nc.new_tab(announce=False)
        with nc.for_tab(tab):
            nc.st.session["title"] = "work in progress"
        tab.task = nc.in_tab(tab, asyncio.sleep(60))
        await asyncio.sleep(0)
        await nc.close_tab()
        return tab
    tab = run(scenario())
    assert tab.task.cancelled() and tab not in nc.st.tabs and len(nc.st.tabs) == 1 and nc.st.active == 0
    saved = [json.loads(p.read_text())["title"] for p in nc.sessions_dir().glob("*.json")]
    assert "work in progress" in saved


def test_the_last_tab_stays_open(sandbox):
    run(nc.close_tab())
    assert len(nc.st.tabs) == 1 and "only tab" in nc.st.toasts[-1]["message"]


def test_provider_switch_waits_for_one_tab(sandbox):
    nc.st.tabs.append(nc.Tab())
    nc.st.provider = "claude"
    run(nc.cmd_provider("ollama"))
    assert "close the other tabs first" in plain(nc.st.output)


def test_toasts_wrap_stack_and_expire(sandbox, monkeypatch):
    nc.show_toast("one")
    nc.show_toast("a much longer message " * 6, title="Heads up", variant="warning")
    nc.show_toast("one")                                    # the same again: moved to the end, not doubled
    assert [t["message"] for t in nc.st.toasts][-1] == "one" and len(nc.st.toasts) == 2
    rows, width = nc.toast_rows()
    assert width <= nc.TOAST_WIDTH and any(kind == "title" for kind, _, _ in rows)
    assert all(nc.text_cells(text) <= nc.TOAST_WIDTH for _, text, _ in rows)
    clock = nc.time.monotonic() + 60
    monkeypatch.setattr(nc.time, "monotonic", lambda: clock)
    assert nc.live_toasts() == []


def test_tabs_in_the_app(sandbox):
    """Ctrl+T opens a tab, each tab keeps its draft, Alt+1 switches back, and the tab bar shows both."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    seen = {}

    async def scenario():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            app = nc.build_app()
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.2)
            inp.send_text("draft in one")
            await asyncio.sleep(0.1)
            inp.send_text("\x14")                           # ctrl+t
            await asyncio.sleep(0.2)
            seen["tabs"] = len(nc.st.tabs), nc.st.active, nc.st.input_box.text
            inp.send_text("draft in two")
            await asyncio.sleep(0.1)
            inp.send_text("\x1b1")                          # alt+1
            await asyncio.sleep(0.2)
            seen["back"] = nc.st.active, nc.st.input_box.text, nc.st.tabs[1].draft
            seen["bar"] = nc.st.tabs[0].home is False
            app.exit()
            await task
    run(scenario())
    assert seen["tabs"] == (2, 1, "")
    assert seen["back"] == (0, "draft in one", "draft in two") and seen["bar"]


def test_closing_twice_and_closing_a_busy_tab(sandbox):
    nc.st.headless = False

    async def scenario():
        nc.new_tab(announce=False)
        nc.new_tab(announce=False)
        busy = nc.st.active_tab
        busy.task = nc.in_tab(busy, asyncio.sleep(60))
        await asyncio.sleep(0)
        await asyncio.gather(nc.close_tab(), nc.close_tab(2))     # the same tab, twice at once
        return busy
    busy = run(scenario())
    assert busy not in nc.st.tabs and len(nc.st.tabs) == 2 and busy.task.cancelled()


def test_a_task_cancelled_before_it_starts_is_cleaned_up(sandbox):
    closed = []

    async def work():
        try:
            await asyncio.sleep(1)
        finally:
            closed.append(True)

    async def scenario():
        task = nc.in_tab(nc.st.active_tab, work())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        run(scenario())


def test_resuming_a_chat_open_in_another_tab_goes_there(sandbox):
    nc.st.backend, nc.st.headless = "api", False
    nc.new_session()
    nc.st.session["title"] = "shared chat"
    nc.st.messages.append({"role": "user", "content": "hi"})
    nc.save_session()
    first = nc.st.active_tab
    nc.new_tab(announce=False)
    run(nc.resume_session("1"))
    assert nc.st.active_tab is first and len(nc.st.tabs) == 2


def test_memory_or_tools_changed_elsewhere_reconnects_the_claude_client(sandbox, monkeypatch):
    nc.st.backend = "sdk"
    reconnected = []

    async def refresh():
        reconnected.append(True)
    monkeypatch.setattr(nc, "refresh_sdk_prompt", refresh)

    async def nothing(text, images=None):
        return None
    monkeypatch.setattr(nc, "sdk_query", nothing)
    nc.st.client = object()
    nc.st.client_signature = nc.sdk_signature()
    run(nc.send("one"))
    assert reconnected == []
    nc.add_memory("prefer tabs over spaces")              # e.g. `#` in another tab
    run(nc.send("two"))
    assert reconnected == [True]
