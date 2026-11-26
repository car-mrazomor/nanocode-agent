import asyncio
import os
import shlex

import pytest

import nanocode as nc
from nanokit import environments as envs
from test_nanocode import call, plain, run


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(envs, "_override", None)
    monkeypatch.setattr(envs, "_started", {})
    calls = []

    async def fake_run_cmd(*cmd, timeout=15):
        calls.append(list(cmd))
        if cmd[1] == "inspect":
            return 1, "Error: No such object"
        return 0, "ok\n"
    monkeypatch.setattr(nc, "run_cmd", fake_run_cmd)
    return calls


def wrapped(command="echo hi"):
    return run(nc.wrapped_command(command))


def test_local_is_default(sandbox):
    assert wrapped() == "echo hi"
    assert envs.prompt_section() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting")
def test_docker_starts_container_once_and_execs(sandbox, fresh):
    nc.st.config["environment"] = {"type": "docker", "image": "python:3.12"}
    cmd = wrapped("ls 'a b' && pwd")
    argv = shlex.split(cmd)
    assert argv[:2] == ["docker", "exec"] and argv[-3:] == ["sh", "-c", "ls 'a b' && pwd"]
    assert argv[argv.index("-w") + 1] == str(sandbox.resolve())
    started = [c for c in fresh if c[1] == "run"]
    assert len(started) == 1 and "python:3.12" in started[0] and f"{sandbox.resolve()}:{sandbox.resolve()}" in started[0]
    wrapped()
    assert len([c for c in fresh if c[1] == "run"]) == 1
    assert "mounted" in envs.prompt_section()
    run(envs.stop_containers())
    assert fresh[-1][:3] == ["docker", "rm", "-f"]


def test_named_container_is_stopped_not_removed(sandbox, fresh):
    nc.st.config["environment"] = {"type": "docker", "engine": "podman", "image": "img", "container": "mybox"}
    assert wrapped().startswith("podman exec")
    run(envs.stop_containers())
    assert fresh[-1][:2] == ["podman", "stop"] and fresh[-1][-1] == "mybox"


def test_docker_needs_image_fails_instead_of_running_locally(sandbox):
    nc.st.config["environment"] = {"type": "docker"}
    text, is_error = call("bash", command="echo local")
    assert "needs an \"image\"" in text and "[exit code 1]" in text and "\nlocal" not in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting")
def test_ssh_and_custom_and_apptainer(sandbox):
    nc.st.config["environment"] = {"type": "ssh", "host": "me@box", "dir": "/srv/my app", "options": ["-p", "22"]}
    argv = shlex.split(wrapped("echo 'x'"))
    assert argv[0] == "ssh" and "me@box" in argv and argv[1:3] == ["-p", "22"]
    assert argv[-1] == "cd '/srv/my app' && echo 'x'"
    assert "LOCAL" in envs.prompt_section()
    nc.st.config["environment"] = {"type": "custom", "wrap": "daytona exec ws -- sh -c {command}"}
    assert shlex.split(wrapped("a && b")) == ["daytona", "exec", "ws", "--", "sh", "-c", "a && b"]
    nc.st.config["environment"] = {"type": "apptainer", "image": "e.sif"}
    assert shlex.split(wrapped("make"))[:2] == ["apptainer", "exec"]


def test_env_command_switches_for_the_session(sandbox):
    run(envs.cmd_env("docker python:3.12"))
    assert envs.config() == {"type": "docker", "engine": "docker", "image": "python:3.12"}
    assert "environment" not in nc.st.config
    run(envs.cmd_env("ssh me@box /srv"))
    assert envs.config()["dir"] == "/srv"
    run(envs.cmd_env("custom no placeholder"))
    run(envs.cmd_env(""))
    text = plain(nc.st.output)
    assert "usage: /env custom" in text and "environment: ssh me@box:/srv" in text
    run(envs.cmd_env("local"))
    assert wrapped() == "echo hi"


@pytest.mark.skipif(os.name == "nt", reason="runs sh")
def test_bash_tool_runs_wrapped_command(sandbox, monkeypatch):
    envs._override = {"type": "custom", "wrap": "sh -c {command}; echo wrapped"}
    assert "inside\nwrapped" in call("bash", command="echo inside")[0]


