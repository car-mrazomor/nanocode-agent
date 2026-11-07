import asyncio
import json
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
    assert set(tools) == set(nc.TOOLS)
    Path("a.txt").write_text("hi\n")
    result = run(tools["read"].handler({"path": "a.txt"}))
    assert result["is_error"] is False and "hi" in result["content"][0]["text"]
