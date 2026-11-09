# Reverse engineering (REA)

nanocode comes with [REA](https://github.com/morluto/rea), an MCP server for reverse engineering: native binaries, apps, Android APKs, Electron and JavaScript bundles, websites, firmware and devices.

Ask things like:

- *"what does this binary do?"*
- *"which API endpoints does this Electron app call?"*
- *"what changed between these two versions of the app?"*

## How it's set up

- **Installed and updated automatically.** The installer installs the latest REA, and `nanocode update` updates it. REA needs Node.js 22+; if you don't have it, the installer downloads the official Node.js LTS into `~/.nanocode/node` (checksum verified, no admin rights, used only by REA).
- **Skip it** with `sh install.sh --no-rea` (Windows: `-NoRea`).

## How the model uses it

REA has about 230 tools, too many to hand to a model at once. nanocode gives it three small tools instead:

| Tool | What it does |
|---|---|
| `rea_search` | Find a REA tool by keywords |
| `rea_schema` | Show its description and arguments |
| `rea_call` | Run it |

REA starts on first use, so it costs nothing until it's needed. Its own `reverse-engineer-anything` skill teaches the model the workflows.

## Safety and failures

- **Asks permission** before REA actions that change something: installing an APK, running Frida, executing commands.
- **Fails safe.** If REA is missing, crashes or times out, the model gets an error and nanocode keeps working. A crashed REA restarts on the next call.
- `/rea` shows its status; `/rea off` and `/rea on` turn it off or on (stored as `"rea"` in the config).

## Optional programs

Some features need programs you install yourself, because they're large, platform-specific or paid:

| For | Install |
|---|---|
| Native binaries (decompiling, functions) | [Ghidra](https://ghidra-sre.org) (free), or Hopper / IDA |
| Android APKs | [JADX](https://github.com/skylot/jadx) and a Java JDK |
| Android devices | adb ([platform-tools](https://developer.android.com/tools/releases/platform-tools)) |
| APK resources | [Apktool](https://apktool.org) |
| Websites | Chrome or Chromium |
| Firmware (Linux) | Binwalk or Unblob |
| Packet captures | TShark ([Wireshark](https://www.wireshark.org)) |

When a task needs one you don't have, nanocode says what to install and carries on with what works without it. Static analysis of .NET, PyInstaller, Electron and JavaScript apps, and of Mach-O, ELF and PE files, needs nothing extra.
