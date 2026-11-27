"""More providers: OpenAI, Anthropic, Gemini, DeepSeek, Groq, Mistral, xAI, Together, Fireworks, LM Studio and
any other OpenAI-compatible API ("custom"). They all run in the core's OpenRouter loop (backend "api"); each one
here says where to send the request, with which key, which fields it takes, what its models are and cost."""
import asyncio
import getpass
import os
import sys
import time

import httpx

import nanocode as nc
from nanokit import command

# Price per 1M tokens (input, output) in USD, for the models we know. Others show "cost unknown".
PRICES = {
    "gpt-5.6-sol": (4, 20), "gpt-5.6-terra": (2, 12), "gpt-5.6-luna": (0.20, 1.20),
    "claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10), "claude-haiku-5-5": (0.10, 0.50),
    "gemini-4-argon": (2, 10), "gemini-3.8-flash": (0.83, 4.12),
    "deepseek-v4-pro": (0.67, 1.33), "deepseek-v4.1-flash": (0.05, 0.57),
    "grok-4.7": (2, 6),
}
CACHE_SECONDS = 600   # a provider's model list is asked for again after this long
SKIP_MODELS = ("embed", "tts", "whisper", "dall-e", "image", "audio", "moderation", "realtime", "transcribe",
               "rerank", "guard", "ocr")


