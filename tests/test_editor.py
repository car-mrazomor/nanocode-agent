import sys
from pathlib import Path

from prompt_toolkit.key_binding import KeyBindings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nanokit import editor  # noqa: E402
from test_keybinds import bound  # noqa: E402


def test_editor_binding_and_edit_text(sandbox, monkeypatch):
    kb = KeyBindings()
    editor.bind(kb, {})
    assert ("c-x", "e") in bound(kb)
    monkeypatch.setenv("EDITOR", "myeditor --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    assert editor.editor_command() == ["myeditor", "--wait"]

    def fake_editor(cmd):
        assert cmd[:2] == ["myeditor", "--wait"]
        path = Path(cmd[-1])
        assert path.read_text() == "draft"
        path.write_text("draft, longer\n")
        return 0

    assert editor.edit_text("draft", run=fake_editor) == "draft, longer"
    try:
        editor.edit_text("x", run=lambda cmd: 1)
        assert False, "a failed editor raises"
    except RuntimeError as e:
        assert "exited with code 1" in str(e)


def test_editor_fallbacks(sandbox, monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(editor.shutil, "which", lambda name: "/usr/bin/vi" if name == "vi" else None)
    expected = ["notepad"] if editor.os.name == "nt" else ["vi"]
    assert editor.editor_command() == expected
