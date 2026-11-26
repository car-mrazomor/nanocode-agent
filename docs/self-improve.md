# Self-improvement

With `"self_improve": true` in `~/.nanocode/config.json`, nanocode learns from its hard turns on its own, like Hermes.

After a turn that needed **15 or more tool calls**, or where **2 or more tools failed**, a quiet review runs in the background. A sub-agent reads what happened in that turn (your request, the tool calls and their failures, the answer) and decides what's worth keeping:

- **lessons** (at most three short sentences) go to the project's memory, `.nanocode/memory.md`, each after asking (as `remember`) unless you're in Auto mode or a rule allows it,
- a **procedure** likely to come up again is saved as a project skill, or improves an existing skill with the same name. Saving a skill asks first unless you're in Auto mode.

Most turns teach nothing new, and then nothing is saved. When something is, one dim line in the chat says what was learned.

The review never blocks you: it starts after the turn has ended, shows none of its work, and runs at most once every 10 minutes. It costs one extra sub-agent call on your current model.

`/learn` still reviews the whole chat when you ask, and `"auto_learn": true` (see [configuration](configuration.md)) is the older, visible version of this that runs in the chat.
