# Getting started

```sh
cd your-project
nanocode
```

## First setup

The first start asks three things. Run `nanocode setup` any time to change them.

1. **Provider**: where the model runs.
2. **Model**.
3. **Permissions**: ask before edits and commands (`ask`), or just do it (`auto`).

Everything is saved in `~/.nanocode/config.json`. `nanocode doctor` checks that the provider, model, key and tools all work.

## Providers

| Provider | What you need | Cost |
|---|---|---|
| **Claude subscription** | Claude Max or Pro. Log in once with [Claude Code](https://claude.com/claude-code): `claude`, then `/login` | Covered by your plan |
| **OpenRouter** | A key from [openrouter.ai/keys](https://openrouter.ai/keys) (or `OPENROUTER_API_KEY`) | Per token |
| **Ollama** | [Ollama](https://ollama.com/download) and a model with tool support, e.g. `ollama pull qwen3` | Free, local |

Switch while running with `/provider claude`, `/provider openrouter` or `/provider ollama`. For one run: `nanocode --provider ollama -m qwen3`.

Default models: `claude-opus-5-5` on the Claude subscription, `openrouter/auto-code` on OpenRouter. On Ollama you pick one of your installed models.

## Models

`/model` lists models: with prices on OpenRouter, the installed ones on Ollama. `/model <name>` switches (Tab completes). The choice is remembered per provider.

**Ollama tips:**

- Use a model with tool support, or it can't edit files or run commands. `/model` and `nanocode doctor` flag models without it.
- The context size is picked from your RAM: 8k, 16k or 32k. Too large a context spills onto the CPU and gets very slow. On an 8 GB Mac, one reply took 12 minutes at 32k and 10 seconds at 8k. Override it with `"ollama_num_ctx"`.

## Thinking

`/think off|low|medium|high` sets how hard the model thinks. The default is `medium`. The level shows under the prompt next to the model; click it to change.

Each provider gets it the way it expects:

| Provider | Sent as |
|---|---|
| OpenRouter | `reasoning` effort (`off` sends `enabled: false`) |
| Ollama | `think` |
| Claude subscription | adaptive thinking with an effort level, or thinking disabled |

If a model can't think, or can't stop thinking, nanocode retries the request without the setting, so the level never breaks a reply.
