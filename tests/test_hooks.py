import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import hooks  # noqa: E402
from test_nanocode import call, plain, run  # noqa: E402


def py(code):
    """A shell command running Python code (works with sh and cmd.exe)."""
    return f'"{sys.executable}" -c "{code}"'


def test_matching():
    assert hooks.matches("*", "bash") and hooks.matches("", "read")
    assert hooks.matches("edit|write", "write") and not hooks.matches("edit|write", "rewrite")
    assert hooks.matches("mcp__gh__*", "mcp__gh__issues")


def test_hooks_for_reads_the_config(sandbox):
    nc.st.config = {"hooks": {"before_tool": [{"match": "bash", "run": "a"}, {"run": "b"}, {"match": "x"}],
                              "turn_end": "c"}}
    assert [h["run"] for h in hooks.hooks_for("before_tool", "bash")] == ["a", "b"]
    assert [h["run"] for h in hooks.hooks_for("before_tool", "read")] == ["b"]
    assert [h["run"] for h in hooks.hooks_for("turn_end")] == ["c"]
    assert hooks.hooks_for("startup") == []


def test_before_tool_nonzero_exit_denies_with_its_output(sandbox):
    code = "import os,sys; print('no rm please: ' + os.environ['NANOCODE_TOOL']); sys.exit(1)"
    nc.st.config = {"hooks": {"before_tool": [{"match": "bash", "run": py(code)}]}}
    text, err = call("bash", command="echo should-not-run")
    assert err and "no rm please: bash" in text and "should-not-run" not in text
    text, err = call("glob", pattern="*")                    # other tools aren't matched
    assert not err


def test_after_tool_gets_env_and_adds_its_output(sandbox):
    code = ("import os,json,sys; d=json.load(sys.stdin); "
            "print(os.environ['NANOCODE_FILE'].endswith('a.txt'), json.loads(os.environ['NANOCODE_ARGS'])['path'], "
            "d['name'], 'created' in os.environ['NANOCODE_RESULT'])")
    nc.st.config = {"hooks": {"after_tool": [{"match": "write", "run": py(code)}]}}
    text, err = call("write", path="a.txt", content="x")
    assert not err and "[after_tool hook] True a.txt write True" in text


def test_timeout_and_turn_end_failure_are_shown(sandbox):
    nc.st.config = {"hooks": {"turn_end": [{"run": py("import time; time.sleep(5)"), "timeout": 0.3},
                                           {"run": py("import sys; print('oops'); sys.exit(3)")}]}}
    run(hooks.turn_end("hi", None))
    screen = plain(nc.st.output)
    assert "timed out after 0.3s" in screen and "exit code 3: oops" in screen


def test_hooks_command_lists_them(sandbox):
    nc.st.config = {"hooks": {"startup": [{"run": "git fetch"}]}}
    run(hooks.hooks_command(""))
    assert "startup" in plain(nc.st.output) and "git fetch" in plain(nc.st.output)


def test_before_tool_fails_closed(sandbox, monkeypatch):
    nc.st.config = {"hooks": {"before_tool": [{"match": "bash", "run": py("import time; time.sleep(5)"),
                                               "timeout": 0.3}]}}
    text, err = call("bash", command="echo should-not-run")
    assert err and "should-not-run" not in text and "timed out" in text

    async def cant_start(entry, event, data):
        return None, "couldn't start: no such shell"
    monkeypatch.setattr(hooks, "run_hook", cant_start)
    assert "deny" in run(hooks.before_tool("bash", {"command": "ls"}))

    async def broken(entry, event, data):
        raise ValueError("bad environment")
    monkeypatch.setattr(hooks, "run_hook", broken)
    result = run(hooks.before_tool("bash", {"command": "ls"}))
    assert "deny" in result and "bad environment" in result["deny"]
