# More providers

Besides the Claude subscription, OpenRouter and Ollama, nanocode can talk directly to any OpenAI-compatible API. You pay the provider directly, with your own key.

| Provider | Name | Key (config or environment) | Default model |
|---|---|---|---|
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-5.6-sol` |
| Anthropic API | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` |
| Google Gemini | `gemini` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | `gemini-4-argon` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| Groq | `groq` | `GROQ_API_KEY` | `openai/gpt-oss-120b` |
| Mistral | `mistral` | `MISTRAL_API_KEY` | `mistral-large-latest` |
| xAI | `xai` | `XAI_API_KEY` | `grok-4.7` |
| Together AI | `together` | `TOGETHER_API_KEY` | `Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8` |
| Fireworks AI | `fireworks` | `FIREWORKS_API_KEY` | `accounts/fireworks/models/kimi-k2-instruct` |
| LM Studio | `lmstudio` | none (local, `http://localhost:1234/v1`) | the first loaded model |
| Any other API | `custom` | in the config | the first in its `models` |

The Anthropic API is not the same as the Claude subscription. It uses an API key and is billed per token, through Anthropic's OpenAI-compatible endpoint.

## Setting one up

```sh
nanocode setup                       # pick "More providers", then the provider, key and model
```

Or while nanocode is running:

```sh
/provider key openai sk-...          # saves the key in ~/.nanocode/config.json
/provider openai                     # switches (the chat continues)
/provider                            # lists every provider and what each one still needs
/model                               # the provider's models, from its own model list
```

The model list is asked from the provider in the background and kept for 10 minutes, so `/model` never waits for the network (the first time, it may show only the known models; open it again a moment later for the full list). `/provider` doesn't go to the network either: for LM Studio it says it isn't running only when the last look at its models failed.

Keys are saved under `"api_keys"`. A key in the environment (`OPENAI_API_KEY`…) works too. If both are set, the saved key is used.

## Your own API (custom)

Any server that speaks the OpenAI chat completions API works, for example vLLM, LiteLLM, llama.cpp's server or a company proxy:

```json
"provider": "custom",
"providers": {
  "custom": {
    "base_url": "http://gpu-box:8000/v1",
    "api_key": "optional",
    "models": ["qwen3-coder"]
  }
}
```

The same `"providers"` entry can also change a built-in provider, for example to send OpenAI requests through a proxy (`"providers": {"openai": {"base_url": "..."}}`):

| Key | What it does |
|---|---|
| `base_url` | The API's address, up to `/v1` (nanocode adds `/chat/completions` and `/models`) |
| `api_key` | The key (instead of `"api_keys"` or the environment) |
| `models` | Models to list in `/model`, besides the ones the API reports |
| `headers` | Extra HTTP headers |
| `max_tokens` | A cap on the reply length, if the API rejects 16000 |
| `reasoning` | `true` to send the `/think` level as `reasoning_effort` |

## Thinking and cost

- `/think` is sent as `reasoning_effort` to OpenAI, Gemini and Groq. If a model rejects it, nanocode sends the request again without it.
- The cost after each reply comes from a small price table. Models not in it show **cost unknown**. LM Studio shows **free (local)**.
- `/compare` takes these providers too: `/compare openai:gpt-5.6-luna, gemini:gemini-3.8-flash`.
