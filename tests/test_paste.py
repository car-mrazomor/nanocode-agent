import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from test_nanocode import mock_http, sse  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def test_dropped_images_become_numbered_chips(sandbox, tmp_path):
    shot, notes = tmp_path / "my shot.png", tmp_path / "notes.txt"
    shot.write_bytes(PNG)
    notes.write_text("x")
    assert nc.pasted(f'"{shot}" "{notes}"') == f"[Image #1] @{notes} "
    assert nc.pasted(str(shot)) == "[Image #2] "
    kind, name = nc.st.attachments["[Image #1]"]
    assert kind == "image" and (nc.project_dir() / "images" / name).read_bytes() == PNG


def test_long_pastes_collapse(sandbox):
    assert nc.pasted("short text") == "short text"
    assert nc.pasted("a\nb\nc") == "a\nb\nc"                     # 3 lines stay
    assert nc.pasted("a\nb\nc\nd") == "[Pasted text #1 +4 lines]"
    assert nc.pasted("x" * 900) == "[Pasted text #2 +1 lines]"
    images = []
    text = nc.expand_chips("fix this: [Pasted text #1 +4 lines] and [Pasted text #9 +1 lines]", images)
    assert text == "fix this: a\nb\nc\nd and [Pasted text #9 +1 lines]" and images == []


def test_image_chips_are_sent_with_the_message(sandbox, monkeypatch):
    chip = nc.add_image_chip(PNG, "image/png")
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return sse([{"choices": [{"delta": {"content": "A picture."}}]},
                    {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}])

    mock_http(monkeypatch, handler)
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "z-ai/glm-5.3", "k"
    asyncio.run(nc.run_input(f"what is in {chip}? also {chip}"))
    content = requests[0]["messages"][-1]["content"]
    assert content[0]["text"] == "what is in [Image #1]? also [Image #1]"   # the chip stays, to refer to
    assert [p["type"] for p in content] == ["text", "image_url"]            # the image once
    assert "[Image #1]" in nc.ANSI_RE.sub("", nc.st.output)


def test_clipboard_image_on_macos(sandbox, monkeypatch):
    monkeypatch.setattr(nc.sys, "platform", "darwin")

    class Result:
        returncode, stdout = 0, b"\xc2\xabdata PNGf" + PNG.hex().upper().encode() + b"\xc2\xbb\n"

    monkeypatch.setattr(nc.subprocess, "run", lambda *a, **k: Result())
    assert nc.clipboard_image() == ("image/png", PNG)
    Result.stdout = b""
    Result.returncode = 1
    assert nc.clipboard_image() is None


def test_paste_again_shows_the_text_to_edit(sandbox, monkeypatch):
    from prompt_toolkit.buffer import Buffer
    monkeypatch.setattr(nc, "show_toast", lambda *a, **k: None)
    long = "def f():\n    return 1\n\nprint(f())\n"
    buffer = Buffer(multiline=True)
    buffer.insert_text("explain")
    nc.paste_into(buffer, long)
    assert buffer.text == "explain [Pasted text #1 +5 lines]"
    buffer.insert_text(" please")
    nc.paste_into(buffer, long)                                   # same text again: shown in full
    assert buffer.text == "explain " + long + " please" and buffer.document.text_before_cursor.endswith(long)
    nc.paste_into(buffer, long)                                   # a third paste collapses again
    assert buffer.text.endswith(long + "[Pasted text #2 +5 lines] please")
    nc.paste_into(buffer, "short")
    assert buffer.text.endswith("lines]short please")


def test_transcript_shows_the_pasted_text(sandbox, monkeypatch):
    chip = nc.add_text_chip("line one\nline two\nline three\nline four")

    def handler(req):
        return sse([{"choices": [{"delta": {"content": "ok"}}]},
                    {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}])

    mock_http(monkeypatch, handler)
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "z-ai/glm-5.3", "k"
    asyncio.run(nc.run_input(f"fix {chip}"))
    screen = nc.ANSI_RE.sub("", nc.st.output)
    assert "line four" in screen and "[Pasted text" not in screen
