#!/bin/sh
# nanocode installer for macOS, Linux, WSL, Android (Termux), iSH and Git Bash.
# Everything lives in ~/.nanocode (override with NANOCODE_HOME); Windows PowerShell: install.ps1.
#
#   sh install.sh                        install or update
#   sh install.sh --uninstall            remove nanocode, keep config, memory and skills
#   sh install.sh --uninstall --purge    remove everything
#   sh install.sh --no-rea               skip REA (optional reverse-engineering tools, needs Node.js 22+)

set -eu

REPO_RAW="https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master"
NANO_HOME="${NANOCODE_HOME:-$HOME/.nanocode}"
APP="$NANO_HOME/app"
VENV="$NANO_HOME/venv"
BIN="$NANO_HOME/bin"
TOOLS="$NANO_HOME/tools"
PYDIR="$NANO_HOME/python"
NODEDIR="$NANO_HOME/node"
REQUIRED="httpx>=0.27 rich>=13 prompt_toolkit>=3.0.40"
OPTIONAL="claude-agent-sdk"
LEGACY_APP="${XDG_DATA_HOME:-$HOME/.local/share}/nanocode"
LEGACY_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/nanocode"

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

UNINSTALL=0
PURGE=0
NO_REA="${NANOCODE_NO_REA:-}"
for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    --no-rea) NO_REA=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
  Darwin) PLATFORM="macos" ;;
  *) if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; then PLATFORM="termux"; else PLATFORM="linux"; fi ;;
esac

on_path() { case ":$PATH:" in *":$1:"*) return 0 ;; *) return 1 ;; esac; }

# Folders where a `nanocode` link makes the command work right away, without a new shell.
link_dirs() {
  if [ "$PLATFORM" = "termux" ] && [ -n "${PREFIX:-}" ]; then echo "$PREFIX/bin"; fi
  echo "$HOME/.local/bin"
  echo "/usr/local/bin"
}

is_ours() {
  [ -L "$1" ] && case "$(readlink "$1")" in *nanocode*) return 0 ;; esac
  [ -f "$1" ] && grep -q "nanocode installer\|nanocode launcher" "$1" 2>/dev/null
}

rc_files() {
  for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" "$HOME/.config/fish/config.fish"; do
    [ -f "$rc" ] && echo "$rc"
  done
  return 0
}

# Removes our PATH lines, including the two-line block older versions wrote.
clean_rc() {
  for rc in $(rc_files); do
    grep -q "nanocode" "$rc" 2>/dev/null || continue
    tmp="$rc.nanocode-tmp"
    awk '/# added by nanocode installer/ { skip = 1; next } skip { skip = 0; next } / # nanocode$/ { next } { print }' "$rc" > "$tmp"
    cat "$tmp" > "$rc"
    rm -f "$tmp"
  done
}

if [ "$UNINSTALL" = 1 ]; then
  for dir in $(link_dirs); do
    if is_ours "$dir/nanocode"; then rm -f "$dir/nanocode" 2>/dev/null || warn "couldn't remove $dir/nanocode"; fi
  done
  rm -rf "$APP" "$VENV" "$BIN" "$TOOLS" "$PYDIR" "$NODEDIR"
  [ -f "$LEGACY_APP/nanocode.py" ] && rm -rf "$LEGACY_APP"
  clean_rc
  if [ "$PURGE" = 1 ]; then
    rm -rf "$NANO_HOME" "$LEGACY_CONFIG" "$HOME/.nanocode_history"
    step "nanocode removed completely"
  else
    step "nanocode removed ${DIM}(config, memory and skills kept in $NANO_HOME; --purge deletes them)${RESET}"
  fi
  say "${DIM}Each project's .nanocode/ folder (saved chats, project memory) was left alone.${RESET}"
  exit 0
fi

say "${BOLD}nanocode installer${RESET} ${DIM}($PLATFORM → $NANO_HOME)${RESET}"
say ""

