import json
import sys

import httpx
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import share  # noqa: E402
from test_nanocode import mock_http, plain, run, sse  # noqa: E402


def make_chat(monkeypatch, reply="Use sk-or-v1-abcdefghijklmnopqrstuvwxyz0123 carefully."):
    nc.st.backend, nc.st.model, nc.st.openrouter_key = "api", "m", "k"
    mock_http(monkeypatch, lambda req: sse([{"choices": [{"delta": {"content": reply}}]}]))
    nc.new_session()
    run(nc.run_input("how do I call the API?"))


def test_redact_text():
    nc.st.config = {"openrouter_api_key": "my-own-config-key-123", "email": {"password": "hunter2hunter2"}}
    text = ("key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 and ghp_" + "a" * 36 + " and AKIAABCDEFGHIJKLMNOP\n"
            "Authorization: Bearer abcdefghijklmnop1234\nAPI_KEY=supersecretvalue1\n"
            "my-own-config-key-123 hunter2hunter2 plain words stay")
    clean, count = share.redact_text(text)
    for secret in ("sk-ant", "ghp_", "AKIA", "abcdefghijklmnop1234", "supersecretvalue1", "my-own-config", "hunter2"):
        assert secret not in clean
    assert "Bearer [REDACTED]" in clean and "API_KEY=[REDACTED]" in clean and "plain words stay" in clean
    assert count >= 7


def test_redact_text_covers_every_config_secret_and_env(monkeypatch):
    nc.st.config = {"api_keys": {"openai": "openai-key-value-1"},
                    "providers": {"custom": {"api_key": "custom-key-value-2", "base_url": "https://example.com/v1"}},
                    "gateway": {"telegram": {"bot_token": "tg-bot-token-3"}},
                    "mcp_servers": {"gh": {"headers": {"Authorization": "hdr-auth-value-8"},
                                           "env": {"GITHUB_PERSONAL": "mcp-env-value-4"}}}}
    monkeypatch.setenv("SOMETHING_SECRET", "env-secret-value-5")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "env-token-value-6")
    text = ("openai-key-value-1 custom-key-value-2 tg-bot-token-3 hdr-auth-value-8 mcp-env-value-4 "
            "env-secret-value-5 env-token-value-6 https://example.com/v1 extra-value-7")
    clean, _ = share.redact_text(text, extra=["extra-value-7"])
    assert clean.split() == ["[REDACTED]"] * 2 + ["[REDACTED]"] * 5 + ["https://example.com/v1", "[REDACTED]"]


def test_share_redacts_the_gist_title(sandbox, monkeypatch):
    make_chat(monkeypatch)
    nc.st.session["title"] = "use sk-or-v1-abcdefghijklmnopqrstuvwxyz0123 here"
    calls = []

    async def fake_gh(*args, timeout=60):
        calls.append(args)
        return (0, "https://gist.github.com/me/0123456789abcdef0123\n") if args[:2] == ("gist", "create") else (0, "")

    async def yes(tool, preview, dangerous):
        assert "sk-or-v1" not in preview
        return True
    monkeypatch.setattr(share, "gh", fake_gh)
    monkeypatch.setattr(share.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(nc, "ask_permission", yes)
    monkeypatch.setattr(nc, "copy_to_clipboard", lambda text: "native")
    run(nc.run_input("/share"))
    create = next(c for c in calls if c[:2] == ("gist", "create"))
    assert "sk-or-v1" not in " ".join(create) and "[REDACTED]" in create[3]


def test_export_markdown_and_json(sandbox, monkeypatch):
    make_chat(monkeypatch)
    run(nc.run_input("/export"))
    md = next(sandbox.glob("*.md")).read_text()
    assert md.startswith("# how do I call the API?") and "## You" in md and "## nanocode" in md
    run(nc.run_input("/export json out/chat.json"))
    data = json.loads((sandbox / "out" / "chat.json").read_text())
    assert data["id"] == nc.st.session["id"] and data["messages"][0]["content"] == "how do I call the API?"
    run(nc.run_input("/export notes.md"))
    assert (sandbox / "notes.md").read_text().startswith("# ")


def test_markdown_shows_tool_calls_and_subscription_screens():
    md = share.to_markdown({"title": "t", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "content": "a.txt"}, {"role": "assistant", "content": "done"}]})
    assert "**Tool `bash`**" in md and '"command": "ls"' in md and "<details>" in md and "a.txt" in md
    md = share.to_markdown({"title": "t", "messages": [], "screen": nc.user_block("hi") + "\nHello there"})
    assert "> hi" in md and "Hello there" in md


