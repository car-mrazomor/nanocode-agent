import sys
import time
from pathlib import Path

import pytest

from test_nanocode import call, nc, plain, run
from nanokit import lsp

FAKE = str(Path(__file__).with_name("fake_lsp.py"))


@pytest.fixture(autouse=True)
def fake_server(sandbox, monkeypatch):
    nc.st.config["lsp"] = {"python": [sys.executable, FAKE]}
    monkeypatch.setattr(lsp, "WAIT", 20.0)     # a busy CI machine (Windows) can take seconds to start Python
    yield
    lsp.stop_all()
    lsp.broken.clear()
    lsp.DECLINED.clear()


def test_edit_reports_errors(sandbox):
    text, err = call("write", path="app.py", content="x = 1\ny = ERROR\nWARN here\nHINT\n")
    assert not err
    assert "LSP (python" in text and "1 error, 1 warning" in text
    assert "app.py:2:5 error: found error" in text and "app.py:3:1 warning" in text and "hint" not in text
    assert "1 error" in plain(nc.st.output)
    text, err = call("edit", path="app.py", old="y = ERROR", new="y = 2")
    assert "LSP (python" in text and "1 warning" in text and "error:" not in text
    text, err = call("edit", path="app.py", old="WARN here", new="ok")
    assert "LSP" not in text                                      # clean: nothing added
    text, err = call("write", path="notes.txt", content="ERROR")
    assert "LSP" not in text                                      # no server for .txt


def test_diagnostics_tool(sandbox):
    Path("a.py").write_text("ERROR\n")
    text, err = call("diagnostics")
    assert "no language server is running" in text
    text, err = call("diagnostics", path="a.py")
    assert not err and "a.py:1:1 error" in text
    text, err = call("diagnostics")
    assert "a.py:1:1 error" in text
    text, err = call("diagnostics", path="x.txt")
    assert err and "not found" in text
    Path("x.go").write_text("package x\n")
    nc.st.config["lsp"] = {"go": "no-such-gopls-xyz"}
    text, err = call("diagnostics", path="x.go")
    assert "no go language server is installed" in text
    assert "diagnostics" in nc.READ_ONLY_TOOLS


def test_crash_restarts_once(sandbox):
    call("write", path="a.py", content="CRASH\n")                  # crashes, restarted, crashes again
    text, err = call("write", path="b.py", content="ERROR\n")
    assert not err and "LSP" not in text and "python" in lsp.broken
    lsp.broken.clear()
    lsp.stop_all()
    text, err = call("write", path="b.py", content="ERROR\n")      # a new session of servers works again
    assert "1 error" in text


def test_off(sandbox):
    nc.st.config["lsp"] = False
    text, err = call("write", path="a.py", content="ERROR\n")
    assert "LSP" not in text and not lsp.servers
    assert "off" in call("diagnostics", path="a.py")[0]


def test_shutdown_stops_servers(sandbox):
    call("write", path="a.py", content="x = 1\n")
    server = lsp.servers["python"]
    assert server.alive()
    run(lsp.shutdown())
    assert not server.alive() and not lsp.servers


STUCK = r'''
import json, sys, time
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
def send(msg):
    body = json.dumps(dict(msg, jsonrpc="2.0")).encode()
    stdout.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stdout.flush()
while True:
    length = None
    while True:
        line = stdin.readline()
        if not line:
            sys.exit(0)
        if not line.strip():
            break
        if line.lower().startswith(b"content-length"):
            length = int(line.split(b":")[1])
    msg = json.loads(stdin.read(length))
    if msg.get("method") == "initialize":
        send({"id": msg["id"], "result": {"capabilities": {}}})
    elif msg.get("method") == "garbage":
        stdout.write(b"Content-Length: 5\r\n\r\nnope!")
        stdout.flush()
    elif msg.get("method") == "textDocument/didOpen":
        time.sleep(60)                                    # stops reading its input
'''


def test_a_server_that_stops_reading_cant_hang_a_turn(sandbox, monkeypatch, tmp_path):
    script = tmp_path / "stuck.py"
    script.write_text(STUCK)
    nc.st.config["lsp"] = {"python": [sys.executable, str(script)]}
    monkeypatch.setattr(lsp, "WRITE_TIMEOUT", 0.5)
    Path("big.py").write_text("x = 1\n" * 400_000)                # far more than a pipe holds
    server = lsp.server_for("big.py")
    assert server.ready.wait(10)
    started = time.monotonic()
    assert server.check("big.py", 10) is None
    assert time.monotonic() - started < 5 and not server.alive()
    assert lsp.server_for("big.py") is not server                 # restarted


def test_unreadable_output_wakes_whoever_waits(sandbox, tmp_path):
    script = tmp_path / "stuck.py"
    script.write_text(STUCK)
    nc.st.config["lsp"] = {"python": [sys.executable, str(script)]}
    server = lsp.server_for("a.py")
    assert server.ready.wait(10)
    started = time.monotonic()
    with pytest.raises(OSError, match="unreadable"):
        server.request("garbage", {}, timeout=20)
    assert time.monotonic() - started < 5
    server.proc.wait(5)
    assert not server.alive()
    with pytest.raises(OSError):
        server.request("anything", {}, timeout=20)                # no wait for a server that's gone


def test_servers_that_run_project_code_need_trust(sandbox, monkeypatch):
    nc.st.config["lsp"] = {"rust": [sys.executable, FAKE]}
    Path("lib.rs").write_text("ERROR\n")
    asked = []

    async def answer(tool, preview, dangerous):
        asked.append(preview)
        return False

    monkeypatch.setattr(nc, "ask_permission", answer)
    text, err = call("write", path="lib.rs", content="ERROR\n")
    assert "LSP" not in text and "rust" not in lsp.servers and len(asked) == 1
    text, _ = call("diagnostics", path="lib.rs")                 # declined: not asked again this session
    assert "isn't trusted" in text and len(asked) == 1 and "rust" not in lsp.servers
    lsp.DECLINED.clear()
    monkeypatch.setattr(nc, "ask_permission", lambda *a: _yes())
    text, err = call("write", path="lib.rs", content="ERROR\n")
    assert "1 error" in text
    assert lsp.project_trusted() and nc.st.config["trusted_projects"]


async def _yes():
    return True