class Provider:
    """An OpenAI-compatible chat completions API. The config can change any of it:
    "providers": {"<name>": {"base_url", "api_key", "models": [...], "headers": {...}, "max_tokens", "reasoning"}}"""

    def __init__(self, name, label, desc, base_url, env=(), default=None, models=(), keys_url="",
                 needs_key=True, reasoning=False, max_tokens="max_tokens", usage_option=True, local=False):
        self.name, self.label, self.desc, self.base_url = name, label, desc, base_url
        self.env, self.default, self.known = tuple(env), default, list(models)
        self.keys_url, self.needs_key, self.reasoning, self.local = keys_url, needs_key, reasoning, local
        self.max_tokens = max_tokens      # "max_tokens", "max_completion_tokens", a cap (int), or None: not sent
        self.usage_option = usage_option  # sends stream_options.include_usage (Mistral rejects unknown fields)
        self._models = {}                 # (url, key) -> (when, {model: description}, reachable: True/False/None)
        self._fetching = {}               # (url, key) -> the background task asking the API

    def settings(self):
        value = (nc.st.config.get("providers") or {}).get(self.name)
        return value if isinstance(value, dict) else {}

    def base(self):
        return (self.settings().get("base_url") or self.base_url or "").rstrip("/")

    def url(self):
        return self.base() + "/chat/completions"

    def key(self):
        keys = nc.st.config.get("api_keys") or {}
        return self.settings().get("api_key") or keys.get(self.name) or \
            next((os.environ[v] for v in self.env if os.environ.get(v)), None)

    def headers(self, require=True):
        key = self.key()
        if not key and self.needs_key and require:
            raise RuntimeError(f"no {self.label} API key: /provider key {self.name} <key>" +
                               (f" (or set {self.env[0]})" if self.env else ""))
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if self.name == "anthropic" and key:
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        headers.update(self.settings().get("headers") or {})
        return headers

    def prepare(self, payload, reasoning):
        """The OpenRouter request made fit for this API: no OpenRouter-only fields, its own thinking setting."""
        wanted = payload.get("max_tokens") or 16000
        payload = {k: v for k, v in payload.items() if k not in ("usage", "reasoning", "max_tokens")}
        style = self.settings().get("max_tokens", self.max_tokens)
        if style == "max_completion_tokens":
            payload["max_completion_tokens"] = wanted
        elif style == "max_tokens":
            payload["max_tokens"] = wanted
        elif isinstance(style, int) and style > 0:
            payload["max_tokens"] = min(wanted, style)
        if self.usage_option:
            payload["stream_options"] = {"include_usage": True}
        if self.settings().get("reasoning", self.reasoning) and reasoning and reasoning.get("effort"):
            payload["reasoning_effort"] = reasoning["effort"]   # dropped if the model rejects it (no_think)
        return payload

    def cost(self, model, usage):
        if usage.get("cost"):
            return usage["cost"]
        price = PRICES.get(model)
        if not price:
            return 0
        return ((usage.get("prompt_tokens") or 0) * price[0] + (usage.get("completion_tokens") or 0) * price[1]) / 1e6

    def cost_note(self, model):
        return "free (local)" if self.local else None if model in PRICES else "cost unknown"

    def error(self, status, message):
        if status in (401, 403):
            return f"{self.label} rejected the API key ({message}). Save a new one: /provider key {self.name} <key>"
        if status == 402:
            return f"{self.label}: out of credits ({message})"
        if status == 404 and "model" in message.lower():
            return f"{self.label}: {message}. Pick another model with /model"
        return f"{self.label} {status}: {message}"

    def fetch_models(self, timeout=3):
        """The API's own list (GET /models), or [] when it has none or can't be reached."""
        resp = httpx.get(self.base() + "/models", headers={**nc.client_headers(), **self.headers(require=False)},
                         timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        names = []
        for item in items or []:
            name = item.get("id") if isinstance(item, dict) else item
            if isinstance(name, str) and not any(word in name.lower() for word in SKIP_MODELS):
                names.append(name.removeprefix("models/"))
        return names

    def _ask(self, timeout=3):
        """(model names from the API, reachable): True / False, or None when there was nothing to ask. Blocks."""
        if not self.base() or (self.needs_key and not self.key()):
            return [], None
        try:
            return self.fetch_models(timeout=timeout), True
        except Exception:
            return [], False

    def _store(self, cache, fetched, reachable):
        names = list(dict.fromkeys(self.known + list(self.settings().get("models") or []) + sorted(fetched)))
        self._models[cache] = (time.monotonic(), {name: self.describe(name) for name in names[:80]}, reachable)
        return self._models[cache][1]

    def _fresh(self, cache):
        entry = self._models.get(cache)
        return entry if entry and time.monotonic() - entry[0] < CACHE_SECONDS else None

    def models(self):
        """{model: description}, cached for 10 minutes. Inside the event loop (the app) it never waits for the
        network: it answers from the cache (or the known models) and refreshes in a thread."""
        cache = (self.base(), self.key())
        entry = self._fresh(cache)
        if entry:
            return entry[1]
        try:
            asyncio.get_running_loop()
        except RuntimeError:                   # no event loop (startup, `nanocode setup`): ask right away
            return self._store(cache, *self._ask())
        task = self._fetching.get(cache)
        if task is None or task.done():
            async def refresh():
                self._store(cache, *await asyncio.to_thread(self._ask))
            self._fetching[cache] = asyncio.ensure_future(refresh())
        stale = self._models.get(cache)
        if stale:
            return stale[1]
        names = list(dict.fromkeys(self.known + list(self.settings().get("models") or [])))
        return {name: self.describe(name) for name in names[:80]}

    async def models_async(self):
        """models() for async code: waits for the list, in a thread, without blocking the event loop."""
        cache = (self.base(), self.key())
        entry = self._fresh(cache)
        if entry:
            return entry[1]
        return self._store(cache, *await asyncio.to_thread(self._ask))

    def describe(self, name):
        if self.local:
            return "local"
        price = PRICES.get(name)
        return f"${price[0]:g} / ${price[1]:g}" if price else ""

    def default_model(self):
        configured = list(self.settings().get("models") or [])
        if self.default or configured:
            return self.default or configured[0]
        return next(iter(self.models()), None)   # LM Studio: the first model it has

    def status(self):
        """Why it can't be used now, or None."""
        if not self.base():
            return f'no base_url: set "providers": {{"{self.name}": {{"base_url": "https://…/v1"}}}} in the config'
        if self.needs_key and not self.key():
            return f"no API key yet: /provider key {self.name} <key>" + (f" (or set {self.env[0]})" if self.env else "")
        if self.local:                         # no network here: what the last look at its models found
            entry = self._models.get((self.base(), self.key()))
            if entry and entry[2] is False:
                return f"{self.label} isn't running at {self.base()} (start its local server)"
        if nc.st.provider == self.name and not nc.st.model:
            return f"no model: load one in {self.label}, or pick one with /model <name>"
        return None

    def setup(self, models, current):
        """`nanocode setup`: the key (and base URL) and the model."""
        settings = nc.st.config.setdefault("providers", {}).setdefault(self.name, {}) if self.name == "custom" \
            else None
        if settings is not None:
            settings["base_url"] = nc.ask("Base URL (ends in /v1 for most APIs)", settings.get("base_url", "")).rstrip("/")
        if not self.needs_key:
            print(f"\n  {nc.DIM}{self.desc}. No key needed.{nc.RESET}")
        elif next((v for v in self.env if os.environ.get(v)), None) and not self.settings().get("api_key"):
            print(f"\n  {nc.DIM}Using {next(v for v in self.env if os.environ.get(v))} from your environment.{nc.RESET}")
        else:
            saved = self.key() or ""
            keep = f" {nc.DIM}[keep {saved[:8]}…{saved[-4:]}]{nc.RESET}" if saved else ""
            if self.keys_url:
                print(f"\n  {nc.DIM}Get a key at {self.keys_url}{nc.RESET}")
            try:
                key = getpass.getpass(f"  {self.label} API key{keep}: ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                sys.exit(130)
            if key and settings is not None:
                settings["api_key"] = key
            elif key:
                nc.st.config.setdefault("api_keys", {})[self.name] = key
            elif not saved and self.name != "custom":
                print(f"  {nc.YELLOW}No key saved. Add it later: /provider key {self.name} <key>{nc.RESET}")
        choices = self.models()
        default = current if current else self.default_model()
        if choices:
            models[self.name] = nc.pick("Model (or type any model name)", [(k, k, v) for k, v in choices.items()],
                                        default)
        else:
            models[self.name] = nc.ask("Model name", default or "")
        if settings is not None and models[self.name] and models[self.name] not in settings.setdefault("models", []):
            settings["models"].append(models[self.name])


PROVIDER_LIST = [
    Provider("openai", "OpenAI", "GPT models with your OpenAI key, pay per token", "https://api.openai.com/v1",
             ["OPENAI_API_KEY"], "gpt-5.6-sol", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
             "https://platform.openai.com/api-keys", reasoning=True, max_tokens="max_completion_tokens"),
    Provider("anthropic", "Anthropic API", "Claude models with an API key, pay per token",
             "https://api.anthropic.com/v1", ["ANTHROPIC_API_KEY"], "claude-sonnet-5-5",
             ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"], "https://console.anthropic.com/settings/keys"),
    Provider("gemini", "Google Gemini", "Gemini models with a Google AI Studio key",
             "https://generativelanguage.googleapis.com/v1beta/openai", ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
             "gemini-4-argon", ["gemini-4-argon", "gemini-3.8-flash"], "https://aistudio.google.com/apikey",
             reasoning=True, max_tokens=None),
    Provider("deepseek", "DeepSeek", "DeepSeek models, low prices", "https://api.deepseek.com/v1",
             ["DEEPSEEK_API_KEY"], "deepseek-chat", ["deepseek-chat", "deepseek-reasoner"],
             "https://platform.deepseek.com/api_keys", max_tokens=8192),
    Provider("groq", "Groq", "open models on very fast hardware", "https://api.groq.com/openai/v1",
             ["GROQ_API_KEY"], "openai/gpt-oss-120b", ["openai/gpt-oss-120b", "moonshotai/kimi-k2-instruct"],
             "https://console.groq.com/keys", reasoning=True, max_tokens=None),
    Provider("mistral", "Mistral", "Mistral and Devstral models", "https://api.mistral.ai/v1",
             ["MISTRAL_API_KEY"], "mistral-large-latest", ["mistral-large-latest", "devstral-medium-latest"],
             "https://console.mistral.ai/api-keys", max_tokens=None, usage_option=False),
    Provider("xai", "xAI", "Grok models", "https://api.x.ai/v1", ["XAI_API_KEY"], "grok-4.7", ["grok-4.7"],
             "https://console.x.ai", max_tokens=None),
    Provider("together", "Together AI", "open models (Qwen, Kimi, Llama, DeepSeek…)", "https://api.together.xyz/v1",
             ["TOGETHER_API_KEY"], "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8", [],
             "https://api.together.ai/settings/api-keys", max_tokens=None),
    Provider("fireworks", "Fireworks AI", "open models, fast", "https://api.fireworks.ai/inference/v1",
             ["FIREWORKS_API_KEY"], "accounts/fireworks/models/kimi-k2-instruct", [],
             "https://fireworks.ai/account/api-keys", max_tokens=8192),
    Provider("lmstudio", "LM Studio", "local models in LM Studio, free and private", "http://localhost:1234/v1",
             ["LMSTUDIO_API_KEY"], needs_key=False, max_tokens=None, local=True),
    Provider("custom", "Custom API", "any OpenAI-compatible API (vLLM, LiteLLM, a proxy…)", "",
             ["CUSTOM_API_KEY"], needs_key=False, max_tokens=None),
]

for _p in PROVIDER_LIST:
    nc.PROVIDERS.setdefault(_p.name, ("api", _p.label, _p.desc))
    nc.API_PROVIDERS.setdefault(_p.name, _p)
    if _p.default:
        nc.DEFAULT_MODELS.setdefault(_p.name, _p.default)


def mask(key):
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "set"


@command("/provider", "list providers, or switch: /provider <name> · save a key: /provider key <name> <key>")
async def provider_command(arg):
    words = arg.split()
    if words[:1] != ["key"]:
        await nc.cmd_provider(arg)
        return
    if len(words) < 2 or (words[1] not in nc.API_PROVIDERS and words[1] != "openrouter"):
        names = ", ".join(["openrouter", *nc.API_PROVIDERS])
        nc.out(f"{nc.DIM}usage: /provider key <name> <key>  (names: {names}){nc.RESET}\n")
        return
    name = words[1]
    if len(words) == 2:
        api = nc.API_PROVIDERS.get(name)
        key = api.key() if api else (nc.st.config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY"))
        nc.out(f"{nc.DIM}{name} key: {mask(key) if key else 'none'}{nc.RESET}\n")
        return
    key = words[2]
    if name == "openrouter":
        nc.st.config["openrouter_api_key"] = key
        if nc.st.provider == "openrouter":
            nc.st.openrouter_key = key
    else:
        nc.st.config.setdefault("api_keys", {})[name] = key
        nc.API_PROVIDERS[name]._models.clear()
    nc.save_config()
    nc.out(f"{nc.GREEN}⏺ Saved the {nc.PROVIDERS[name][1]} key{nc.RESET} {nc.DIM}({mask(key)}) · "
           f"switch to it: /provider {name}{nc.RESET}\n")
