import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
import nanokit  # noqa: E402
from nanokit import plugins  # noqa: E402
from test_nanocode import plain, run  # noqa: E402

PLUGIN = '''
import nanocode as nc
from nanokit import command, cli

@command("/hello-{tag}", "say hello")
async def hello(arg):
    nc.out("hello " + arg)

@cli("greet-{tag}", "greet")
def greet(argv):
    print("greetings", *argv)
    return 0
'''


@pytest.fixture(autouse=True)
def fresh():
    plugins.PLUGINS.clear()
    plugins.STATE["loaded"] = False
    before_cmds, before_cli = dict(nanokit.COMMANDS), dict(nanokit.CLI)
    yield
    for name in set(nanokit.COMMANDS) - set(before_cmds):
        nanokit.COMMANDS.pop(name)
        nc.COMMANDS.pop(name, None)
    for name in set(nanokit.CLI) - set(before_cli):
        nanokit.CLI.pop(name)
    for name in [m for m in sys.modules if m.startswith("nanocode_plugin_")]:
        sys.modules.pop(name)


def write_plugin(root, name, text):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(text)
    return root / f"{name}.py"


def test_global_plugins_load_and_errors_are_kept(sandbox):
    write_plugin(nc.home_dir() / "plugins", "good", PLUGIN.format(tag="g"))
    write_plugin(nc.home_dir() / "plugins", "broken", "x = 1\nraise ValueError('nope')\n")
    assert plugins.load_all() == []
    assert "/hello-g" in nanokit.COMMANDS and nc.COMMANDS["/hello-g"] == "say hello"
    run(nanokit.COMMANDS["/hello-g"]["fn"]("there"))
    assert "hello there" in plain(nc.st.output)
    run(plugins.plugins_command(""))
    screen = plain(nc.st.output)
    assert "good" in screen and "loaded" in screen and "ValueError: nope (line 2)" in screen


def test_project_plugins_wait_for_trust(sandbox):
    path = write_plugin(nc.project_dir() / "plugins", "proj", PLUGIN.format(tag="p"))
    waiting = plugins.load_all()
    assert waiting == [path] and "/hello-p" not in nanokit.COMMANDS
    run(plugins.plugins_command(""))
    assert "not trusted yet" in plain(nc.st.output)
    run(plugins.plugins_command("trust"))
    assert "/hello-p" in nanokit.COMMANDS
    assert plugins.file_hash(path) in nc.st.config["trusted_plugins"]
    assert plugins.file_hash(path) in nc.load_config().get("trusted_plugins", [])   # saved


def test_trusted_hash_loads_and_a_change_asks_again(sandbox):
    path = write_plugin(nc.project_dir() / "plugins", "proj", PLUGIN.format(tag="t"))
    nc.st.config["trusted_plugins"] = [plugins.file_hash(path)]
    assert plugins.load_all() == [] and "/hello-t" in nanokit.COMMANDS
    plugins.PLUGINS.clear()
    path.write_text(PLUGIN.format(tag="t") + "\n# changed\n")
    assert plugins.load_all() == [path]


def test_ask_trust_uses_the_dialog(sandbox):
    path = write_plugin(nc.project_dir() / "plugins", "proj", PLUGIN.format(tag="d"))
    opened = []
    nc.st.ui["open_dialog"] = lambda title, items: opened.append((title, items))
    plugins.ask_trust(plugins.load_all())
    title, items = opened[0]
    assert "proj.py" in title and [i[0] for i in items] == ["Trust and load", "Don't load"]
    items[1][2]()
    assert plugins.PLUGINS[str(path)]["status"] == "declined"
    items[0][2]()
    assert "/hello-d" in nanokit.COMMANDS


def test_plugin_cli(sandbox, capsys):
    write_plugin(nc.home_dir() / "plugins", "tool", PLUGIN.format(tag="c"))
    assert plugins.plugin_cli(["greet-c", "a", "b"]) == 0
    assert "greetings a b" in capsys.readouterr().out
    assert plugins.plugin_cli([]) == 2
