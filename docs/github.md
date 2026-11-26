# GitHub agent

Comment `/nanocode` (or `/nc`) on an issue or a pull request, and nanocode works on it in GitHub Actions:

- **On an issue:** it reads the issue and your comment and does what you ask. If it changed files, it pushes a new branch `nanocode/issue-<n>-…` and opens a pull request that closes the issue. Otherwise it answers in a comment.
- **On a pull request** (a comment, or a review comment on a line): it works on the pull request's branch, commits and pushes there, and answers in a comment.

## Set it up

In your repository:

```sh
nanocode github install
```

This writes `.github/workflows/nanocode.yml`. Then:

1. Add the secret **`OPENROUTER_API_KEY`**: Settings → Secrets and variables → Actions → New repository secret (or `gh secret set OPENROUTER_API_KEY`).
2. Optional: add the variable **`NANOCODE_MODEL`** (same page, Variables tab) to pick the model, e.g. `anthropic/claude-sonnet-5`. Without it, OpenRouter's default is used.
3. Commit and push the workflow.
4. Comment `/nanocode fix the typo in the README` on an issue.

`nanocode github install --force` rewrites the workflow file.

## How it runs

The workflow runs on new comments that contain `/nanocode` or `/nc`, **only from people with write access** (owners, members, collaborators). It has two jobs:

1. **`agent`** checks out the repository, installs nanocode, and runs `nanocode github run --agent-only <dir>`. That reads the event (`GITHUB_EVENT_PATH`), gives the model the issue or pull request title and description, the comment (and the code it's on, for review comments), and runs one turn with edits and commands allowed. Dangerous commands are still refused. It writes the answer and the changes (a patch; `.nanocode/` is never included) to `<dir>`, which is uploaded as the `nanocode-out` artifact.
2. **`publish`** downloads it and runs `nanocode github run --publish <dir>`: it applies the patch, commits as `nanocode[bot]`, pushes (a new branch and pull request for an issue, the pull request's own branch for a pull request) and posts the answer as a comment. No model and no agent run in this job.

`nanocode github run` without a flag does both in one process (for older workflows).

## Security

Issue and pull request text is written by other people, and the agent runs commands without asking, so treat everything the agent job can reach as readable by whoever wrote that text:

- **The agent job can't write.** Its `GITHUB_TOKEN` has read-only permissions, and only the `publish` job, which runs no agent and no code from the repository, gets `contents: write`, `pull-requests: write` and `issues: write`.
- **No credentials in the agent's environment.** Before the agent starts, nanocode takes `GH_TOKEN`, `GITHUB_TOKEN` and every `*_API_KEY`, `*_TOKEN`, `*_SECRET` and `*_PASSWORD` variable out of its environment, so `bash`, shells and MCP servers don't inherit them. The model's key moves into nanocode's in-memory config. nanocode's own `gh` calls and `git push` get the token back; `git push` runs with the checkout's hooks and credential helpers turned off.
- **Checkouts don't keep the token:** both jobs use `persist-credentials: false`; fetches and pushes go through `gh auth git-credential`.
- **Secrets are redacted** from every comment, pull request body and error before it's posted: the values of those environment variables, every key and token in the config, and anything that looks like an API key.
- **Pull requests from forks** are answered read-only: their code isn't checked out or run. The agent gets their diff, can only use read-only tools (no `bash`, no edits), and nothing is pushed.
- **nanocode is pinned.** The workflow downloads the tagged release `v<NANOCODE_VERSION>` (the version that wrote it), not the latest code. Change `NANOCODE_VERSION` in the workflow to upgrade. For a stricter pin, vendor that tarball or check its checksum.

What this doesn't stop: the model's key (`OPENROUTER_API_KEY`) is in the agent job, and a GitHub-hosted runner gives every step `sudo`, so a determined prompt injection can still get at it (for example by reading the nanocode process's memory) and send it out. Use a key with a spending limit, and keep the trigger limited to people with write access.

## Other providers

`nanocode github run` uses OpenRouter when `OPENROUTER_API_KEY` is set. To use something else, set `NANOCODE_PROVIDER` (`openrouter` or `ollama`, with `OLLAMA_HOST` pointing at a server the runner can reach) in the workflow's `env`. The Claude subscription needs a logged-in Claude Code, so it isn't a good fit for CI.
