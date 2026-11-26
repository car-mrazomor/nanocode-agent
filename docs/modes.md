# Plan and build modes

Press **Shift+Tab** (or type `/plan` and `/build`) to switch modes.

- **Build** (the default): the model edits files and runs commands as usual.
- **Plan**: the model explores and plans but doesn't change anything. It can read, search and fetch, and it can run commands to look around, but every command asks you first, even with `/auto` on. Edits, writes and other tools that change things are refused. It ends with a plan: which files to change and how, and the risks.

When the plan looks right, switch to build mode and say "go ahead".

Each tab has its own mode. `/plan <message>` and `/build <message>` switch and send the message in one step.
