# Install and update

## Install

macOS, Linux, WSL, Android (Termux):

```sh
curl -fsSL https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master/install.ps1 | iex
```

Installer options:

| Option | What it does |
|---|---|
| `sh install.sh --no-rea` (Windows `-NoRea`) | Skip the REA reverse-engineering server |
| `NANOCODE_HOME=/path` | Install somewhere other than `~/.nanocode` |

## Where things go

Everything lives in `~/.nanocode`. Nothing else on your system changes except one PATH line.

```
~/.nanocode/
  app/            nanocode.py
  venv/           private Python environment with nanocode's packages
  python/         private Python 3.12, only if you had no Python 3.10+
  node/           private Node.js LTS, only for REA and only if you had no Node 22+
  tools/rea/      the REA MCP server
  config.json     your settings (readable only by you)
  NANOCODE.md     your global memory
  skills/         your global skills
  update.json     cached update check
```

**Python.** If you have Python 3.10 or newer, it's used. If not, the installer downloads a private Python 3.12 with [uv](https://github.com/astral-sh/uv). No admin rights needed. On Termux it runs `pkg install python`.

**Node.js.** Only REA needs it (version 22+). If yours is missing or too old, the installer downloads the official Node.js LTS into `~/.nanocode/node` and checks it against the published checksum.

## Update

```sh
nanocode update     # latest nanocode, and REA
nanocode version    # your version, and whether a newer one exists
```

nanocode also checks for a new version once a day, in the background. It reads the `VERSION` file on GitHub, so the check is a tiny request. When there's a newer version:

- The start screen shows `/update to install vX.Y.Z`.
- `/update` (or clicking that line) offers **Update** or **Skip**.
  - **Update** installs in the background while you keep working. `/exit` then restarts nanocode on the new version.
  - **Skip** hides the reminder for that version.
- On Windows, Update closes nanocode first, installs, and you start it again.

`/version` checks right away. To turn the check off, set `"update_check": false` in the config, or `NANOCODE_NO_UPDATE_CHECK=1`.

## Uninstall

```sh
nanocode uninstall           # remove the app; keep config, memory and skills
nanocode uninstall --purge   # remove everything in ~/.nanocode
```
