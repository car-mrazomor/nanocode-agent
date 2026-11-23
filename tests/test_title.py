import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402


class FakeOutput:
    def __init__(self):
        self.data = ""

    def write_raw(self, text):
        self.data += text

    def flush(self):
        pass


class FakeApp:
    is_running = True

    def __init__(self):
        self.output = FakeOutput()

    def invalidate(self):
        pass


class Task:
    def __init__(self, done=False, cancelled=False):
        self._done, self._cancelled = done, cancelled

    def done(self):
        return self._done

    def cancelled(self):
        return self._cancelled


def setup(monkeypatch, terminal=None):
    app = FakeApp()
    monkeypatch.setattr(nc.st, "app", app)
    monkeypatch.setattr(nc, "detect_terminal", lambda: terminal)
    monkeypatch.delenv("TMUX", raising=False)
    nc.st.home = False
    return app.output


def test_tab_title_follows_the_chat(sandbox, monkeypatch):
    out = setup(monkeypatch)
    nc.update_title()
    assert out.data == f"\x1b]0;nanocode · {Path.cwd().name}\x07"
    nc.st.session["title"] = "Fix the login bug"
    nc.st.task = Task()
    nc.update_title()
    assert out.data.endswith("\x1b]0;✻ Fix the login bug\x07")
    before = out.data
    nc.update_title()
    assert out.data == before                          # only written when it changes
    nc.st.pending_permission = object()
    assert nc.terminal_title() == "△ Fix the login bug"
    nc.st.pending_permission, nc.st.task = None, Task(done=True)
    nc.st.unseen = True
    assert nc.terminal_title() == "✓ Fix the login bug"
    nc.st.session["title"] = "x" * 80
    assert len(nc.terminal_title()) == 2 + 48
    nc.st.config["terminal_title"] = False
    out.data = ""
    nc.st.shown_title = None
    nc.update_title()
    assert out.data == ""


def test_done_while_away_notifies_and_marks_the_tab(sandbox, monkeypatch):
    out = setup(monkeypatch, "iterm2")
    nc.st.session["title"] = "Add tests"
    nc.focus_changed(False)
    nc.turn_finished(Task(done=True))
    assert nc.st.unseen and out.data == "\a\x1b]9;Done: Add tests\x07"
    nc.focus_changed(True)                             # back in the tab: the ✓ goes away
    assert not nc.st.unseen
    out.data = ""
    nc.turn_finished(Task(done=True))                  # you were looking: nothing
    assert out.data == "" and not nc.st.unseen
    nc.focus_changed(False)
    nc.turn_finished(Task(cancelled=True))             # stopped with Esc: nothing
    assert out.data == ""


def test_notification_per_terminal_and_settings(sandbox, monkeypatch):
    out = setup(monkeypatch, "kitty")
    nc.st.focused = False
    nc.notify("Needs your OK: bash")
    assert out.data == ("\a\x1b]99;i=nanocode:d=0;nanocode\x1b\\"
                        "\x1b]99;i=nanocode:d=1:p=body;Needs your OK: bash\x1b\\")
    out = setup(monkeypatch, "terminal.app")
    nc.st.focused = False
    nc.notify("Done: x")
    assert out.data == "\a"                            # bell only: Terminal.app marks the tab
    out = setup(monkeypatch, "ghostty")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    nc.st.focused = False
    nc.notify("Done; x")
    assert out.data == "\a\x1bPtmux;\x1b\x1b]9;Done, x\x07\x1b\\"
    out.data = ""
    nc.st.config["notify"] = "off"
    nc.notify("Done")
    assert out.data == ""
    nc.st.config["notify"] = "always"
    nc.st.focused = True
    nc.notify("Done")
    assert out.data.startswith("\a")


def test_without_focus_reports_only_long_turns_notify(sandbox, monkeypatch):
    out = setup(monkeypatch, "iterm2")
    nc.st.focused = None
    nc.st.task_started = nc.time.time() - 2
    nc.turn_finished(Task(done=True))
    assert out.data == ""
    nc.st.task_started = nc.time.time() - 60
    nc.turn_finished(Task(done=True))
    assert out.data.startswith("\a") and nc.st.unseen
    nc.seen_by_user()                                  # any key: you're back
    assert not nc.st.unseen


def test_focus_reports_are_filtered_from_input(sandbox, monkeypatch):
    from prompt_toolkit.input.vt100_parser import Vt100Parser
    setup(monkeypatch)
    nc.install_key_translation(keys=False)
    keys = []
    parser = Vt100Parser(keys.append)
    parser.feed("\x1b[O")
    assert nc.st.focused is False
    parser.feed("\x1b[Ia")
    parser.flush()
    assert nc.st.focused is True and [k.data for k in keys] == ["a"]
