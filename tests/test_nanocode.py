import asyncio
import json
import time
import os
import shutil
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nanocode as nc  # noqa: E402


def plain(text):
    return nc.ANSI_RE.sub("", text)


def run(coro):
    return asyncio.run(coro)


def call(tool_name, **args):
    return run(nc.run_tool(tool_name, args))


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    userhome = tmp_path / "userhome"
    project = tmp_path / "project"
    userhome.mkdir()
    project.mkdir()
    monkeypatch.setenv("NANOCODE_HOME", str(tmp_path / "nanocode-home"))
    monkeypatch.setenv("HOME", str(userhome))
    monkeypatch.setenv("USERPROFILE", str(userhome))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(userhome / ".config"))
    for var in ("MODEL", "OPENROUTER_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(project)
    nc.st.__init__()
    nc.st.auto_approve = True
    yield project


def mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(nc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def sse(chunks):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + ": OPENROUTER PROCESSING\n\ndata: [DONE]\n\n"
    return httpx.Response(200, text=body)


def tool_call_chunk(call_id, name, args):
    return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "function": {
        "name": name, "arguments": json.dumps(args)}}]}}]}


# --- tools ---

def test_read_numbers_and_offset(sandbox):
    Path("f.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    text, err = call("read", path="f.txt", offset=3, limit=2)
    assert not err
    assert "3│line3" in text and "4│line4" in text and "line5" not in text
    assert "lines 3-4 of 10" in text


def test_read_missing_suggests_and_handles_dirs_and_binaries(sandbox):
    Path("config.py").write_text("x = 1\n")
    text, err = call("read", path="confg.py")
    assert err and "Did you mean" in text and "config.py" in text
    Path("pkg").mkdir()
    Path("pkg/a.py").write_text("")
    text, err = call("read", path="pkg")
    assert not err and "a.py" in text
    Path("img.bin").write_bytes(b"\x89PNG\0\0data")
    text, err = call("read", path="img.bin")
    assert not err and "binary" in text


def test_write_edit_undo(sandbox):
    text, err = call("write", path="sub/a.txt", content="hello\nworld\n")
    assert not err and "created" in text
    text, err = call("edit", path="sub/a.txt", old="world", new="there")
    assert not err
    assert Path("sub/a.txt").read_text() == "hello\nthere\n"
    nc.cmd_undo()
    assert Path("sub/a.txt").read_text() == "hello\nworld\n"
    nc.cmd_undo()
    assert not Path("sub/a.txt").exists()
    nc.cmd_undo()
    assert "nothing to undo" in plain(nc.st.output)


def test_edit_errors_and_replace_all(sandbox):
    Path("a.py").write_text("x = 1\nx = 1\n")
    text, err = call("edit", path="a.py", old="y = 2", new="z")
    assert err and "not found" in text
    text, err = call("edit", path="a.py", old="x = 1", new="x = 2")
    assert err and "appears 2 times" in text
    text, err = call("edit", path="a.py", old="x = 1", new="x = 2", all=True)
    assert not err and Path("a.py").read_text() == "x = 2\nx = 2\n"
    text, err = call("edit", path="a.py", old="", new="x")
    assert err


def test_edit_forgives_line_numbers_and_extra_indent(sandbox):
    Path("calc.py").write_text("def add(a, b):\n    return a - b\n")
    text, err = call("edit", path="calc.py", old="    def add(a, b):\n        return a - b",
                     new="    def add(a, b):\n        return a + b")
    assert not err, text
    assert Path("calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    text, err = call("edit", path="calc.py", old="2│    return a + b", new="2│    return a * b")
    assert not err, text
    assert Path("calc.py").read_text() == "def add(a, b):\n    return a * b\n"


def test_edit_keeps_crlf_line_endings(sandbox):
    Path("w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    text, err = call("edit", path="w.txt", old="one\ntwo", new="1\n2")
    assert not err, text
    assert Path("w.txt").read_bytes() == b"1\r\n2\r\nthree\r\n"


def test_glob_skips_vendored_and_supports_prefixes(sandbox):
    for p in ("src/a.py", "src/b/c.py", "node_modules/x.py", ".github/w.yml"):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text("")
    text, _ = call("glob", pattern="**/*.py")
    names = {Path(line).as_posix() for line in text.splitlines()}
    assert names == {"src/a.py", "src/b/c.py"}
    text, _ = call("glob", pattern="src/*.py")
    assert [Path(t).as_posix() for t in text.splitlines()] == ["src/a.py"]
    text, _ = call("glob", pattern="./.github/*.yml")
    assert "w.yml" in text
    text, _ = call("glob", pattern="*.rs")
    assert text == "no files found"


def test_glob_to_regex():
    assert nc.glob_to_regex("**/*.py").match("a/b/c.py")
    assert nc.glob_to_regex("**/*.py").match("c.py")
    assert not nc.glob_to_regex("*.py").match("a/c.py")
    assert nc.glob_to_regex("src/?.ts").match("src/a.ts")


def test_grep_builtin(sandbox, monkeypatch):
    monkeypatch.setattr(nc.shutil, "which", lambda name: None)
    Path("a.py").write_text("def foo():\n    return 1\n")
    Path("b.txt").write_text("FOO bar\n")
    Path("node_modules").mkdir()
    Path("node_modules/c.py").write_text("def foo(): pass\n")
    text, _ = call("grep", pattern="def foo")
    assert "a.py:1:def foo():" in text.replace("\\", "/") and "node_modules" not in text
    text, _ = call("grep", pattern="foo", ignore_case=True, include="*.txt")
    assert "b.txt:1:FOO bar" in text and "a.py" not in text
    text, err = call("grep", pattern="(")
    assert err and "invalid regex" in text


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep not installed")
def test_grep_ripgrep(sandbox):
    Path("a.py").write_text("hello world\n")
    text, _ = call("grep", pattern="hello")
    assert "a.py:1:hello world" in text


def test_bash_output_exit_code_and_timeout(sandbox):
    py = f'"{sys.executable}"'
    text, err = call("bash", command=f'{py} -c "print(42)"')
    assert not err and "42" in text and "[exit code 0]" in text
    text, _ = call("bash", command=f'{py} -c "import sys; sys.exit(3)"')
    assert "[exit code 3]" in text
    text, _ = call("bash", command=f'{py} -c "import time; time.sleep(20)"', timeout=1)
    assert "timed out after 1s" in text


def test_permissions_in_headless_mode(sandbox):
    nc.st.headless = True
    nc.st.auto_approve = False
    text, _ = call("write", path="x.txt", content="hi")
    assert text == nc.DENIED and not Path("x.txt").exists()
    nc.st.auto_approve = True
    text, err = call("write", path="x.txt", content="hi")
    assert not err and Path("x.txt").exists()
    text, _ = call("bash", command="rm -rf / --no-preserve-root")
    assert text == nc.DENIED


def test_tool_input_problems_dont_crash(sandbox):
    text, err = call("nope", a=1)
    assert err and "unknown tool" in text
    text, err = call("read")
    assert err and "missing required" in text
    assert nc.st.turn_errors == 2


def test_cap_keeps_head_and_tail():
    text = "a" * 50_000 + "END"
    capped = nc.cap(text, 1000)
    assert len(capped) < 1200 and capped.endswith("END") and "characters cut" in capped


# --- memory, skills, prompt ---

def test_memory_dedupes_and_finds_name(sandbox):
    nc.add_memory("we use pnpm")
    nc.add_memory("we use  pnpm")
    assert nc.project_memory_file().read_text().count("we use pnpm") == 1
    nc.add_memory("Name: Ivan", scope="global")
    assert nc.user_name() == "Ivan"
    assert (nc.project_dir() / ".gitignore").read_text().count("memory.md") == 1
    labels = [label for label, _, _ in nc.memory_files()]
    assert labels == ["global", "project"]


def test_save_skill_and_load_it(sandbox):
    text, err = call("save_skill", name="Run Tests!", description="how to run tests\nhere",
                     instructions="1. pytest -q")
    assert not err and "run-tests" in text
    skills = nc.list_skills()
    assert skills["run-tests"]["description"] == "how to run tests here"
    assert "pytest -q" in nc.load_skill("run-tests")
    prompt = nc.build_system_prompt()
    assert "- run-tests: how to run tests here" in prompt and "save_skill" in prompt
    text, err = call("skill", name="missing")
    assert err and "run-tests" in text


def test_remember_tool_global(sandbox):
    text, err = call("remember", fact="prefers tabs", scope="global")
    assert not err
    assert "prefers tabs" in nc.global_memory_file().read_text()
    assert "prefers tabs" in nc.build_system_prompt()


# --- history ---

def test_repair_history_drops_unfinished_calls():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "function": {"name": "x", "arguments": "{}"}},
                                                               {"id": "3", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "2", "content": "ok"},
    ]
    assert [m["role"] for m in nc.repair_history(msgs)] == ["user", "assistant", "tool"]


def test_fit_history_trims_old_output_then_turns():
    msgs = []
    for i in range(6):
        msgs += [{"role": "user", "content": f"q{i}"},
                 {"role": "assistant", "content": None, "tool_calls": [{"id": str(i), "function": {"name": "read", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": str(i), "content": "x" * 4000},
                 {"role": "assistant", "content": f"a{i}"}]
    fitted, trimmed = nc.fit_history(msgs, 1500)
    assert trimmed and fitted[0]["role"] == "user" and "trimmed" in fitted[0]["content"]
    assert nc.estimate_tokens(fitted) <= 1500 or len([m for m in fitted if m["role"] == "user"]) == 1
    assert fitted[-1]["content"] == "a5"
    assert [m["role"] for m in nc.repair_history(fitted)] == [m["role"] for m in fitted]
    same, trimmed = nc.fit_history(msgs[:4], 10_000)
    assert not trimmed and same == msgs[:4]


# --- providers ---

def test_openrouter_turn_with_retry_and_tool(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "openrouter/auto-code", "k"
    Path("app.py").write_text("")
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        if len(requests) == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        if len(requests) == 2:
            return sse([{"model": "z-ai/glm-5.3", "choices": [{"delta": {"content": "Looking."}}]},
                        tool_call_chunk("c1", "glob", {"pattern": "*.py"}),
                        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5, "cost": 0.001}}])
        return sse([{"model": "z-ai/glm-5.3", "choices": [{"delta": {"content": "One **file**."}}]},
                    {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 3, "cost": 0.002}}])

    mock_http(monkeypatch, handler)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(nc.asyncio, "sleep", no_sleep)
    run(nc.send("how many py files?", display="how many py files?"))
    assert [m["role"] for m in nc.st.messages] == ["user", "assistant", "tool", "assistant"]
    assert nc.st.messages[2]["content"] == "app.py"
    assert requests[1]["messages"][0]["role"] == "system" and requests[1]["tools"]
    screen = plain(nc.st.output)
    assert "retrying" in screen and "One file." in screen and "routed to z-ai/glm-5.3" in screen
    assert nc.st.usage["cost"] == pytest.approx(0.003)
    assert list(nc.sessions_dir().glob("*.json"))


def test_openrouter_auth_error_is_clear(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "x/y", "bad"
    mock_http(monkeypatch, lambda req: httpx.Response(401, json={"error": {"message": "No auth"}}))
    run(nc.run_input("hi"))
    assert "nanocode setup" in nc.st.last_error
    assert nc.st.messages == [{"role": "user", "content": "hi"}]


def test_ollama_turn(sandbox, monkeypatch):
    nc.st.backend, nc.st.model = "ollama", "qwen3"
    Path("a.py").write_text("")
    requests = []

    def handler(req):
        body = json.loads(req.content)
        requests.append(body)
        if len(requests) == 1:
            lines = [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "glob", "arguments": {"pattern": "*.py"}}}]}, "done": False},
                {"message": {"content": ""}, "done": True, "prompt_eval_count": 50, "eval_count": 7}]
        else:
            lines = [{"message": {"content": "There is "}, "done": False},
                     {"message": {"content": "one."}, "done": True, "prompt_eval_count": 60, "eval_count": 3}]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines) + "\n")

    mock_http(monkeypatch, handler)
    run(nc.send("count py files"))
    assert requests[0]["options"]["num_ctx"] == nc.st.num_ctx
    tool_msg = requests[1]["messages"][-1]
    assert tool_msg == {"role": "tool", "content": "a.py", "tool_name": "glob"}
    assert isinstance(requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"], dict)
    assert "There is one." in plain(nc.st.output)


def test_ollama_down_message(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.ollama_url = "ollama", "qwen3", "http://localhost:9"

    def handler(req):
        raise httpx.ConnectError("refused")

    mock_http(monkeypatch, handler)
    run(nc.run_input("hi"))
    assert "can't reach Ollama" in nc.st.last_error


def test_compact_replaces_history(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "m", "k"
    nc.st.messages = [{"role": "user", "content": "build x " * 200}, {"role": "assistant", "content": "done " * 200}]
    mock_http(monkeypatch, lambda req: sse([{"choices": [{"delta": {"content": "We built x."}}]}]))
    run(nc.run_input("/compact"))
    assert len(nc.st.messages) == 2 and "We built x." in nc.st.messages[0]["content"]


def test_sessions_roundtrip(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "m", "k"
    mock_http(monkeypatch, lambda req: sse([{"choices": [{"delta": {"content": "Hi Ivan."}}]}]))
    nc.new_session()
    run(nc.run_input("my name is Ivan"))
    nc.st.__init__()
    nc.st.backend = "api"
    run(nc.run_input("/resume"))
    assert "my name is Ivan" in plain(nc.st.output)
    run(nc.run_input("/resume 1"))
    assert [m["role"] for m in nc.st.messages] == ["user", "assistant"]
    assert "Resumed" in plain(nc.st.output)


# --- commands ---

def test_commands(sandbox):
    run(nc.run_input("/help"))
    assert "/compact" in plain(nc.st.output)
    run(nc.run_input("/nope"))
    assert "unknown command /nope" in plain(nc.st.output)
    run(nc.run_input("# use tabs"))
    assert "use tabs" in nc.project_memory_file().read_text()
    run(nc.run_input("/auto off"))
    assert nc.st.auto_approve is False
    run(nc.run_input("/clear"))
    assert "New chat" in plain(nc.st.output)


def test_expand_mentions(sandbox):
    Path("notes.md").write_text("secret sauce")
    text = nc.expand_mentions("look at @notes.md, please")
    assert '<file path="notes.md,">' not in text and "secret sauce" in text


# --- config, setup, cli ---

def test_setup_wizard_ollama_offline(sandbox, monkeypatch):
    answers = iter(["3", "", "", "4096", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    def offline(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(nc, "ollama_models", offline)
    nc.setup_wizard()
    cfg = json.loads(nc.config_file().read_text())
    assert cfg["provider"] == "ollama" and cfg["models"]["ollama"] == "qwen3"
    assert cfg["ollama_num_ctx"] == 4096 and cfg["permissions"] == "ask"
    if os.name != "nt":
        assert oct(nc.config_file().stat().st_mode & 0o777) == "0o600"


def test_apply_config_env_overrides(sandbox, monkeypatch):
    nc.st.config = {"provider": "openrouter", "models": {"openrouter": "z-ai/glm-5.3"}}
    assert "OpenRouter API key" in nc.apply_config()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    monkeypatch.setenv("MODEL", "x-ai/grok-4.7")
    assert nc.apply_config() is None and nc.st.model == "x-ai/grok-4.7" and nc.st.backend == "api"
    monkeypatch.setattr(nc, "refresh_ollama_models", lambda: [])
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    assert nc.apply_config("ollama") is None and nc.st.ollama_url == "http://127.0.0.1:11434"
    assert "unknown provider" in nc.apply_config("nope")


def test_migrate_legacy(sandbox, tmp_path, monkeypatch):
    legacy = tmp_path / "userhome" / ".config" / "nanocode"
    (legacy).mkdir(parents=True)
    (legacy / "NANOCODE.md").write_text("- Name: Ivan")
    nc.migrate_legacy()
    assert (legacy / "NANOCODE.md").exists(), "a custom NANOCODE_HOME must not take the real files"
    (legacy / "NANOCODE.md").unlink()
    monkeypatch.delenv("NANOCODE_HOME")
    monkeypatch.setattr(nc, "home_dir", lambda: tmp_path / "userhome" / ".nanocode")
    (legacy / "skills" / "deploy").mkdir(parents=True)
    (legacy / "skills" / "deploy" / "SKILL.md").write_text("---\nname: deploy\n---\nx")
    (legacy / "config.json").write_text('{"provider": "claude"}')
    (legacy / "NANOCODE.md").write_text("- Name: Ivan")
    nc.migrate_legacy()
    assert nc.load_config() == {"provider": "claude"}
    assert nc.global_memory_file().read_text() == "- Name: Ivan"
    assert (nc.home_dir() / "skills" / "deploy" / "SKILL.md").exists()
    assert not legacy.exists()


def test_cli_version_and_headless(sandbox, monkeypatch, capsys):
    assert nc.main(["--version"]) == 0
    assert nc.__version__ in capsys.readouterr().out
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    mock_http(monkeypatch, lambda req: sse([{"choices": [{"delta": {"content": "4"}}]}]))
    assert nc.main(["-p", "2+2?", "--provider", "openrouter", "-m", "m"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "4"
    assert "in ·" in captured.err


def test_sdk_tools_wrap_our_tools(sandbox):
    if nc.sdk is None:
        pytest.skip("claude-agent-sdk not installed")
    tools = {t.name: t for t in nc.sdk_tools()}
    assert set(tools) == {t['name'] for t in nc.active_tools()}
    Path("a.txt").write_text("hi\n")
    result = run(tools["read"].handler({"path": "a.txt"}))
    assert result["is_error"] is False and "hi" in result["content"][0]["text"]


# --- REA (reverse-engineering MCP server), with a fake server ---

def install_fake_rea(tmp_path):
    rea_dir = nc.rea_dir()
    skill = rea_dir / "node_modules" / "rea-agents" / "skills" / "reverse-engineer-anything"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: reverse-engineer-anything\ndescription: reverse engineer things\n---\n"
                                    "Start with inspect_artifact.")
    entry = Path(__file__).with_name("fake_rea.py")
    (rea_dir / "runtime.json").write_text(json.dumps({"node": sys.executable, "entry": str(entry), "version": "0.0-test"}))


def test_rea_hidden_and_harmless_when_not_installed(sandbox):
    names = {t["function"]["name"] for t in nc.api_tools()}
    assert "rea_call" not in names and "read" in names
    assert "reverse engineering" not in nc.build_system_prompt().lower()
    text, err = call("rea_search", query="apk")
    assert err and "isn't installed" in text


def test_rea_search_schema_call(sandbox, tmp_path):
    install_fake_rea(tmp_path)

    async def scenario():
        try:
            found, _ = await nc.run_tool("rea_search", {"query": "android apk install"})
            schema, _ = await nc.run_tool("rea_schema", {"tool": "inspect_artifact"})
            bad, bad_err = await nc.run_tool("rea_call", {"tool": "inspect_artifact", "arguments": {}})
            good, good_err = await nc.run_tool("rea_call", {"tool": "inspect_artifact", "arguments": {"path": "/bin/ls"}})
            typo, typo_err = await nc.run_tool("rea_call", {"tool": "inspect_artifcat"})
            return found, schema, bad, bad_err, good, good_err, typo, typo_err
        finally:
            await nc.st.rea.close()

    found, schema, bad, bad_err, good, good_err, typo, typo_err = run(scenario())
    assert found.splitlines()[0].startswith("install_adb_package") and "asks permission" in found
    assert '"required": ["path"]' in schema
    assert bad_err and "arguments schema" in bad
    assert not good_err and 'inspect_artifact ok: {"path": "/bin/ls"}' in good
    assert typo_err and "Did you mean: inspect_artifact" in typo
    assert {"rea_search", "rea_schema", "rea_call"} <= {t["function"]["name"] for t in nc.api_tools()}
    assert "rea_search" in nc.build_system_prompt()
    assert "reverse-engineer-anything" in nc.list_skills()
    assert "rea_call(tool=" in nc.load_skill("reverse-engineer-anything")


def test_rea_risky_tools_ask_and_crashes_recover(sandbox, tmp_path):
    install_fake_rea(tmp_path)
    nc.st.headless, nc.st.auto_approve = True, False

    async def scenario():
        try:
            denied, _ = await nc.run_tool("rea_call", {"tool": "install_adb_package", "arguments": {"apk": "a.apk"}})
            crashed, crash_err = await nc.run_tool("rea_call", {"tool": "crash_server"})
            again, again_err = await nc.run_tool("rea_call", {"tool": "inspect_artifact", "arguments": {"path": "x"}})
            return denied, crashed, crash_err, again, again_err
        finally:
            await nc.st.rea.close()

    denied, crashed, crash_err, again, again_err = run(scenario())
    assert denied == nc.DENIED
    assert crash_err and "stopped unexpectedly" in crashed
    assert not again_err and "inspect_artifact ok" in again


def test_rea_can_be_turned_off(sandbox, tmp_path):
    install_fake_rea(tmp_path)
    nc.st.config = {"rea": False}
    assert not nc.rea_enabled()
    assert "rea_call" not in {t["function"]["name"] for t in nc.api_tools()}
    assert "reverse-engineer-anything" not in nc.list_skills()


def test_print_mode_does_not_wait_on_idle_stdin(sandbox):
    import subprocess
    script = Path(__file__).resolve().parents[1] / "nanocode.py"
    env = {**os.environ, "OPENROUTER_API_KEY": ""}
    proc = subprocess.Popen([sys.executable, str(script), "-p", "hi", "--provider", "openrouter"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("nanocode -p hung waiting on an idle stdin")
    finally:
        proc.stdin.close()
    stderr = proc.stderr.read()
    proc.stdout.close()
    proc.stderr.close()
    assert proc.returncode == 1 and b"OpenRouter API key" in stderr


def test_piped_stdin_is_attached(sandbox, monkeypatch, capsys):
    import io as _io
    monkeypatch.setattr(sys, "stdin", _io.StringIO("ERROR: disk full"))
    assert nc.read_piped_stdin() == "ERROR: disk full"


def test_rea_close_stops_the_server(sandbox, tmp_path):
    install_fake_rea(tmp_path)

    async def scenario():
        await nc.run_tool("rea_search", {"query": "inspect"})
        proc = nc.st.rea.proc
        await nc.st.rea.close()
        return proc.returncode

    assert run(scenario()) is not None


# --- background shells, the done line, /recap ---

@pytest.mark.parametrize("backend", ["builtin", "tmux", "screen"])
def test_shell_start_read_send_stop(sandbox, backend):
    if backend != "builtin" and (os.name == "nt" or not shutil.which(backend)):
        pytest.skip(f"{backend} not available")
    nc.st.config = {"shell_backend": backend}
    script = Path("echoer.py")
    script.write_text("import sys\nprint('ready', flush=True)\nfor line in sys.stdin:\n    print('got ' + line.strip(), flush=True)\n")
    command = f'"{sys.executable}" -u echoer.py'

    async def scenario():
        try:
            started, err = await nc.run_tool("shell", {"action": "start", "name": "echo", "command": command})
            assert not err, started
            assert "running" in started and "ready" in started
            sent, err = await nc.run_tool("shell", {"action": "send", "name": "echo", "text": "hello"})
            assert not err, sent
            for _ in range(20):
                read, _ = await nc.run_tool("shell", {"action": "read", "name": "echo"})
                if "got hello" in read:
                    break
                await asyncio.sleep(0.5)
            assert "got hello" in read and "[echo: running]" in read
            listed, _ = await nc.run_tool("shell", {"action": "list"})
            assert "echo: running" in listed
            again, err = await nc.run_tool("shell", {"action": "start", "name": "echo", "command": command})
            assert err and "already running" in again
            stopped, _ = await nc.run_tool("shell", {"action": "stop", "name": "echo"})
            assert "stopped" in stopped and not nc.st.shells
        finally:
            await nc.stop_all_shells()

    run(scenario())
    assert "shells/" in (nc.project_dir() / ".gitignore").read_text()


def test_shell_errors_and_permissions(sandbox):
    nc.st.config = {"shell_backend": "builtin"}
    text, err = call("shell", action="read", name="nope")
    assert err and "no shell named" in text
    text, err = call("shell", action="start", name="x")
    assert err and "command" in text
    nc.st.headless, nc.st.auto_approve = True, False
    text, _ = call("shell", action="start", name="x", command="echo hi")
    assert text == nc.DENIED and not nc.st.shells


def test_done_line_and_recap(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "m", "k"
    Path("a.txt").write_text("one\n")

    def handler(req):
        body = json.loads(req.content)
        last = body["messages"][-1]
        if last["role"] == "user" and "recap" in last["content"]:
            assert "tools" not in body
            return sse([{"choices": [{"delta": {"content": "- You asked for an edit."}}]},
                        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9, "cost": 0.0001}}])
        if last["role"] == "user":
            return sse([tool_call_chunk("c1", "edit", {"path": "a.txt", "old": "one", "new": "two\nthree"})])
        return sse([{"choices": [{"delta": {"content": "Edited."}}]},
                    {"choices": [], "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "cost": 0.0123}}])

    mock_http(monkeypatch, handler)
    nc.new_session()
    run(nc.run_input("change one to two"))
    screen = plain(nc.st.output)
    assert any(f"▣ {verb} for" in screen for verb in nc.DONE_VERBS)
    assert "· m · 1.2k in · 40 out · $0.0123 · " in screen
    run(nc.run_input("/recap"))
    screen = plain(nc.st.output)
    assert "Recap" in screen and "a.txt  +2 -1" in screen and "You asked for an edit." in screen
    assert len(nc.st.messages) == 4


def test_cost_labels(sandbox):
    nc.st.backend = "ollama"
    assert nc.fmt_cost(0) == "free (local)"
    nc.st.backend = "sdk"
    assert nc.fmt_cost(0.5) == "$0.5000 (in your plan)"
    nc.st.backend = "api"
    assert nc.fmt_cost(2.5) == "$2.50"
    assert nc.fmt_duration(11) == "11s" and nc.fmt_duration(125) == "2m 5s" and nc.fmt_duration(3700) == "1h 1m"


def test_recap_survives_resume_and_titles_are_tidy(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "m", "k"
    Path("a.txt").write_text("one\n")
    replies = iter([sse([tool_call_chunk("c1", "edit", {"path": "a.txt", "old": "one", "new": "two"})]),
                    sse([{"choices": [{"delta": {"content": "Done."}}]},
                         {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.5}}])])
    mock_http(monkeypatch, lambda req: next(replies))
    nc.new_session()
    run(nc.run_input("please change the word one into the word two in the file called a.txt right now"))
    assert nc.st.session["title"] == "please change the word one into the word two in the file…"
    nc.st.__init__()
    nc.st.backend = "api"
    run(nc.resume_session("1"))
    assert nc.st.usage["cost"] == 0.5 and nc.st.changed == {"a.txt": [1, 1]}
    assert nc.st.activity and nc.st.activity[0][0] == "edit"


# --- keys and /terminal-setup ---

def test_editing_keys(sandbox):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    sent = []

    async def scenario():
        from prompt_toolkit.application import create_app_session

        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            app = nc.build_app()
            nc.st.input_box.accept_handler = lambda buff: sent.append(buff.text) or False
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.2)
            box = nc.st.input_box

            async def keys(text):
                inp.send_text(text)
                await asyncio.sleep(0.15)

            await keys("first line")
            await keys("\x1b[13;2u")          # Shift+Enter (kitty protocol / CSI u)
            await keys("second\x1b[27;2;13~")  # Shift+Enter (xterm modifyOtherKeys)
            await keys("third\n")              # Ctrl+J
            await keys("fourth\x1b\r")         # Alt+Enter
            lines = box.text
            await keys("fifth word")
            await keys("\x1b[1;3D")            # Alt+Left: to the start of "word"
            word_jump = box.document.text_after_cursor
            await keys("\x1b[1;5D")            # Ctrl+Left: to the start of "fifth"
            ctrl_jump = box.document.text_after_cursor
            await keys("\x05")                 # Ctrl+E: end of line
            await keys("\x15")                 # Ctrl+U: delete everything before the cursor
            cleared = box.text
            await keys("hi\r")                 # Enter sends
            app.exit()
            await task
            return lines, word_jump, ctrl_jump, cleared

    lines, word_jump, ctrl_jump, cleared = run(scenario())
    assert lines == "first line\nsecond\nthird\nfourth\n"
    assert word_jump == "word" and ctrl_jump == "fifth word"
    assert cleared == ""
    assert sent == ["hi"]


def test_terminal_setup_kitty(sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    nc.st.config = {"keyboard_protocol": "off"}
    monkeypatch.delenv("KITTY_CONFIG_DIRECTORY", raising=False)
    conf = tmp_path / "userhome" / ".config" / "kitty" / "kitty.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("font_size 14\n")
    assert nc.terminal_needs_setup()
    run(nc.terminal_setup())
    text = conf.read_text()
    assert text.startswith("font_size 14\n") and "map shift+enter send_text all \\x1b\\r" in text
    assert not nc.terminal_needs_setup()
    assert conf.with_name("kitty.conf.nanocode-backup").read_text() == "font_size 14\n"
    run(nc.terminal_setup())
    assert conf.read_text().count("shift+enter") == 1 and "already set up" in plain(nc.st.output)


def test_terminal_setup_vscode_and_manual(sandbox, tmp_path, monkeypatch):
    for var in ("KITTY_WINDOW_ID", "GHOSTTY_RESOURCES_DIR", "WT_SESSION", "ALACRITTY_WINDOW_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    keybindings = tmp_path / "keybindings.json"
    keybindings.write_text('[{"key": "ctrl+k", "command": "x"}]')
    monkeypatch.setattr(nc, "editor_keybindings_path", lambda terminal: keybindings)
    run(nc.terminal_setup())
    data = json.loads(keybindings.read_text())
    assert data[0]["key"] == "ctrl+k" and data[1]["key"] == "shift+enter" and data[1]["args"]["text"] == "\u001b\r"
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    run(nc.terminal_setup())
    assert "Option+Enter" in plain(nc.st.output)


def test_provider_command(sandbox, monkeypatch):
    nc.st.config = {"provider": "openrouter", "openrouter_api_key": "k",
                    "models": {"openrouter": "z-ai/glm-5.3", "ollama": "qwen3"}}
    assert nc.apply_config() is None
    nc.st.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    monkeypatch.setattr(nc, "ollama_models", lambda *a, **k: [{"name": "qwen3", "size_gb": 1, "params": "4B", "tools": True}])
    run(nc.run_input("/provider"))
    screen = plain(nc.st.output)
    assert "● openrouter" in screen and "ollama" in screen and "claude" in screen
    run(nc.run_input("/provider ollama"))
    assert nc.st.backend == "ollama" and nc.st.model == "qwen3" and len(nc.st.messages) == 2
    assert nc.load_config()["provider"] == "ollama" and "the chat continues" in plain(nc.st.output)
    del nc.st.config["openrouter_api_key"]
    run(nc.run_input("/provider openrouter"))
    assert nc.st.backend == "ollama" and "no API key" in plain(nc.st.output)
    run(nc.run_input("/provider nope"))
    assert "unknown provider" in plain(nc.st.output)
    if nc.sdk is None:
        run(nc.run_input("/provider claude"))
        assert nc.st.backend == "ollama"


# --- OpenCode-style TUI ---

def test_tui_home_palette_leader_and_interrupt(sandbox, monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    nc.st.config = {"provider": "openrouter", "models": {"openrouter": "m"}}
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "m", "k"

    async def slow(req):
        await asyncio.sleep(10)
        return sse([{"choices": [{"delta": {"content": "late"}}]}])

    mock_http(monkeypatch, slow)
    seen = {}

    async def scenario():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            nc.new_session()
            app = nc.build_app()
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.2)

            async def keys(text, wait=0.2):
                inp.send_text(text)
                await asyncio.sleep(wait)

            seen["home_at_start"] = nc.st.home
            await keys("\x10")                      # ctrl+p: command palette
            seen["palette"] = nc.st.dialog and nc.st.dialog["title"]
            await keys("switch model")
            await keys("\r")                        # opens the model list
            seen["models"] = nc.st.dialog and nc.st.dialog["title"]
            await keys("\x1b", 0.3)                 # esc closes it
            seen["closed"] = nc.st.dialog is None and app.layout.has_focus(nc.st.input_box)
            await keys("hello\r", 0.4)
            seen["home_after_send"] = nc.st.home
            seen["busy"] = not nc.st.task.done()
            await keys("\x1b", 0.3)                 # first esc only arms the interrupt
            seen["still_busy"] = not nc.st.task.done()
            await keys("\x1b", 0.5)                 # second esc interrupts
            seen["stopped"] = nc.st.task.done()
            app.exit()
            await task

    run(scenario())
    assert seen == {"home_at_start": True, "palette": "Commands", "models": "Switch model", "closed": True,
                    "home_after_send": False, "busy": True, "still_busy": True, "stopped": True}
    assert "interrupted" in plain(nc.st.output)


def test_user_block_wraps_inside_the_panel(sandbox):
    block = plain(nc.user_block("word " * 40))
    rows = block.strip("\n").split("\n")
    assert all(row.startswith("┃") for row in rows)
    assert len({len(row) for row in rows}) == 1 and len(rows) > 3


def test_at_file_completion(sandbox):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    Path("src").mkdir()
    Path("src/app.py").write_text("")
    Path("setup.py").write_text("")
    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        nc.build_app()
        completer = nc.st.input_box.buffer.completer
        found = [c.text for c in completer.get_completions(Document("look at @s"), None)]
        nested = [c.text for c in completer.get_completions(Document("@src/"), None)]
    assert found == ["@setup.py", "@src/"]
    assert nested == ["@src/app.py"]


# --- precise key reporting (kitty protocol / modifyOtherKeys) and ↑/↓ in long prompts ---

def test_translate_keys():
    t = nc.translate_keys
    assert t("\x1b[13;2u") == "\n"            # Shift+Enter (kitty protocol)
    assert t("\x1b[27;2;13~") == "\n"         # Shift+Enter (modifyOtherKeys)
    assert t("\x1b[13;3u") == "\x1b\r"        # Alt+Enter
    assert t("\x1b[127;9u") == "\x15"         # Cmd+Backspace → delete to start
    assert t("\x1b[127;3u") == "\x1b\x7f"     # Option+Backspace → delete word
    assert t("\x1b[1;9D") == "\x01" and t("\x1b[1;9C") == "\x05"  # Cmd+←/→
    assert t("\x1b[1;3D") == "\x1b[1;3D"      # Option+← untouched (already understood)
    assert t("\x1b[99;5u") == "\x03"          # Ctrl+C
    assert t("\x1b[97;7u") == "\x1b\x01"      # Ctrl+Alt+A
    assert t("\x1b[98;3u") == "\x1bb"         # Alt+B
    assert t("\x1b[97:65;4u") == "\x1bA"      # Shift+Alt+A, with the shifted key reported
    assert t("\x1b[27u") == "\x1b"            # Esc
    assert t("\x1b[57441;2u") == ""           # a lone modifier key: ignored
    assert t("\x1b[115;9u") == ""             # Cmd+S: left to the terminal
    assert t("plain text ü") == "plain text ü"


def test_kitty_keys_and_vertical_moves_in_the_input(sandbox):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    nc.install_key_translation()
    seen = {}

    async def scenario():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            app = nc.build_app()
            box = nc.st.input_box
            box.accept_handler = lambda buff: False
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.2)

            async def keys(text, wait=0.15):
                inp.send_text(text)
                await asyncio.sleep(wait)

            await keys("older prompt\r")
            await keys("one\x1b[13;2utwo\x1b[27;2;13~three")
            seen["newlines"] = box.text
            await keys("\x1b[1;9D")
            seen["cmd_left"] = box.document.text_after_cursor
            await keys("\x1b[1;9C\x1b[127;9u")
            seen["cmd_backspace"] = box.text
            await keys("some text\x1b[99;5u")
            seen["ctrl_c_clears"] = box.text
            long = "x" * 230
            await keys(long)
            width = box.window.render_info.window_width
            await keys("\x1b[A")                  # ↑ inside a wrapped line
            seen["up_moved"] = (box.text == long, len(box.text) - box.buffer.cursor_position == width)
            for _ in range(5):
                await keys("\x1b[A", 0.1)
            seen["history"] = box.text
            app.exit()
            await task

    run(scenario())
    assert seen["newlines"] == "one\ntwo\nthree"
    assert seen["cmd_left"] == "three"
    assert seen["cmd_backspace"] == ""
    assert seen["ctrl_c_clears"] == ""
    assert seen["up_moved"] == (True, True)
    assert seen["history"] == "older prompt"


def test_mouse_scroll_select_and_copy(sandbox, monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    copied = []
    monkeypatch.setattr(nc, "copy_to_clipboard", lambda text: copied.append(text) or "native")
    seen = {}

    async def scenario():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            app = nc.build_app()
            nc.st.home = False
            nc.reset_view("\n".join(f"line {i}" for i in range(300)))
            box = nc.st.input_box
            box.accept_handler = lambda buff: False
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.2)

            async def keys(text, wait=0.15):
                inp.send_text(text)
                await asyncio.sleep(wait)

            await keys("old prompt\r")
            await keys("draft")
            screen = app.renderer._last_screen.visible_windows_to_write_positions
            chat, prompt = screen[nc.st.output_window], screen[box.window]
            height = nc.st.view["height"]
            seen["full"] = [row[0] for row in nc.st.view["rows"]] == list(range(300 - height, 300))

            def at(x, y):  # 1-based terminal cell inside the chat
                return f"{chat.xpos + x + 1};{chat.ypos + y + 1}"

            await keys(f"\x1b[<64;{at(2, 3)}M" * 2)                       # wheel up over the chat
            seen["over_chat"] = nc.st.scroll_top
            await keys(f"\x1b[<64;{prompt.xpos + 3};{prompt.ypos + 1}M")   # wheel up over the prompt
            seen["over_prompt"] = (nc.st.scroll_top, box.text)
            seen["full_after_scroll"] = len(nc.st.view["rows"]) == height
            for _ in range(500):                                          # far past the top: still a full screen
                inp.send_text(f"\x1b[<64;{at(2, 3)}M")
            await asyncio.sleep(0.3)
            seen["top"] = (nc.st.scroll_top, len(nc.st.view["rows"]) == height)
            await keys(f"\x1b[<0;{at(0, 0)}M\x1b[<32;{at(2, 1)}M\x1b[<32;{at(3, 1)}M\x1b[<0;{at(3, 1)}m")
            seen["drag"] = list(copied)
            await keys(f"\x1b[<0;{at(2, 2)}M\x1b[<0;{at(2, 2)}m\x1b[<0;{at(2, 2)}M\x1b[<0;{at(2, 2)}m")
            seen["double"] = copied[-1]
            await keys(f"\x1b[<0;{at(5, 4)}M\x1b[<0;{at(5, 4)}m")       # a plain click clears the selection
            seen["cleared"] = nc.st.selection
            await keys(f"\x1b[<65;{at(2, 3)}M" * 200, 0.3)              # wheel down, back to the newest output
            seen["back"] = nc.st.scroll_top
            app.exit()
            await task

    run(scenario())
    assert seen["full"]
    assert seen["over_chat"] is not None
    assert seen["over_prompt"][1] == "draft" and seen["over_prompt"][0][0] == seen["over_chat"][0] - 3
    assert seen["full_after_scroll"]
    assert seen["top"] == ((0, 0), True)
    assert seen["drag"] == ["line 0\nline"]
    assert seen["double"] == "line"
    assert seen["cleared"] is None
    assert seen["back"] is None


def test_transcript_rows_wrap_and_select():
    nc.reset_view("short\n" + "abcdefghij" * 3 + "\n\x1b[1mbold\x1b[0m end")
    rows = nc.transcript_rows(10, 10)
    assert ["".join(t for _, t in row) for row in rows] == ["short", "abcdefghij", "abcdefghij", "abcdefghij",
                                                          "bold end"]
    nc.st.selection = {"anchor": (1, 28), "head": (2, 1), "dragging": False}
    assert nc.selected_text() == "ij\nbo"
    rows = nc.transcript_rows(10, 10)
    assert any(nc.SELECTED_STYLE in style for style, _ in rows[3])
    nc.reset_view()


@pytest.mark.skipif(os.name == "nt", reason="Windows reads keys through the console API, not this protocol")
def test_no_setup_hint_when_keys_are_reported(sandbox, monkeypatch):
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    for var in ("TMUX", "STY"):
        monkeypatch.delenv(var, raising=False)
    assert nc.keyboard_protocol_enabled() and not nc.terminal_needs_setup()


def test_sticky_prompt_while_reading_the_answer():
    nc.reset_view(nc.user_block("why is the sky blue") + "\n" + "\n".join(f"answer {i}" for i in range(100)))
    rows = nc.transcript_rows(80, 20)
    text = ["".join(t for _, t in row) for row in rows]
    assert "why is the sky blue" in text[0] and text[1].startswith("▀")
    count, line = nc.st.view["sticky"]
    assert count == 2 and line == 1
    nc.transcript_mouse(nc_mouse("MOUSE_UP"), None, 5, 0)   # clicking it jumps to the message
    rows = nc.transcript_rows(80, 20)
    assert nc.st.scroll_top == (1, 0) and not nc.st.view["sticky"]
    nc.reset_view()


def nc_mouse(name):
    from prompt_toolkit.mouse_events import MouseEventType
    return getattr(MouseEventType, name)


def test_wheel_acceleration(monkeypatch):
    nc.reset_view("\n".join(f"line {i}" for i in range(500)))
    nc.transcript_rows(80, 20)
    clock = [100.0]
    monkeypatch.setattr(nc.time, "monotonic", lambda: clock[0])
    moved = []
    monkeypatch.setattr(nc, "scroll_transcript", lambda rows: moved.append(rows) or True)
    nc.wheel_scroll(1)                       # a lone notch: 3 rows
    clock[0] += 1
    nc.wheel_scroll(1)
    for _ in range(30):                      # a trackpad stream: 1 row each, then faster
        clock[0] += 0.01
        nc.wheel_scroll(1)
    assert moved[:2] == [3, 3] and moved[2] == 1 and moved[-1] == 4
    nc.reset_view()


def test_think_levels_reach_every_provider(sandbox, monkeypatch):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "z-ai/glm", "k"
    sent = []

    def openrouter(req):
        body = json.loads(req.content)
        sent.append(body.get("reasoning"))
        if body.get("reasoning") == {"enabled": False}:
            return httpx.Response(400, json={"error": {"message": "Reasoning is mandatory for this endpoint"}})
        return sse([{"choices": [{"delta": {"reasoning": "hmm"}}]}, {"choices": [{"delta": {"content": "ok"}}]}])

    current = [openrouter]
    mock_http(monkeypatch, lambda req: current[0](req))
    assert nc.think_level() == "medium"                       # on by default
    run(nc.send("hi"))
    assert sent == [{"effort": "medium"}] and nc.st.thinking_now is False
    run(nc.run_input("/think off"))
    assert nc.load_config().get("think") == "off"
    run(nc.send("hi"))                                         # rejected, so it's sent without the setting
    assert sent[1:] == [{"enabled": False}, None] and ("api", "z-ai/glm") in nc.st.no_think
    run(nc.run_input("/think high"))
    run(nc.send("hi"))
    assert sent[-1] == {"effort": "high"}
    run(nc.run_input("/think nope"))
    assert "takes off, low, medium, high" in plain(nc.st.output)

    nc.st.backend, nc.st.model = "ollama", "llama3"
    seen = []

    def ollama(req):
        body = json.loads(req.content)
        seen.append(body.get("think", "unset"))
        if body.get("think") is not None and body.get("think") != "unset":
            return httpx.Response(400, json={"error": '"llama3" does not support thinking'})
        return httpx.Response(200, text=json.dumps({"message": {"content": "ok"}, "done": True}) + "\n")

    current[0] = ollama
    run(nc.send("hi"))
    assert seen == [True, "unset"]
    nc.st.model = "gpt-oss:20b"
    monkeypatch.setattr(nc, "rejects_thinking", lambda status, error: False)
    current[0] = lambda req: seen.append(json.loads(req.content).get("think")) or httpx.Response(
        200, text=json.dumps({"message": {"content": "ok"}, "done": True}) + "\n")
    run(nc.send("hi"))
    assert seen[-1] == "high"


@pytest.mark.skipif(nc.sdk is None, reason="claude-agent-sdk isn't installed")
def test_think_level_reaches_claude(sandbox, monkeypatch):
    made = []

    class FakeClient:
        def __init__(self, options):
            made.append(options)

        async def connect(self):
            pass

    monkeypatch.setattr(nc.sdk, "ClaudeSDKClient", FakeClient)
    run(nc.make_client())
    nc.st.config["think"] = "off"
    run(nc.make_client())
    assert made[0].thinking == {"type": "adaptive"} and made[0].effort == "medium"
    assert made[1].thinking == {"type": "disabled"} and made[1].effort is None


def test_small_model_mistakes_are_handled(sandbox, tmp_path, monkeypatch):
    install_fake_rea(tmp_path)

    async def scenario():
        try:
            wrapped, err = await nc.run_tool("rea_call", {"tool": "rea_search", "arguments": {"query": "apk"}})
            own, own_err = await nc.run_tool("rea_call", {"tool": "glob", "arguments": '{"pattern": "*.nothing"}'})
            missing, missing_err = await nc.run_tool("rea_call", {"tool": "rea_schema"})
            return wrapped, err, own, own_err, missing, missing_err
        finally:
            await nc.st.rea.close()

    wrapped, err, own, own_err, missing, missing_err = run(scenario())
    assert not err and "install_adb_package" in wrapped                       # ran rea_search instead of failing
    assert not own_err and missing_err and "rea_schema needs: tool" in missing

    names = lambda: {t["name"] for t in nc.active_tools()}           # noqa: E731
    nc.st.backend, nc.st.model = "ollama", "qwen2.5:0.5b"
    assert "rea_call" not in names() and "read" in names() and "Reverse engineering" not in nc.build_system_prompt()
    nc.st.model = "qwen3:8b"
    assert "rea_call" in names()

    nc.st.backend, nc.st.model = "ollama", "qwen2.5:0.5b"
    mock_http(monkeypatch, lambda req: httpx.Response(200, text="\n".join(json.dumps(x) for x in [
        {"message": {"content": "The ball is 5 cents."}, "done": False},
        {"message": {"content": "\n</tool_response>"}, "done": True}]) + "\n"))
    run(nc.send("bat and ball"))
    assert "</tool_response>" not in nc.st.output and nc.st.messages[-1]["content"] == "The ball is 5 cents."


def test_version_file_matches_the_code():
    assert (Path(nc.__file__).parent / "VERSION").read_text().strip() == nc.__version__


def test_update_check(sandbox, monkeypatch, capsys):
    calls = []

    def github(req):
        calls.append(str(req.url))
        return httpx.Response(200, text="99.0.0\n")

    real = httpx.Client
    monkeypatch.setattr(nc.httpx, "get", lambda url, **kw: real(transport=httpx.MockTransport(github)).get(url))
    assert nc.parse_version("0.10.0") > nc.parse_version("0.9.9") and not nc.is_newer(nc.__version__)
    assert run(nc.check_for_update()) == "99.0.0" and nc.st.update_available == "99.0.0"
    run(nc.check_for_update())                                # cached for a day: no second request
    assert len(calls) == 1 and calls[0].endswith("/master/VERSION")
    assert nc.main(["version"]) == 0 and "99.0.0 is available: run `nanocode update`" in capsys.readouterr().out
    run(nc.run_input("/version"))
    assert "99.0.0 is available" in plain(nc.st.output)
    monkeypatch.setenv("NANOCODE_NO_UPDATE_CHECK", "1")
    nc.st.update_available = None
    assert run(nc.check_for_update()) is None and nc.st.update_available is None

    monkeypatch.setattr(nc.httpx, "get", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    assert nc.main(["version"]) == 0 and "couldn't check for updates" in capsys.readouterr().out


def test_update_in_the_app(sandbox, monkeypatch):
    (nc.home_dir()).mkdir(parents=True, exist_ok=True)
    (nc.home_dir() / "update.json").write_text(json.dumps({"checked": time.time() + 999, "latest": "99.0.0"}))
    monkeypatch.setattr(nc, "fetch_latest_version", lambda timeout=4: "99.0.0")
    opened = []
    nc.st.ui["open_dialog"] = lambda title, items: opened.append((title, [label for label, _, _ in items], items))

    run(nc.check_for_update())
    assert nc.st.update_available == "99.0.0"
    run(nc.run_input("/update"))
    title, labels, items = opened[-1]
    assert title == "Update available · nanocode 99.0.0" and labels[-1] == "Skip"
    assert labels[0] == "Update"

    monkeypatch.setattr(nc, "run_installer_quietly", lambda: (0, "installed"))
    run(nc.install_update())
    assert nc.st.update_state == "installed" and "Update successful!" in plain(nc.st.output)
    run(nc.run_input("/update"))
    assert opened[-1][0] == "Update successful!" and opened[-1][1][0] == "Restart"

    started = []
    monkeypatch.setattr(nc.os, "execv", lambda path, argv: started.append(path))
    (nc.home_dir() / "bin").mkdir()
    (nc.home_dir() / "bin" / "nanocode").write_text("")
    nc.restart_app()
    if os.name != "nt":
        assert nc.after_exit() == 0 and started and started[0].endswith("nanocode")

    nc.st.update_state = None
    monkeypatch.setattr(nc, "run_installer_quietly", lambda: (1, "boom\nno network"))
    run(nc.install_update())
    assert nc.st.update_state == "failed" and "no network" in plain(nc.st.output)

    nc.skip_update("99.0.0")                       # Skip: no more reminders about this version
    run(nc.check_for_update())
    assert nc.st.update_available is None


def test_update_notice_renders_on_both_screens(sandbox):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def scenario():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            app = nc.build_app()
            nc.st.update_available = "99.0.0"
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.2)
            try:
                app.renderer.render(app, app.layout)  # draws now: an error fails the test instead of hanging
                nc.st.home = False                   # the chat view's footer shows it too
                app.renderer.render(app, app.layout)
                nc.st.update_state = "installed"
                app.renderer.render(app, app.layout)
            finally:
                app.exit()
                await asyncio.wait_for(task, 5)

    run(scenario())
