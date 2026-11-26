import asyncio
import json

import httpx
import pytest

import nanocode as nc
from nanokit import providers
from test_nanocode import mock_http, plain, run, sse

KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
            "GROQ_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY",
            "LMSTUDIO_API_KEY", "CUSTOM_API_KEY")


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    for api in nc.API_PROVIDERS.values():
        api._models.clear()


def test_providers_are_registered():
    for name in ("openai", "anthropic", "gemini", "deepseek", "groq", "mistral", "xai", "together", "fireworks",
                 "lmstudio", "custom"):
        assert nc.PROVIDERS[name][0] == "api" and name in nc.API_PROVIDERS
    assert list(nc.PROVIDERS)[:3] == ["claude", "openrouter", "ollama"]


def test_openai_turn_uses_its_url_key_and_fields(sandbox, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    nc.st.config = {"provider": "openai", "think": "high"}
    assert nc.apply_config() is None and nc.st.model == "gpt-5.6-sol"
    requests = []

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        requests.append((str(req.url), req.headers.get("authorization"), json.loads(req.content)))
        return sse([{"choices": [{"delta": {"content": "Hi."}}]},
                    {"choices": [], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}])

    mock_http(monkeypatch, handler)
    run(nc.send("hello"))
    url, auth, body = requests[0]
    assert url == "https://api.openai.com/v1/chat/completions" and auth == "Bearer sk-test"
    assert "usage" not in body and "reasoning" not in body and "max_tokens" not in body
    assert body["max_completion_tokens"] == 16000 and body["reasoning_effort"] == "high"
    assert body["stream_options"] == {"include_usage": True}
    assert nc.st.usage["cost"] == pytest.approx(4.0)
    assert "Hi." in plain(nc.st.output)


def test_rejected_reasoning_is_dropped(sandbox, monkeypatch):
    nc.st.config = {"provider": "xai", "api_keys": {"xai": "k"}, "think": "low",
                    "providers": {"xai": {"reasoning": True}}}
    assert nc.apply_config() is None
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {"message": "reasoning_effort is not supported"}})
        return sse([{"choices": [{"delta": {"content": "ok"}}]}])

    mock_http(monkeypatch, handler)
    run(nc.send("hi"))
    assert "reasoning_effort" in bodies[0] and "reasoning_effort" not in bodies[1]
    assert ("api", "grok-4.7") in nc.st.no_think
    assert "cost unknown" not in plain(nc.st.output)   # grok-4.7 has a price


def test_anthropic_headers_and_unknown_cost(sandbox, monkeypatch):
    nc.st.config = {"provider": "anthropic", "api_keys": {"anthropic": "sk-ant"},
                    "models": {"anthropic": "claude-new-model"}}
    assert nc.apply_config() is None
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), key=req.headers.get("x-api-key"), version=req.headers.get("anthropic-version"))
        return sse([{"choices": [{"delta": {"content": "Hello"}}]},
                    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}])

    mock_http(monkeypatch, handler)
    run(nc.send("hi"))
    assert seen == {"url": "https://api.anthropic.com/v1/chat/completions", "key": "sk-ant", "version": "2023-06-01"}
    assert "cost unknown" in plain(nc.st.output)


def test_auth_error_names_the_provider(sandbox, monkeypatch):
    nc.st.config = {"provider": "groq", "api_keys": {"groq": "bad"}}
    assert nc.apply_config() is None
    mock_http(monkeypatch, lambda req: httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))
    run(nc.run_input("hi"))
    assert "Groq rejected the API key" in nc.st.last_error and "/provider key groq" in nc.st.last_error


def test_custom_provider_and_model_list(sandbox, monkeypatch):
    nc.st.config = {"provider": "custom", "providers": {"custom": {
        "base_url": "http://box:8000/v1/", "api_key": "c", "models": ["my-model"]}}}
    monkeypatch.setattr(nc.httpx, "get", lambda url, **kw: httpx.Response(
        200, json={"data": [{"id": "other"}, {"id": "text-embedding-3"}]}, request=httpx.Request("GET", url)))
    assert nc.apply_config() is None and nc.st.model == "my-model"
    assert list(nc.model_choices()) == ["my-model", "other"]
    assert nc.API_PROVIDERS["custom"].url() == "http://box:8000/v1/chat/completions"
    nc.st.config["providers"]["custom"]["base_url"] = ""
    assert "base_url" in nc.apply_config()


