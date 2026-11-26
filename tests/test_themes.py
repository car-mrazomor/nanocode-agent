import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import themes  # noqa: E402
from test_nanocode import plain  # noqa: E402

NEW = ["tokyonight", "catppuccin", "gruvbox", "nord", "dracula", "everforest", "one-dark", "solarized-dark",
       "kanagawa", "rosepine", "github-light"]


def test_every_new_theme_has_every_color():
    assert set(NEW) <= set(nc.THEMES) and set(themes.THEMES) == set(NEW)
    for name in NEW:
        colors = nc.THEMES[name]
        assert set(colors) == set(nc.THEMES["opencode"]), name
        assert all(nc.HEX_RE.match(v) for v in colors.values()), name


def test_theme_command_lists_and_picks_them(sandbox):
    nc.st.config = {}
    nc.cmd_theme("")
    assert "tokyonight" in plain(nc.st.output) and "github-light" in plain(nc.st.output)
    nc.cmd_theme("github-light")
    assert nc.st.config["theme"] == "github-light" and nc.THEME["bg"] == "#ffffff"
    nc.apply_theme({"theme": "one-dark"})
    assert nc.THEME == nc.THEMES["one-dark"]
