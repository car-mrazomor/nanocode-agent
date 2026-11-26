import sys
from pathlib import Path

from prompt_toolkit.key_binding import KeyBindings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import keybinds  # noqa: E402
from test_nanocode import plain, run  # noqa: E402


def test_parse_keys():
    assert keybinds.parse_keys("c-t") == ("c-t",)
    assert keybinds.parse_keys("ctrl+x w") == ("c-x", "w")
    assert keybinds.parse_keys("Ctrl+X Ctrl+E") == ("c-x", "c-e")
    assert keybinds.parse_keys("alt+m") == ("escape", "m")
    assert keybinds.parse_keys("escape n") == ("escape", "n")


def bound(kb):
    return {tuple(str(getattr(k, "value", k)) for k in b.keys): b for b in kb.bindings}


def test_configured_keys_are_bound_and_call_their_action(sandbox):
    calls = []
    nc.st.config = {"keybinds": {"palette": "c-k", "tabs": ["alt+t", "c-x z"], "nope": "c-q", "help": "c-x bogus"}}
    kb = KeyBindings()
    keybinds.bind(kb, {"palette": lambda: calls.append("palette"), "tabs_dialog": lambda: calls.append("tabs")})
    keys = bound(kb)
    assert ("c-k",) in keys and ("escape", "t") in keys and ("c-x", "z") in keys
    keys[("c-k",)].handler(None)
    keys[("c-x", "z")].handler(None)
    assert calls == ["palette", "tabs"]
    assert {(a, why) for a, _, why in keybinds.PROBLEMS} >= {("nope", "unknown action")}
    assert any(a == "help" for a, _, _ in keybinds.PROBLEMS)
    run(keybinds.keybinds_command(""))
    screen = plain(nc.st.output)
    assert "palette" in screen and "c-k" in screen and "nope: c-q: unknown action" in screen


def test_keybinds_work_in_the_app(sandbox):
    """A configured key (Ctrl+N here) wins over a built-in one and runs its action; the palette is reachable."""
    import asyncio
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    nc.st.config = {"keybinds": {"new_tab": "c-n"}}
    seen = {}

    async def scenario():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            app = nc.build_app()
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.2)
            inp.send_text("\x0e")                           # ctrl+n
            await asyncio.sleep(0.2)
            seen["tabs"] = len(nc.st.tabs)
            seen["palette"] = callable(nc.st.ui.get("palette"))
            app.exit()
            await task
    run(scenario())
    assert seen == {"tabs": 2, "palette": True}
