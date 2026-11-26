"""More built-in themes, in OpenCode's spirit: pick one with /theme <name> (or "theme" in the config).

Each sets every color nanocode uses (see nanocode.THEME_COLORS for what each one colors). Single colors can
still be changed on top, with /theme <color> #rrggbb.
"""
from __future__ import annotations

import nanocode as nc


def theme(bg, panel, panel2, border, text, muted, faint, accent, accent2, pink, cyan, success, error, warning,
          think):
    return {"bg": bg, "panel": panel, "panel2": panel2, "border": border, "text": text, "muted": muted,
            "faint": faint, "accent": accent, "accent2": accent2, "pink": pink, "cyan": cyan,
            "success": success, "error": error, "warning": warning, "think": think}


THEMES = {
    "tokyonight": theme("#1a1b26", "#1f2335", "#292e42", "#3b4261", "#c0caf5", "#787c99", "#565f89",
                        "#7aa2f7", "#bb9af7", "#ff79c6", "#7dcfff", "#9ece6a", "#f7768e", "#e0af68", "#ff9e64"),
    "catppuccin": theme("#1e1e2e", "#181825", "#313244", "#45475a", "#cdd6f4", "#a6adc8", "#6c7086",
                        "#89b4fa", "#cba6f7", "#f5c2e7", "#94e2d5", "#a6e3a1", "#f38ba8", "#f9e2af", "#fab387"),
    "gruvbox": theme("#282828", "#32302f", "#3c3836", "#504945", "#ebdbb2", "#a89984", "#7c6f64",
                     "#83a598", "#d3869b", "#d3869b", "#8ec07c", "#b8bb26", "#fb4934", "#fabd2f", "#fe8019"),
    "nord": theme("#2e3440", "#3b4252", "#434c5e", "#4c566a", "#eceff4", "#a3abb9", "#6b7385",
                  "#88c0d0", "#b48ead", "#c895bf", "#8fbcbb", "#a3be8c", "#bf616a", "#ebcb8b", "#d08770"),
    "dracula": theme("#282a36", "#21222c", "#343746", "#44475a", "#f8f8f2", "#9ea8c7", "#6272a4",
                     "#bd93f9", "#ff79c6", "#ff79c6", "#8be9fd", "#50fa7b", "#ff5555", "#f1fa8c", "#ffb86c"),
    "everforest": theme("#2d353b", "#343f44", "#3d484d", "#475258", "#d3c6aa", "#9da9a0", "#7a8478",
                        "#7fbbb3", "#d699b6", "#e69875", "#83c092", "#a7c080", "#e67e80", "#dbbc7f", "#e69875"),
    "one-dark": theme("#282c34", "#21252b", "#2c313a", "#3e4451", "#abb2bf", "#7f848e", "#5c6370",
                      "#61afef", "#c678dd", "#e06c9f", "#56b6c2", "#98c379", "#e06c75", "#e5c07b", "#d19a66"),
    "solarized-dark": theme("#002b36", "#073642", "#0a4050", "#2a5a66", "#d5dcdc", "#93a1a1", "#657b83",
                            "#268bd2", "#6c71c4", "#d33682", "#2aa198", "#859900", "#dc322f", "#b58900", "#cb4b16"),
    "kanagawa": theme("#1f1f28", "#16161d", "#2a2a37", "#54546d", "#dcd7ba", "#a6a69c", "#727169",
                      "#7e9cd8", "#957fb8", "#d27e99", "#7fb4ca", "#98bb6c", "#e82424", "#e6c384", "#ffa066"),
    "rosepine": theme("#191724", "#1f1d2e", "#26233a", "#403d52", "#e0def4", "#908caa", "#6e6a86",
                      "#9ccfd8", "#c4a7e7", "#ebbcba", "#31748f", "#9ccfd8", "#eb6f92", "#f6c177", "#f6c177"),
    "github-light": theme("#ffffff", "#f6f8fa", "#eaeef2", "#d0d7de", "#1f2328", "#59636e", "#8c959f",
                          "#0969da", "#8250df", "#bf3989", "#1b7c83", "#1a7f37", "#cf222e", "#9a6700", "#bc4c00"),
}

for _name, _colors in THEMES.items():
    nc.THEMES.setdefault(_name, _colors)
