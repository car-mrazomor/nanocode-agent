# Agents and planning

nanocode works over many turns on its own: it plans, calls tools, checks the results and keeps going until the task is done.

## Plans

For a task with several steps, the model keeps a checklist with the `todo` tool. It shows in the chat and updates as it works:

```
  ☑ Find where the config is parsed
  ◐ Add the new option
  ☐ Update the tests
```

The model sends the whole list each time, so what you see is always the current plan.

## Sub-agents

The `agent` tool hands a self-contained side task to a sub-agent: exploring the codebase, reading docs, researching on the web. A sub-agent:

- starts with a **fresh context**, so the main chat doesn't fill up with file dumps,
- works on its own and returns **only its result**,
- runs **in parallel** with other sub-agents when the model starts several at once,
- can **read, search and use the web**, but not edit files or run commands.

To let sub-agents edit and run commands too, set `"agent_edits": true` in `~/.nanocode/config.json`. They still ask before each change unless you're in Auto mode.
