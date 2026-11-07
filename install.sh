#!/bin/sh
# nanocode installer: puts a `nanocode` command on your PATH.
#
#   sh install.sh               install or update
#   sh install.sh --uninstall   remove it
#
# Works on macOS, Linux, WSL, Android (Termux), iSH, and Git Bash on Windows.
# Everything goes into its own folder with a private Python venv, so it never
# touches your system Python packages.

set -eu

RAW_URL="https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master/nanocode.py"
DEPS="claude-agent-sdk prompt_toolkit rich httpx"
APP_DIR="${NANOCODE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/nanocode}"

if [ -t 1 ]; then
  BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"; RED="$(printf '\033[31m')"
  GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"; RESET="$(printf '\033[0m')"
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '%s⏺%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s⏺ %s%s\n' "$YELLOW" "$*" "$RESET"; }
die()  { printf '%s⏺ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

# --- Detect the system -------------------------------------------------------

OS="$(uname -s 2>/dev/null || echo unknown)"
case "$OS" in
  MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
  Darwin)               PLATFORM="macos" ;;
  *)
    if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; then PLATFORM="termux"
    else PLATFORM="linux"; fi ;;
esac

# Hint for installing Python, based on the package manager we can find
python_hint() {
  if   [ "$PLATFORM" = "termux" ];            then echo "pkg install python"
  elif command -v brew    >/dev/null 2>&1;    then echo "brew install python"
  elif command -v apt-get >/dev/null 2>&1;    then echo "sudo apt-get install python3 python3-venv"
  elif command -v dnf     >/dev/null 2>&1;    then echo "sudo dnf install python3"
  elif command -v pacman  >/dev/null 2>&1;    then echo "sudo pacman -S python"
  elif command -v zypper  >/dev/null 2>&1;    then echo "sudo zypper install python3"
  elif command -v apk     >/dev/null 2>&1;    then echo "apk add python3 py3-pip"
  elif [ "$PLATFORM" = "windows" ];           then echo "winget install Python.Python.3.12"
  else echo "install Python 3.10+ from https://www.python.org/downloads/"; fi
}

# Where the `nanocode` command goes: a bin folder that is already on PATH if possible
pick_bin_dir() {
  if [ "$PLATFORM" = "termux" ] && [ -n "${PREFIX:-}" ]; then echo "$PREFIX/bin"; return; fi
  case ":$PATH:" in *":$HOME/.local/bin:"*) echo "$HOME/.local/bin"; return ;; esac
  if [ "$PLATFORM" != "windows" ] && [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
    echo "/usr/local/bin"; return
  fi
  echo "$HOME/.local/bin"   # not on PATH yet: we add it to the shell profile below
}

BIN_DIR="$(pick_bin_dir)"
LAUNCHER="$BIN_DIR/nanocode"

# --- Uninstall ---------------------------------------------------------------

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$APP_DIR"
  for f in "$LAUNCHER" "$HOME/.local/bin/nanocode" "/usr/local/bin/nanocode"; do
    [ -f "$f" ] && grep -q "nanocode installer" "$f" 2>/dev/null && rm -f "$f"
  done
  step "nanocode removed ${DIM}(saved chats in your projects' .nanocode/ folders were kept)${RESET}"
  exit 0
fi

say "${BOLD}nanocode installer${RESET} ${DIM}($PLATFORM)${RESET}"
say ""

# --- Python 3.10+ -------------------------------------------------------------

PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PY="$(command -v "$cand")"; break
  fi
done
[ -n "$PY" ] || die "Python 3.10 or newer not found. Install it with:  $(python_hint)"
step "Python: $("$PY" --version 2>&1) ${DIM}($PY)${RESET}"

# --- Copy nanocode.py ---------------------------------------------------------

mkdir -p "$APP_DIR"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/nanocode.py" ]; then
  cp "$SCRIPT_DIR/nanocode.py" "$APP_DIR/nanocode.py"
  step "Copied nanocode.py from $SCRIPT_DIR"
else
  # Run outside the repo (e.g. curl ... | sh): download it instead
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$RAW_URL" -o "$APP_DIR/nanocode.py"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$APP_DIR/nanocode.py" "$RAW_URL"
  else die "nanocode.py not found next to this script, and neither curl nor wget is installed"; fi
  step "Downloaded nanocode.py"
fi

# --- Private venv + dependencies ---------------------------------------------

