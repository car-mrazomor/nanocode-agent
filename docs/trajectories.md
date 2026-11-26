# Exporting trajectories

Your saved chats can become training data for fine-tuning a model: one JSON line per chat, with every tool call and result.

```
nanocode trajectories export [--format sharegpt|openai] [--out file.jsonl] [--all-projects] [--compress]
```

| Option | What it does |
|---|---|
| `--format sharegpt` | The default. `{"conversations": [{"from": "human" \| "gpt" \| "function_call" \| "observation", "value": ...}]}` |
| `--format openai` | `{"messages": [...]}` in the OpenAI chat format, with `tool_calls` and `tool` messages |
| `--out file.jsonl` | Write to a file (default: standard output; the summary goes to standard error) |
| `--all-projects` | Every project with saved chats (the ones nanocode has chatted in, and any found a few folders deep in your home folder), not only this one |
| `--compress` | Shorten long tool output (head and tail kept) and drop failed tool calls that the model then retried |

Notes:

- Only chats with OpenRouter or Ollama are exported: they keep their full messages. Claude subscription chats keep their history inside Claude Code, so they're skipped (the summary says how many).
- Unfinished tool calls (a chat stopped mid-step) are left out, and attached images become `[image]`.
- The system prompt isn't included.
- Chats can contain private things (code, keys you pasted): check the file before sharing it.
