# Plugins

A plugin is a Python file that adds to nanocode: slash commands, tools for the model, hooks, command line commands. It uses the same small API as nanocode's own feature modules (see `nanokit/__init__.py`).

| Where | Loaded |
|---|---|
| `~/.nanocode/plugins/*.py` | Always: they're yours |
| `.nanocode/plugins/*.py` | Once you trust them (see below) |

```python
# ~/.nanocode/plugins/hello.py
import nanocode as nc
from nanokit import command, on, cli


@command("/hello", "say hello")
async def hello(arg):
    nc.out(f"hello {arg or 'world'}")


@nc.tool("word_count", "Count the words in a file.", {"path": {"type": "string"}}, ["path"])
async def word_count(args):
    return str(len(nc.read_text(nc.resolve(args["path"])).split()))


@on("turn_end")
def done(text, error):
    return None


@cli("hello", "say hello from the command line")
def hello_cli(argv):
    print("hello", *argv)
    return 0
```

- Plugins load when nanocode starts (with `nanocode -p`, on the first message). A plugin that fails is skipped; `/plugins` shows each plugin and the error of any that failed.
- `nanocode plugin <name> [args]` runs a plugin's `@cli` command (here `nanocode plugin hello`).
- Every hook in `nanokit.EVENTS` is available to `@on`, except `startup` (plugins load during it).
- Files whose name starts with `_` are skipped, so helpers can live next to plugins.

## Trusting project plugins

A project's plugins are code from whoever wrote the repository, so nanocode asks before running them: when you open a project that has some, a dialog asks whether to trust and load them. Your answer is remembered by each file's hash (`trusted_plugins` in the config): an unchanged plugin loads without asking, and a changed one asks again. If you said no, `/plugins trust` loads them later.