def test_gemini_lists_models_without_prefix(sandbox, monkeypatch):
    nc.st.config = {"api_keys": {"gemini": "g"}}
    calls = []

    def get(url, **kw):
        calls.append((url, kw["headers"]))
        return httpx.Response(200, json={"data": [{"id": "models/gemini-9"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(nc.httpx, "get", get)
    models = nc.API_PROVIDERS["gemini"].models()
    assert "gemini-9" in models and models["gemini-4-argon"] == "$2 / $10"
    assert calls[0][0].startswith("https://generativelanguage.googleapis.com/v1beta/openai/models")
    nc.API_PROVIDERS["gemini"].models()
    assert len(calls) == 1   # cached


def test_lmstudio_needs_no_key(sandbox, monkeypatch):
    nc.st.config = {"provider": "lmstudio"}

    def down(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(nc.httpx, "get", down)
    assert "isn't running" in nc.apply_config()
    nc.API_PROVIDERS["lmstudio"]._models.clear()
    monkeypatch.setattr(nc.httpx, "get", lambda url, **kw: httpx.Response(
        200, json={"data": [{"id": "qwen3-coder"}]}, request=httpx.Request("GET", url)))
    assert nc.apply_config() is None and nc.st.model == "qwen3-coder"
    assert nc.fmt_cost(0) == "free (local)"
    assert "authorization" not in {k.lower() for k in nc.API_PROVIDERS["lmstudio"].headers()}


def test_provider_key_command_and_switch(sandbox, monkeypatch):
    nc.st.config = {"provider": "openrouter", "openrouter_api_key": "k"}
    assert nc.apply_config() is None
    nc.st.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    run(nc.run_input("/provider openai"))
    assert nc.st.provider == "openrouter" and "no API key" in plain(nc.st.output)
    run(nc.run_input("/provider key openai sk-abcdefghijklmnop"))
    assert nc.load_config()["api_keys"] == {"openai": "sk-abcdefghijklmnop"}
    assert "sk-abcdefghijklmnop" not in plain(nc.st.output)
    run(nc.run_input("/provider openai"))
    assert nc.st.provider == "openai" and nc.st.backend == "api" and len(nc.st.messages) == 2
    run(nc.run_input("/provider"))
    screen = plain(nc.st.output)
    assert "● openai" in screen and "deepseek" in screen and "lmstudio" in screen
    run(nc.run_input("/provider key openrouter sk-or-new"))
    assert nc.load_config()["openrouter_api_key"] == "sk-or-new"
    run(nc.run_input("/provider key nope x"))
    assert "usage: /provider key" in plain(nc.st.output)


def test_setup_wizard_more_providers(sandbox, monkeypatch):
    answers = iter(["4", "deepseek", "", "1"])   # More providers → DeepSeek → first model → ask first
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(nc.getpass, "getpass", lambda prompt="": "sk-deep")
    monkeypatch.setattr(nc.httpx, "get", lambda url, **kw: httpx.Response(
        200, json={"data": [{"id": "deepseek-chat"}]}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(nc, "rea_runtime", lambda: None)
    nc.setup_wizard()
    cfg = json.loads(nc.config_file().read_text())
    assert cfg["provider"] == "deepseek" and cfg["models"]["deepseek"] == "deepseek-chat"
    assert cfg["api_keys"] == {"deepseek": "sk-deep"}


def test_compare_with_a_provider_prefix(sandbox, monkeypatch):
    nc.st.config = {"api_keys": {"mistral": "m"}}
    seen = []

    def handler(req):
        seen.append((str(req.url), json.loads(req.content)))
        return sse([{"choices": [{"delta": {"content": "answer"}}]}])

    mock_http(monkeypatch, handler)
    result = run(nc.compare_one("mistral:mistral-large-latest", "hi"))
    assert result["error"] is None and result["text"] == "answer"
    assert seen[0][0] == "https://api.mistral.ai/v1/chat/completions"
    assert "stream_options" not in seen[0][1] and seen[0][1]["model"] == "mistral-large-latest"


def test_status_never_touches_the_network(sandbox, monkeypatch):
    nc.st.config = {"provider": "lmstudio"}

    def no_network(url, **kw):
        raise AssertionError("status() went to the network")
    monkeypatch.setattr(nc.httpx, "get", no_network)
    api = nc.API_PROVIDERS["lmstudio"]
    assert api.status() is None or "no model" in api.status()      # nothing known yet: no complaint
    api._store((api.base(), api.key()), [], False)
    assert "isn't running" in api.status()


def test_models_in_the_event_loop_dont_block_and_are_cached(sandbox, monkeypatch):
    import threading
    nc.st.config = {"api_keys": {"gemini": "g"}}
    calls, loop_thread = [], []

    def get(url, **kw):
        calls.append(threading.get_ident())
        return httpx.Response(200, json={"data": [{"id": "gemini-9"}]}, request=httpx.Request("GET", url))
    monkeypatch.setattr(nc.httpx, "get", get)
    api = nc.API_PROVIDERS["gemini"]

    async def go():
        loop_thread.append(threading.get_ident())
        first = api.models()                   # answers at once from the known models
        await asyncio.sleep(0.05)
        while api._fetching and not all(t.done() for t in api._fetching.values()):
            await asyncio.sleep(0.01)
        return first, api.models(), await api.models_async()
    first, second, third = run(go())
    assert "gemini-9" not in first and "gemini-4-argon" in first
    assert "gemini-9" in second and second == third and len(calls) == 1
    assert calls[0] != loop_thread[0]          # fetched in a thread, not on the event loop
    entry = api._models[(api.base(), api.key())]
    api._models[(api.base(), api.key())] = (entry[0] - providers.CACHE_SECONDS - 1, *entry[1:])
    api.models()                               # stale, outside a loop: asked again
    assert len(calls) == 2
