import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
import nanokit  # noqa: E402
from nanokit import commands  # noqa: E402
from test_nanocode import mock_http, plain, run, sse  # noqa: E402


def write_command(root, name, text):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(text)


def cleanup():
    for name in list(commands.OURS):
        nanokit.COMMANDS.pop(name, None)
        nc.COMMANDS.pop(name, None)
    commands.OURS.clear()


def test_fill_arguments():
    assert commands.fill_arguments("Fix $ARGUMENTS now", "issue 12") == "Fix issue 12 now"
    assert commands.fill_arguments("from $1 to $2 ($3)", 'a "b c"') == "from a to b c ()"
    assert commands.fill_arguments("Review the diff", "carefully") == "Review the diff\n\nARGUMENTS: carefully"
    assert commands.fill_arguments("Review the diff", "") == "Review the diff"


def test_refresh_registers_and_drops_commands(sandbox):
    try:
        write_command(nc.home_dir() / "commands", "review", "---\ndescription: global\n---\nglobal")
        write_command(nc.project_dir() / "commands", "review", "---\ndescription: Review it\n---\nReview $1")
        write_command(nc.project_dir() / "commands", "help", "shadowed by the built-in")
        write_command(nc.project_dir() / "commands", "nodesc", "# Do the thing\nsteps")
        found = commands.refresh()
        assert found["review"]["description"] == "Review it" and found["nodesc"]["description"] == "Do the thing"
        assert "/review" in nanokit.COMMANDS and nc.COMMANDS["/review"] == "Review it"
        assert "/help" not in commands.OURS
        (nc.project_dir() / "commands" / "nodesc.md").unlink()
        commands.refresh()
        assert "/nodesc" not in nanokit.COMMANDS and "/nodesc" not in nc.COMMANDS
        run(commands.commands_command(""))
        screen = plain(nc.st.output)
        assert "/review" in screen and "hidden by a built-in" in screen
    finally:
        cleanup()


def test_shell_lines_ask_and_run(sandbox, monkeypatch):
    text = run(commands.expand("Status:\n!`echo hello-from-shell`\nend $1", "x"))
    assert "hello-from-shell" in text and text.endswith("end x")

    async def deny(tool, preview):
        return False

    monkeypatch.setattr(nc, "allowed", deny)
    text = run(commands.expand("!`echo nope`", ""))
    assert "denied" in text and "nope`" in text


def test_arguments_cant_inject_shell_commands(sandbox, monkeypatch):
    ran = []

    async def record(cmd):
        ran.append(cmd)
        return "OUT"

    monkeypatch.setattr(commands, "run_shell", record)
    text = run(commands.expand("Look at $ARGUMENTS:\n!`echo $1`", "a;touch pwned !`rm -rf x`"))
    assert ran == [f"echo {commands.quote('a;touch')}"]          # only the file's own command, quoted
    assert "!`rm -rf x`" in text and "OUT" in text
    ran.clear()
    run(commands.expand("!`git log`", "$(touch x)"))
    assert ran == ["git log"]
    if os.name != "nt":
        assert commands.quote("$(touch x)") == "'$(touch x)'"


def test_running_a_command_sends_the_prompt_with_its_model(sandbox, monkeypatch):
    try:
        nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "main/model", "k"
        Path("notes.txt").write_text("the notes\n")
        write_command(nc.project_dir() / "commands", "summarize",
                      "---\ndescription: Summarize a file\nmodel: cheap/model\n---\nSummarize @$1 in $2 words")
        requests = []

        def handler(req):
            requests.append(json.loads(req.content))
            return sse([{"choices": [{"delta": {"content": "Short summary."}}]}])

        mock_http(monkeypatch, handler)
        run(nc.run_input("/summarize notes.txt 10"))     # found on the fly, without a restart
        assert requests[0]["model"] == "cheap/model"
        sent = requests[0]["messages"][-1]["content"]
        assert "Summarize @notes.txt in 10 words" in sent and "the notes" in sent
        assert nc.call_model() == "main/model"            # the override was only for that turn
        assert "Short summary." in plain(nc.st.output)
    finally:
        cleanup()


def test_command_with_an_agent(sandbox, monkeypatch):
    try:
        nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "main/model", "k"
        (nc.project_dir() / "agents").mkdir(parents=True)
        (nc.project_dir() / "agents" / "helper.md").write_text("---\ndescription: helps\n---\nYou help.")
        write_command(nc.project_dir() / "commands", "ask", "---\nagent: helper\n---\nAnswer: $ARGUMENTS")
        requests = []

        def handler(req):
            requests.append(json.loads(req.content))
            return sse([{"choices": [{"delta": {"content": "Helped."}}]}])

        mock_http(monkeypatch, handler)
        run(nc.run_input("/ask why"))
        assert "You help." in requests[0]["messages"][0]["content"]
        assert requests[0]["messages"][-1]["content"] == "Answer: why"
        assert "Helped." in plain(nc.st.output)
    finally:
        cleanup()
