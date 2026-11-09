# Development

nanocode is one file, `nanocode.py`, so you can read and change all of it.

## Run from source

```sh
git clone https://github.com/car-mrazomor/nanocode-agent
cd nanocode-agent
pip install -r requirements.txt pytest
python nanocode.py
```

## Tests

```sh
python -m pytest -q tests
```

The suite in `tests/test_nanocode.py` covers the tools, providers, sessions, config, CLI, the interface (scrolling, selection, wrapping), thinking, updates, MCP, web, compare, sub-agents and email (against a fake IMAP server). CI runs it on Linux, macOS and Windows.

## Layout

```
nanocode.py      the whole agent
install.sh       installer for macOS / Linux / WSL / Termux
install.ps1      installer for Windows
VERSION          the current version, read by the update check
tests/           pytest suite
docs/            the website (index.html) and these docs
assets/          logos
```

## Adding a tool

Tools are plain async functions registered with `@tool`:

```python
@tool("word_count", "Count the words in a file.",
      {"path": {"type": "string", "description": "file to count"}}, ["path"])
async def word_count(args):
    return str(len(Path(args["path"]).read_text().split()))
```

The name, description and JSON-schema arguments are sent to every provider. Add the name to `READ_ONLY_TOOLS` if it doesn't change anything, and call `await allowed(name, preview)` before it does.

## Releases

Every change moves the version: `VERSION` and `__version__` in `nanocode.py` (a test checks they match). Patch for fixes, minor for features. Each version gets a `vX.Y.Z` tag. The in-app update check reads `VERSION` on `master`.
