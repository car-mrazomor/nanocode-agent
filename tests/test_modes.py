from pathlib import Path

from prompt_toolkit.key_binding import KeyBindings

from test_nanocode import call, nc, plain, run
from nanokit import modes


def test_plan_mode_is_read_only(sandbox, capsys):
    Path("a.txt").write_text("hi\n")
    assert modes.current_mode() == "build"
    run(nc.run_input("/plan"))
    assert modes.current_mode() == "plan" and "Plan mode" in plain(nc.st.output)
    assert "# Plan mode" in nc.build_system_prompt()
    text, err = call("write", path="b.txt", content="x")
    assert err and "plan mode" in text and not Path("b.txt").exists()
    text, err = call("edit", path="a.txt", old="hi", new="yo")
    assert err and Path("a.txt").read_text() == "hi\n"
    text, err = call("read", path="a.txt")
    assert not err and "hi" in text
    nc.st.headless = True                                      # auto-approve is on, but commands still ask
    text, err = call("bash", command="echo hi")
    assert err and "denied" in text
    nc.st.headless = False
    run(nc.run_input("/build"))
    assert modes.current_mode() == "build" and "# Plan mode" not in nc.build_system_prompt()
    text, err = call("write", path="b.txt", content="x")
    assert not err and Path("b.txt").exists()


def test_mode_is_per_tab_and_shift_tab_toggles(sandbox):
    nc.st.tabs.append(nc.Tab())
    kb = KeyBindings()
    modes.keys(kb, {})
    handler = next(b.handler for b in kb.bindings if [str(k) for k in b.keys] == ["Keys.BackTab"])
    handler(None)
    assert modes.current_mode(nc.st.tabs[0]) == "plan" and modes.current_mode(nc.st.tabs[1]) == "build"
    handler(None)
    assert modes.current_mode() == "build"
    assert any(key == "Shift+Tab" for key, _ in nc.KEYS_HELP)
