"""A tiny stand-in language server for the tests: every line containing ERROR or WARN is reported, and a file
containing CRASH makes it exit (run as: python fake_lsp.py)."""
import json
import sys

stdin, stdout = sys.stdin.buffer, sys.stdout.buffer


def send(msg):
    body = json.dumps(dict(msg, jsonrpc="2.0")).encode()
    stdout.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stdout.flush()


def receive():
    length = None
    while True:
        line = stdin.readline()
        if not line:
            sys.exit(0)
        if not line.strip():
            break
        key, _, value = line.decode().partition(":")
        if key.lower() == "content-length":
            length = int(value)
    return json.loads(stdin.read(length))


while True:
    msg = receive()
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        send({"id": 1000, "method": "workspace/configuration", "params": {"items": [{}, {}]}})
        send({"id": msg["id"], "result": {"capabilities": {"textDocumentSync": 1}}})
    elif method == "shutdown":
        send({"id": msg["id"], "result": None})
    elif method == "exit":
        sys.exit(0)
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        doc = params["textDocument"]
        text = doc.get("text") if method.endswith("didOpen") else params["contentChanges"][-1]["text"]
        if "CRASH" in text:
            sys.exit(1)
        found = []
        for n, line in enumerate(text.splitlines()):
            for word, severity in (("ERROR", 1), ("WARN", 2), ("HINT", 4)):
                if word in line:
                    found.append({"range": {"start": {"line": n, "character": line.index(word)},
                                            "end": {"line": n, "character": len(line)}},
                                  "severity": severity, "message": f"found {word.lower()}"})
        send({"method": "textDocument/publishDiagnostics", "params": {"uri": doc["uri"], "diagnostics": found}})