def cmd_exe(line, env=None):
    """What cmd.exe /c does to a line: %VAR% expansion, then ^ escapes and quote mode. Fails on an unescaped
    operator (the line would run another command). Returns the line the program gets."""
    env = {"PATH": "C:\\bin", "X": "expanded"} if env is None else env
    expanded, i = "", 0
    while i < len(line):
        end = line.find("%", i + 1) if line[i] == "%" else -1
        if end > 0 and line[i + 1:end] in env:
            expanded += env[line[i + 1:end]]
            i = end + 1
        else:
            expanded += line[i]
            i += 1
    out, quoted, i = "", False, 0
    while i < len(expanded):
        c = expanded[i]
        if c == '"':
            quoted = not quoted
        elif c == "^" and not quoted:
            i += 1
            c = expanded[i]
        elif c in "&|<>()" and not quoted:
            raise AssertionError(f"cmd.exe operator {c!r} in {line!r}")
        out += c
        i += 1
    return out


def crt_argv(line):
    """The C runtime's command-line parsing (what the program's argv is)."""
    args, cur, has, quoted, i = [], "", False, False, 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            n = 0
            while i < len(line) and line[i] == "\\":
                n, i = n + 1, i + 1
            if i < len(line) and line[i] == '"':
                cur += "\\" * (n // 2)
                if n % 2:
                    cur += '"'
                    i += 1
            else:
                cur += "\\" * n
            has = True
            continue
        if c == '"':
            quoted, has = not quoted, True
        elif c in " \t" and not quoted:
            if has:
                args.append(cur)
            cur, has = "", False
        else:
            cur, has = cur + c, True
        i += 1
    if has:
        args.append(cur)
    return args


def test_windows_cmd_quoting_can_t_break_out():
    tricky = ["docker", "exec", "-w", "C:\\a b\\", "box", "sh", "-c",
              'echo "hi" & calc | x > y < z (a) %PATH% %X% 100% ^caret !bang! back\\slash\\ "\\"end\\\\"']
    line = envs.cmd_join(tricky)
    assert crt_argv(cmd_exe(line)) == tricky
    for arg in ['"', '\\"', 'a" & calc & "', '%X%', '^', '"&"', '\\\\"&calc&\\\\"']:
        assert crt_argv(cmd_exe(envs.cmd_join(["prog", arg]))) == ["prog", arg], arg
    with pytest.raises(nc.ToolError):
        envs.cmd_join(["sh", "-c", "a\nb"])


def test_docker_on_windows_multiline_script_survives_cmd(sandbox, monkeypatch):
    import base64
    monkeypatch.setattr(envs, "on_windows", lambda: True)
    nc.st.config["environment"] = {"type": "docker", "image": "python:3.12"}
    script = "cat <<'EOF' > a.txt\nx & y | z\nEOF\necho %PATH% \"done\""
    argv = crt_argv(cmd_exe(wrapped(script)))
    assert argv[:2] == ["docker", "exec"] and argv[-3:-1] == ["sh", "-c"]
    encoded = argv[-1].split("echo ", 1)[1].split(" |", 1)[0]
    assert base64.b64decode(encoded).decode() == script
    assert argv[argv.index("-w") + 1] == "/workspace"
    nc.st.config["environment"] = {"type": "ssh", "host": "me@box"}
    assert crt_argv(cmd_exe(wrapped("a && b"))) == ["ssh", "-o", "BatchMode=yes", "me@box", "a && b"]


def test_parallel_commands_start_one_container(sandbox, monkeypatch, fresh):
    nc.st.config["environment"] = {"type": "docker", "image": "python:3.12"}
    real = nc.run_cmd

    async def slow(*cmd, timeout=15):
        await asyncio.sleep(0.05)
        return await real(*cmd, timeout=timeout)
    monkeypatch.setattr(nc, "run_cmd", slow)

    async def both():
        return await asyncio.gather(nc.wrapped_command("a"), nc.wrapped_command("b"), nc.wrapped_command("c"))
    run(both())
    assert len([c for c in fresh if c[1] == "run"]) == 1
    assert len([c for c in fresh if c[1] == "inspect"]) == 1