VENV="$APP_DIR/venv"
venv_python() { if [ -x "$VENV/bin/python" ]; then echo "$VENV/bin/python"; elif [ -x "$VENV/Scripts/python.exe" ]; then echo "$VENV/Scripts/python.exe"; fi; }  # Windows uses Scripts/
if [ -d "$VENV" ] && [ -z "$(venv_python)" ]; then
  rm -rf "$VENV"  # half-made venv from an earlier failed run
fi
if [ ! -d "$VENV" ]; then
  "$PY" -m venv "$VENV" 2>/dev/null || die "Could not create a venv. On Debian/Ubuntu run:  sudo apt-get install python3-venv"
fi
VPY="$(venv_python)"

# `user = true` in pip.conf (common with Homebrew Python), or PIP_TARGET/PIP_PREFIX in the env,
# make pip refuse to install into a venv. Turn them off for our pip only; your pip config stays untouched.
export PIP_USER=0
unset PIP_TARGET PIP_PREFIX

step "Installing dependencies ${DIM}($DEPS)${RESET}"
"$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
# shellcheck disable=SC2086
if ! "$VPY" -m pip install --quiet --upgrade $DEPS; then
  die "pip install failed (see the error above). On Android/iSH, claude-agent-sdk may have no build for this device."
fi

# --- The `nanocode` command --------------------------------------------------

write_launcher() {
  cat <<EOF
#!/bin/sh
# nanocode installer: launcher (re-run install.sh to update, --uninstall to remove)
exec "$VPY" "$APP_DIR/nanocode.py" "\$@"
EOF
}

if mkdir -p "$BIN_DIR" 2>/dev/null && [ -w "$BIN_DIR" ]; then
  write_launcher > "$LAUNCHER"
  chmod +x "$LAUNCHER"
else
  die "Can't write to $BIN_DIR"
fi
step "Command installed: ${BOLD}$LAUNCHER${RESET}"

# Make sure ~/.local/bin is on PATH for future shells
PATH_NOTE=""
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    LINE="export PATH=\"$BIN_DIR:\$PATH\""
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
      if [ -f "$rc" ] || [ "$rc" = "$HOME/.profile" ]; then
        grep -qsF "$LINE" "$rc" || printf '\n# added by nanocode installer\n%s\n' "$LINE" >> "$rc"
      fi
    done
    PATH_NOTE="yes"
    ;;
esac

# --- Global memory (~/.config/nanocode/NANOCODE.md) --------------------------

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nanocode"
MEMORY="$CONFIG_DIR/NANOCODE.md"
if [ -f "$MEMORY" ]; then
  step "Memory already exists, kept it ${DIM}($MEMORY)${RESET}"
else
  mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/skills"
  NAME=""
  if [ -r /dev/tty ] && { true < /dev/tty; } 2>/dev/null; then  # works even with: curl ... | sh
    printf '%s?%s Your name, so nanocode can greet you %s(Enter to skip)%s: ' "$YELLOW" "$RESET" "$DIM" "$RESET"
    read -r NAME < /dev/tty || NAME=""
  fi
  {
    echo "# Memory"
    echo ""
    echo "Things nanocode should always know about you, in every project. Edit freely."
    echo "Project-only notes go in <project>/.nanocode/memory.md (type '# note' in nanocode)."
    echo ""
    echo "## About me"
    if [ -n "$NAME" ]; then echo "- Name: $NAME"; else echo "<!-- - Name: your name -->"; fi
    echo ""
    echo "## Preferences"
  } > "$MEMORY"
  step "Created memory ${DIM}($MEMORY)${RESET}"
fi

# --- Done --------------------------------------------------------------------

say ""
say "${GREEN}${BOLD}nanocode is installed.${RESET}"
if [ -n "$PATH_NOTE" ]; then
  say "Open a new terminal (or run: ${BOLD}export PATH=\"$BIN_DIR:\$PATH\"${RESET}), then:"
else
  say "Run it from any project folder:"
fi
say "  ${BOLD}cd your-project && nanocode${RESET}"
say ""
say "${DIM}Claude Code SDK mode: log in once with Claude Code (run 'claude', then /login).${RESET}"
if ! command -v claude >/dev/null 2>&1; then
  say "${DIM}  Claude Code isn't installed: curl -fsSL https://claude.ai/install.sh | bash${RESET}"
fi
say "${DIM}OpenRouter mode: export OPENROUTER_API_KEY=sk-or-...${RESET}"
