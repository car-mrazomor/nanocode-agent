"""nanocode introduces itself to every provider, and logs each reply's usage for trackers."""
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from test_nanocode import mock_http, sse  # noqa: E402

REPLY = [{"choices": [{"delta": {"content": "hi"}}]},
         {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 7, "cost": 0.0021}}]


def capture(monkeypatch, reply=None):
    seen = []

    def handler(req):
        seen.append(req)
        return reply(req) if reply else sse(REPLY)
    mock_http(monkeypatch, handler)
    return seen


def lines():
    path = nc.usage_log_path()
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_openrouter_calls_say_nanocode(sandbox, monkeypatch):
    seen = capture(monkeypatch)
    nc.st.backend, nc.st.provider, nc.st.model, nc.st.openrouter_key = "api", "openrouter", "z-ai/glm-5.3", "k"
    nc.new_session()
    asyncio.run(nc.send("hello"))
    h = seen[0].headers
    assert h["user-agent"].startswith(f"nanocode/{nc.__version__} (") and h["x-title"] == "nanocode"
    assert "nanocode" in h["http-referer"] and h["authorization"] == "Bearer k"


def test_other_providers_say_nanocode_too_but_your_headers_win(sandbox, monkeypatch):
    seen = capture(monkeypatch)
    nc.st.config = {"provider": "openai", "api_keys": {"openai": "sk-x"}, "models": {"openai": "gpt-x"},
                    "providers": {"openai": {"headers": {"X-Title": "my-team"}}}}
    assert nc.apply_config() is None
    nc.new_session()
    asyncio.run(nc.send("hello"))
    h = seen[0].headers
    assert h["user-agent"].startswith("nanocode/") and h["x-title"] == "my-team" and h["authorization"] == "Bearer sk-x"


def test_ollama_calls_say_nanocode(sandbox, monkeypatch):
    def reply(req):
        body = "\n".join(json.dumps(x) for x in [{"message": {"role": "assistant", "content": "hi"}, "done": False},
                                                {"done": True, "prompt_eval_count": 50, "eval_count": 5}])
        return httpx.Response(200, text=body)
    seen = capture(monkeypatch, reply)
    nc.st.backend, nc.st.provider, nc.st.model = "ollama", "ollama", "qwen3"
    nc.st.ollama_models = [{"name": "qwen3", "tools": True}]
    nc.new_session()
    asyncio.run(nc.send("hello"))
    assert seen[0].headers["user-agent"].startswith("nanocode/")
    line = lines()[-1]
    assert line["provider"] == "ollama" and line["billing"] == "local" and line["input_tokens"] == 50


def test_every_reply_is_logged_for_trackers(sandbox, monkeypatch):
    capture(monkeypatch)
    nc.st.backend, nc.st.provider, nc.st.model, nc.st.openrouter_key = "api", "openrouter", "z-ai/glm-5.3", "k"
    nc.new_session()
    asyncio.run(nc.send("hello"))
    asyncio.run(nc.send("again"))
    first = lines()[0]
    assert len(lines()) == 2 and first["agent"] == "nanocode" and first["version"] == nc.__version__
    assert first["provider"] == "openrouter" and first["model"] == "z-ai/glm-5.3" and first["billing"] == "api"
    assert first["input_tokens"] == 120 and first["output_tokens"] == 7 and first["cost_usd"] == 0.0021
    assert first["surface"] == "terminal" and first["session"] == nc.st.session["id"]
    assert first["project"] == str(Path.cwd()) and first["timestamp"].endswith("Z")


def test_claude_subscription_replies_are_logged_as_nanocode(sandbox):
    nc.st.backend, nc.st.provider, nc.st.model = "sdk", "claude", "claude-opus-5-5"
    nc.new_session()
    nc.show_usage(1000, 50, 0.04, 3.2)
    line = lines()[-1]
    assert line["agent"] == "nanocode" and line["provider"] == "claude" and line["billing"] == "subscription"


def test_the_usage_log_can_be_turned_off(sandbox):
    nc.st.config["usage_log"] = False
    nc.new_session()
    nc.show_usage(10, 1, 0, 1)
    assert lines() == []
