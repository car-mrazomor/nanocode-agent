import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from test_nanocode import mock_http, sse  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def test_default_theme_is_opencode_with_our_accents(sandbox):
    nc.apply_theme({})
    assert nc.THEME["bg"] == "#0a0a0a" and nc.THEME["panel"] == "#141414" and nc.THEME["text"] == "#eeeeee"
    assert nc.THEME["accent"] == "#7fa7c9" and nc.THEME["accent2"] == "#9d8fc4"


def test_config_picks_a_theme_and_overrides_colors(sandbox):
    nc.apply_theme({"theme": "nanocode", "colors": {"accent": "#ff79c6", "bg": "red", "nope": "#000000"}})
    assert nc.THEME["bg"] == nc.THEMES["nanocode"]["bg"]          # invalid values are ignored
    assert nc.THEME["accent"] == "#ff79c6" and "nope" not in nc.THEME
    nc.apply_theme({"theme": "unknown"})
    assert nc.THEME == nc.THEMES["opencode"]


def test_theme_command_saves_and_applies(sandbox):
    nc.st.config = {}
    nc.cmd_theme("accent #FF79C6")
    assert nc.THEME["accent"] == "#ff79c6" and nc.load_config()["colors"] == {"accent": "#ff79c6"}
    nc.cmd_theme("nanocode")
    assert nc.THEME["bg"] == nc.THEMES["nanocode"]["bg"] and nc.THEME["accent"] == "#ff79c6"
    nc.cmd_theme("accent default")
    assert nc.THEME["accent"] == nc.THEMES["nanocode"]["accent"] and "colors" not in nc.load_config()
    nc.cmd_theme("reset")
    assert nc.THEME == nc.THEMES["opencode"] and "theme" not in nc.load_config()
    nc.cmd_theme("accent blue")
    assert "/theme takes" in nc.ANSI_RE.sub("", nc.st.output)
    nc.cmd_theme("")
    assert "accent2" in nc.ANSI_RE.sub("", nc.st.output)
    assert nc.tui_style() is not None


def test_dropped_files_become_mentions(sandbox, tmp_path):
    spaced = Path.cwd() / "my shot.png"
    spaced.write_bytes(PNG)
    other = tmp_path / "notes.txt"
    other.write_text("x")
    # macOS terminals escape spaces, others quote (Windows Terminal: "..."), some send file:// URLs
    if os.name != "nt":
        assert nc.dropped_paths(str(spaced).replace(" ", "\\ ")) == [spaced]
        assert nc.dropped_paths(f"'{spaced}' {other}\n") == [spaced, other]
    assert nc.dropped_paths(f'"{spaced}" "{other}"') == [spaced, other]
    assert nc.dropped_paths(str(spaced)) == [spaced]
    assert nc.dropped_paths(spaced.as_uri()) == [spaced]
    assert nc.dropped_paths("hello world") is None
    assert nc.dropped_paths("app.py") is None                       # not absolute: ordinary text
    assert nc.dropped_paths(str(tmp_path / "missing.txt")) is None
    assert nc.mention(spaced) == '@"my shot.png"' and nc.mention(other) == f"@{other}"


def test_mentioned_images_are_attached(sandbox, monkeypatch):
    Path("my shot.png").write_bytes(PNG)
    Path("a.txt").write_text("hello")
    images = []
    text = nc.expand_mentions('look at @"my shot.png" and @a.txt', images)
    assert len(images) == 1 and (nc.project_dir() / "images" / images[0]).read_bytes() == PNG
    assert '<file path="a.txt">' in text and "my shot.png" in text
    assert nc.expand_mentions("@my-missing.png") == "@my-missing.png"
    assert "<file" not in nc.expand_mentions('@"my shot.png"')        # no image list: skipped like binary

    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return sse([{"choices": [{"delta": {"content": "A picture."}}]},
                    {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}])

    mock_http(monkeypatch, handler)
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "z-ai/glm-5.3", "k"
    asyncio.run(nc.run_input('what is @"my shot.png"'))
    content = requests[0]["messages"][-1]["content"]
    assert content[0]["type"] == "text" and content[1]["type"] == "image_url"
