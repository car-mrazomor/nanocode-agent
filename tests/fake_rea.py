"""A tiny stand-in for the REA MCP server, used by the tests (run as: python fake_rea.py mcp)."""
import json
import os
import sys

TOOLS = [
    {"name": "inspect_artifact", "description": "Inspect a file and report its format. More text here.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "install_adb_package", "description": "Install an APK on a connected Android device.",
     "inputSchema": {"type": "object", "properties": {"apk": {"type": "string"}}}, "annotations": {}},
    {"name": "crash_server", "description": "Makes the fake server exit.", "inputSchema": {"type": "object"}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": 999, "method": "roots/list"})
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                      "serverInfo": {"name": "rea", "version": "0.0-test"}}})
    elif method == "tools/list":
        page = (msg.get("params") or {}).get("cursor")
        result = {"tools": TOOLS[:2], "nextCursor": "p2"} if not page else {"tools": TOOLS[2:]}
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if name == "crash_server":
            os._exit(1)
        if name == "inspect_artifact" and "path" not in args:
            send({"jsonrpc": "2.0", "id": rid, "result": {"isError": True, "content": [
                {"type": "text", "text": "arguments do not match the advertised input schema"}]}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": f"{name} ok: {json.dumps(args)}"}]}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}})