python_hint() {
  if   [ "$PLATFORM" = "termux" ];          then echo "pkg install python"
  elif command -v brew    >/dev/null 2>&1;  then echo "brew install python"
  elif command -v apt-get >/dev/null 2>&1;  then echo "sudo apt-get install python3 python3-venv"
  elif command -v dnf     >/dev/null 2>&1;  then echo "sudo dnf install python3"
  elif command -v pacman  >/dev/null 2>&1;  then echo "sudo pacman -S python"
  elif command -v zypper  >/dev/null 2>&1;  then echo "sudo zypper install python3"
  elif command -v apk     >/dev/null 2>&1;  then echo "apk add python3 py3-pip"
  elif [ "$PLATFORM" = "windows" ];         then echo "winget install Python.Python.3.12"
  else echo "install Python 3.10+ from https://www.python.org/downloads/"; fi
}

fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  else die "need curl or wget to download $1"; fi
}

# Python 3.10+ already on the system, if any.
find_python() {
  for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
      command -v "$cand"
      return
    fi
  done
}

# Installs Python without admin rights: Termux/Alpine use their package manager; everywhere else uv
# downloads a standalone Python into ~/.nanocode/python. Nothing outside ~/.nanocode is touched.
install_python() {
  if [ "$PLATFORM" = "termux" ]; then
    step "Installing Python ${DIM}(pkg install python)${RESET}"
    pkg install -y python >/dev/null 2>&1 || die "couldn't install Python; run: pkg install python"
    PY="$(find_python)"
    return
  fi
  if command -v apk >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then
    step "Installing Python ${DIM}(apk add python3)${RESET}"
    apk add --no-cache python3 >/dev/null 2>&1 || die "couldn't install Python; run: apk add python3"
    PY="$(find_python)"
    return
  fi
  UV="$TOOLS/uv"
  [ -x "$UV.exe" ] && UV="$UV.exe"
  export UV_PYTHON_INSTALL_DIR="$PYDIR" UV_CACHE_DIR="$TOOLS/cache"
  if [ -x "$UV" ]; then
    PY="$("$UV" python find 3.12 2>/dev/null || true)"
    case "$PY" in "$PYDIR"*) return ;; *) PY="" ;; esac
  fi
  step "Installing Python 3.12 into $PYDIR ${DIM}(via uv, no admin rights needed)${RESET}"
  if [ ! -x "$UV" ]; then
    mkdir -p "$TOOLS"
    fetch "https://astral.sh/uv/install.sh" "$TOOLS/uv-install.sh"
    UV_UNMANAGED_INSTALL="$TOOLS" sh "$TOOLS/uv-install.sh" >/dev/null 2>&1 \
      || die "couldn't download uv to install Python. Install Python 3.10+ yourself:  $(python_hint)"
    rm -f "$TOOLS/uv-install.sh"
    [ -x "$UV" ] || UV="$TOOLS/uv.exe"
  fi
  "$UV" python install 3.12 --no-bin >/dev/null 2>&1 \
    || die "couldn't download Python. Install Python 3.10+ yourself:  $(python_hint)"
  PY="$("$UV" python find 3.12 2>/dev/null)"
  rm -rf "$TOOLS/cache"
}

PY=""
[ -n "${NANOCODE_MANAGED_PYTHON:-}" ] || PY="$(find_python)"
[ -n "$PY" ] || install_python
[ -n "$PY" ] || die "Python 3.10 or newer not found. Install it with:  $(python_hint)"
step "Python $("$PY" -c 'import platform; print(platform.python_version())') ${DIM}($PY)${RESET}"

mkdir -p "$APP" "$BIN"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/nanocode.py" ]; then
  cp "$SCRIPT_DIR/nanocode.py" "$APP/nanocode.py.new"
  cp "$SCRIPT_DIR/install.sh" "$APP/install.sh"
  [ -f "$SCRIPT_DIR/install.ps1" ] && cp "$SCRIPT_DIR/install.ps1" "$APP/install.ps1"
  step "Copied nanocode from $SCRIPT_DIR"
else
  fetch "$REPO_RAW/nanocode.py" "$APP/nanocode.py.new"
  fetch "$REPO_RAW/install.sh" "$APP/install.sh" || true
  step "Downloaded nanocode"
fi
head -1 "$APP/nanocode.py.new" | grep -q python || die "downloaded nanocode.py looks broken; try again later"
mv "$APP/nanocode.py.new" "$APP/nanocode.py"