def test_import_file_and_cli(sandbox, monkeypatch, capsys):
    make_chat(monkeypatch)
    run(nc.run_input("/export json chat.json"))
    original = nc.st.session["id"]
    run(nc.run_input("/import chat.json"))
    assert "Imported" in plain(nc.st.output)
    files = sorted(p.stem for p in nc.sessions_dir().glob("*.json"))
    assert len(files) == 2 and original in files              # the same id: a new one is made
    assert nc.main(["import", "chat.json"]) == 0
    assert "imported" in capsys.readouterr().out
    assert len(list(nc.sessions_dir().glob("*.json"))) == 3
    (sandbox / "bad.json").write_text("[1]")
    assert nc.main(["import", "bad.json"]) == 1
    assert len(nc.list_sessions()) == 3


def test_import_url(sandbox, monkeypatch):
    chat = {"id": "x1", "title": "From the web", "backend": "api", "messages": [{"role": "user", "content": "hi"}]}
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return httpx.Response(200, text=json.dumps(chat), request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)
    path, data = share.import_chat(share.load_export("https://gist.github.com/me/0123456789abcdef0123"))
    assert seen == ["https://gist.githubusercontent.com/raw/0123456789abcdef0123"]
    assert data["title"] == "From the web" and path.exists()


def test_share_and_unshare(sandbox, monkeypatch):
    make_chat(monkeypatch)
    calls, uploaded = [], []

    async def fake_gh(*args, timeout=60):
        calls.append(args)
        if args[:2] == ("gist", "create"):
            uploaded.append(Path(args[-1]).read_text())
            return 0, "- Creating gist\nhttps://gist.github.com/me/0123456789abcdef0123\n"
        return 0, ""

    async def yes(tool, preview, dangerous):
        return True
    monkeypatch.setattr(share, "gh", fake_gh)
    monkeypatch.setattr(share.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(nc, "ask_permission", yes)
    monkeypatch.setattr(nc, "copy_to_clipboard", lambda text: "native")
    run(nc.run_input("/share"))
    assert "sk-or-v1" not in uploaded[0] and "[REDACTED]" in uploaded[0]
    text = plain(nc.st.output)
    assert "secret removed" in text and "Shared" in text and "gist.github.com/me/0123456789abcdef0123" in text
    assert not Path(calls[-1][-1]).exists()                  # the temp copy is gone
    run(nc.run_input("/unshare https://gist.github.com/me/0123456789abcdef0123"))
    assert calls[-1] == ("gist", "delete", "0123456789abcdef0123", "--yes") and "Unshared" in plain(nc.st.output)


def test_share_needs_permission_and_gh(sandbox, monkeypatch):
    make_chat(monkeypatch)
    monkeypatch.setattr(share.shutil, "which", lambda name: None)
    run(nc.run_input("/share"))
    assert "isn't installed" in plain(nc.st.output)
    calls = []

    async def fake_gh(*args, timeout=60):
        calls.append(args)
        return 0, ""

    async def no(tool, preview, dangerous):
        return False
    monkeypatch.setattr(share.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(share, "gh", fake_gh)
    monkeypatch.setattr(nc, "ask_permission", no)
    run(nc.run_input("/share"))
    assert calls == [("auth", "status")]                     # asked, said no: nothing uploaded
