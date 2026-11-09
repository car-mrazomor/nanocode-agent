# Compare

Send one prompt to several models and see the answers side by side.

```
/compare models anthropic/claude-sonnet-5, z-ai/glm-5.3, ollama:qwen3:8b
/compare explain this regex: ^(?=.*\d).{8,}$
```

- **Models** can be OpenRouter models, `ollama:<model>` or `claude:<model>` (your subscription). Up to six. `/compare` alone shows the current set; `/compare models` with no names goes back to the defaults. The choice is saved as `"compare_models"`.
- **Layout:** answers appear side by side, or one under the other in a narrow terminal, each with its time, tokens and cost.
- **All at once:** the prompt goes to every model in parallel.

## Only what you send, only when you ask

- Compare is a **command, not a tool**. The model can never start a comparison.
- It sends **only the prompt you typed**, plus any files you attach with `@`. No chat history, memory, project details or system prompt.
- The answers **don't become part of your chat**.