venv_python() {
  for p in "$VENV/bin/python" "$VENV/Scripts/python.exe"; do
    [ -x "$p" ] && "$p" -c "" >/dev/null 2>&1 && { echo "$p"; return; }
  done
}
if [ -d "$VENV" ] && [ -z "$(venv_python)" ]; then
  rm -rf "$VENV"
fi
if [ ! -d "$VENV" ] && ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
  # e.g. Debian/Ubuntu without python3-venv: use our own Python instead of asking for sudo
  rm -rf "$VENV"
  case "$PY" in "$PYDIR"*) die "couldn't create a Python venv in $VENV" ;; esac
  warn "this Python can't create a venv; installing a private one"
  PY=""
  install_python
  if [ -z "$PY" ] || ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
    die "couldn't create a Python venv in $VENV"
  fi
fi
VPY="$(venv_python)"
[ -n "$VPY" ] || die "the venv in $VENV is broken; delete it and run the installer again"

# pip.conf with `user = true` (common with Homebrew Python) or PIP_TARGET/PIP_PREFIX break installs into a venv.
export PIP_USER=0 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_CACHE_DIR="$NANO_HOME/.pip-cache"
unset PIP_TARGET PIP_PREFIX

step "Installing dependencies ${DIM}(httpx, rich, prompt_toolkit)${RESET}"
"$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
# shellcheck disable=SC2086
"$VPY" -m pip install --quiet --upgrade $REQUIRED || die "pip install failed (see the error above)"
step "Installing claude-agent-sdk ${DIM}(for the Claude subscription provider)${RESET}"
# shellcheck disable=SC2086
if ! "$VPY" -m pip install --quiet --upgrade $OPTIONAL >/dev/null 2>&1; then
  warn "claude-agent-sdk has no build for this device; OpenRouter and Ollama still work"
fi

cat > "$BIN/nanocode" <<EOF
#!/bin/sh
# nanocode launcher (made by the nanocode installer; update: nanocode update, remove: nanocode uninstall)
exec "$VPY" "$APP/nanocode.py" "\$@"
EOF
chmod +x "$BIN/nanocode"
rm -rf "$NANO_HOME/.pip-cache"

if [ -f "$LEGACY_APP/nanocode.py" ]; then
  rm -rf "$LEGACY_APP"
  step "Removed the old install in $LEGACY_APP"
fi

LINKED=""
for dir in $(link_dirs); do
  on_path "$dir" || continue
  [ -d "$dir" ] && [ -w "$dir" ] || continue
  if [ -e "$dir/nanocode" ] && ! is_ours "$dir/nanocode"; then continue; fi
  ln -sf "$BIN/nanocode" "$dir/nanocode" && LINKED="$dir/nanocode" && break
done

clean_rc
for rc in $(rc_files) "$HOME/.profile"; do
  case "$rc" in
    *config.fish) line="fish_add_path $BIN # nanocode" ;;
    *) line="export PATH=\"$BIN:\$PATH\" # nanocode" ;;
  esac
  grep -qsF "$line" "$rc" || printf '%s\n' "$line" >> "$rc"
done
if [ -n "$LINKED" ]; then
  step "Command: ${BOLD}nanocode${RESET} ${DIM}($LINKED → $BIN/nanocode)${RESET}"
else
  step "Command: ${BOLD}$BIN/nanocode${RESET} ${DIM}(added to PATH in your shell profile)${RESET}"
fi

if [ -z "${NANOCODE_HOME:-}" ]; then
  for name in config.json NANOCODE.md skills; do
    if [ -e "$LEGACY_CONFIG/$name" ] && [ ! -e "$NANO_HOME/$name" ]; then
      mv "$LEGACY_CONFIG/$name" "$NANO_HOME/$name"
    fi
  done
  if [ -e "$HOME/.nanocode_history" ] && [ ! -e "$NANO_HOME/history" ]; then
    mv "$HOME/.nanocode_history" "$NANO_HOME/history"
  fi
  rmdir "$LEGACY_CONFIG/skills" "$LEGACY_CONFIG" 2>/dev/null || true
fi

mkdir -p "$NANO_HOME/skills"
MEMORY="$NANO_HOME/NANOCODE.md"
if [ ! -f "$MEMORY" ]; then
  NAME=""
  if [ -r /dev/tty ] && { true < /dev/tty; } 2>/dev/null; then
    printf '%s?%s Your name, so nanocode can greet you %s(Enter to skip)%s: ' "$YELLOW" "$RESET" "$DIM" "$RESET"
    read -r NAME < /dev/tty || NAME=""
  fi
  {
    echo "# Memory"
    echo ""
    echo "What nanocode should always know about you, in every project. Edit freely."
    echo "Project notes go in <project>/.nanocode/memory.md (type '# note' in nanocode)."
    echo ""
    echo "## About me"
    if [ -n "$NAME" ]; then echo "- Name: $NAME"; else echo "<!-- - Name: your name -->"; fi
    echo ""
    echo "## Preferences"
  } > "$MEMORY"
  step "Created memory ${DIM}($MEMORY)${RESET}"
fi

# REA (https://github.com/morluto/rea): optional reverse-engineering tools, always the latest version.
# Any problem here only skips REA; nanocode itself is already installed.
node_ok() {
  [ -x "$1" ] && [ "$("$1" -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge 22 ] 2>/dev/null
}

# Sets node/npm: Node 22+ on the system, else our private one in ~/.nanocode/node, else installs it.
find_node() {
  node="$(command -v node || true)"
  npm="$(command -v npm || true)"
  if [ -z "${NANOCODE_PRIVATE_NODE:-}" ] && [ -n "$npm" ] && node_ok "$node"; then
    return 0
  fi
  node="$NODEDIR/bin/node"
  npm="$NODEDIR/bin/npm"
  if node_ok "$node"; then
    return 0
  fi
  install_node && node_ok "$node" && return 0
  return 1
}

# Node.js for REA without admin rights: the official LTS build from nodejs.org, checksum-verified, into
# ~/.nanocode/node. Termux and Alpine (musl, no official builds) use their package manager instead.
install_node() {
  if [ "$PLATFORM" = "termux" ]; then
    step "Installing Node.js for REA ${DIM}(pkg install nodejs)${RESET}"
    pkg install -y nodejs >/dev/null 2>&1 || { warn "REA skipped: couldn't install Node.js (pkg install nodejs)"; return 1; }
    node="$(command -v node)"; npm="$(command -v npm)"
    return 0
  fi
  if [ -e /lib/ld-musl-x86_64.so.1 ] || [ -e /lib/ld-musl-aarch64.so.1 ] || [ -e /lib/ld-musl-i386.so.1 ]; then
    if command -v apk >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then
      step "Installing Node.js for REA ${DIM}(apk add nodejs npm)${RESET}"
      apk add --no-cache nodejs npm >/dev/null 2>&1 || { warn "REA skipped: couldn't install Node.js (apk add nodejs npm)"; return 1; }
      node="$(command -v node)"; npm="$(command -v npm)"
      return 0
    fi
    warn "REA skipped: it needs Node.js 22+ (on Alpine: apk add nodejs npm), then run: nanocode update"
    return 1
  fi
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) target="darwin-arm64" ;;
    Darwin-x86_64) target="darwin-x64" ;;
    Linux-x86_64|Linux-amd64) target="linux-x64" ;;
    Linux-aarch64|Linux-arm64) target="linux-arm64" ;;
    Linux-armv7l) target="linux-armv7l" ;;
    *) warn "REA skipped: no official Node.js build for $(uname -s) $(uname -m). Install Node.js 22+, then run: nanocode update"
       return 1 ;;
  esac
  step "Installing Node.js for REA into $NODEDIR ${DIM}(official LTS, no admin rights needed)${RESET}"
  dl="$NANO_HOME/.node-download"
  rm -rf "$dl"
  mkdir -p "$dl"
  if ! fetch "https://nodejs.org/dist/index.json" "$dl/index.json" 2>/dev/null; then
    rm -rf "$dl"
    warn "REA skipped: couldn't reach nodejs.org to download Node.js"
    return 1
  fi
  version="$("$PY" -c 'import json, sys
print(next(r["version"] for r in json.load(open(sys.argv[1])) if r["lts"] and int(r["version"][1:].split(".")[0]) >= 22))' "$dl/index.json" 2>/dev/null || true)"
  archive="node-$version-$target.tar.gz"
  if [ -z "$version" ] || ! fetch "https://nodejs.org/dist/$version/$archive" "$dl/$archive" 2>/dev/null \
      || ! fetch "https://nodejs.org/dist/$version/SHASUMS256.txt" "$dl/SHASUMS256.txt" 2>/dev/null; then
    rm -rf "$dl"
    warn "REA skipped: couldn't download Node.js"
    return 1
  fi
  if ! "$PY" - "$dl" "$archive" "$NODEDIR" <<'PYCODE'
import hashlib, os, shutil, sys, tarfile
folder, archive, dest = sys.argv[1:4]
expected = next(line.split()[0] for line in open(os.path.join(folder, "SHASUMS256.txt")) if line.split()[-1] == archive)
with open(os.path.join(folder, archive), "rb") as f:
    if hashlib.sha256(f.read()).hexdigest() != expected:
        sys.exit("checksum mismatch")
with tarfile.open(os.path.join(folder, archive)) as tar:
    if hasattr(tarfile, "data_filter"):
        tar.extractall(folder, filter="data")
    else:
        tar.extractall(folder)
shutil.rmtree(dest, ignore_errors=True)
os.replace(os.path.join(folder, archive[: -len(".tar.gz")]), dest)
PYCODE
  then
    rm -rf "$dl"
    warn "REA skipped: the Node.js download didn't verify"
    return 1
  fi
  rm -rf "$dl"
  step "Node.js $version ready ${DIM}(used only by REA)${RESET}"
}

install_rea() {
  if [ -n "$NO_REA" ] || grep -qs '"rea": false' "$NANO_HOME/config.json"; then
    return
  fi
  find_node || return 0
  rea="$TOOLS/rea"
  mkdir -p "$rea"
  step "Installing REA ${DIM}(reverse-engineering tools, latest; skip with --no-rea)${RESET}"
  if ! PATH="$(dirname "$node"):$PATH" npm_config_cache="$rea/.npm-cache" "$npm" install --prefix "$rea" --no-audit --no-fund --loglevel=error \
      rea-agents@latest >"$rea/install.log" 2>&1; then
    rm -rf "$rea/.npm-cache"
    warn "REA failed to install (log: $rea/install.log); nanocode works without it"
    return
  fi
  rm -rf "$rea/.npm-cache"
  entry="$rea/node_modules/rea-agents/scripts/rea.mjs"
  version="$("$node" "$entry" --version 2>/dev/null | head -1 || true)"
  if [ -z "$version" ]; then
    warn "REA installed but doesn't start (log: $rea/install.log); nanocode works without it"
    return
  fi
  printf '{"node": "%s", "entry": "%s", "version": "%s"}\n' "$node" "$entry" "$version" > "$rea/runtime.json"
  step "REA $version ready ${DIM}(loaded only when a task needs it)${RESET}"
}
install_rea

"$BIN/nanocode" --version >/dev/null 2>&1 || die "nanocode doesn't start; run $BIN/nanocode --version to see why"

say ""
say "${GREEN}${BOLD}nanocode $("$BIN/nanocode" --version | cut -d' ' -f2) is installed.${RESET}"
if [ -n "$LINKED" ]; then
  say "Run it from any project folder:  ${BOLD}cd your-project && nanocode${RESET}"
else
  say "Open a new terminal (or run: ${BOLD}export PATH=\"$BIN:\$PATH\"${RESET}), then:  ${BOLD}nanocode${RESET}"
fi
say ""
say "${DIM}The first start asks for a provider: Claude subscription, OpenRouter, or Ollama (local).${RESET}"
say "${DIM}Later: nanocode setup · nanocode doctor · nanocode update · nanocode uninstall${RESET}"
if ! command -v claude >/dev/null 2>&1; then
  say "${DIM}  Claude subscription needs Claude Code: curl -fsSL https://claude.ai/install.sh | bash${RESET}"
fi
if ! command -v ollama >/dev/null 2>&1; then
  say "${DIM}  Local models need Ollama: https://ollama.com/download${RESET}"
fi
