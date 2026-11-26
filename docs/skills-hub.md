# Installing skills

nanocode reads skills in the open [Agent Skills](https://agentskills.io) format: a folder with a `SKILL.md` (see [Memory, skills and learning](memory-and-skills.md)). Community skills can be installed straight from GitHub:

| Command | What it does |
|---|---|
| `/skills` | List your skills |
| `/skills hub [query]` | Browse the skills in the index repository ([anthropics/skills](https://github.com/anthropics/skills)), optionally filtered |
| `/skills install <name>` | Install a skill from the index by name, e.g. `/skills install pdf` |
| `/skills install owner/repo[/path]` | Install from any repository: every skill in it, or the one in `path` |
| `/skills install https://github.com/owner/repo/tree/main/skills/pdf` | A GitHub link works too |
| `/skills remove <name>` | Delete an installed skill |

- Skills go to `~/.nanocode/skills` (every project). Add `--project` to install into (or remove from) `.nanocode/skills` instead.
- Before installing, nanocode shows what it found and asks. A skill is instructions (and sometimes scripts) the model will follow, so read it if you're unsure about its source.
- Each `SKILL.md` is checked: it needs frontmatter with a `name` (lowercase letters, digits and `-`) and a `description`. An installed skill with the same name is replaced.
- Downloads use GitHub's API. If you hit its rate limit, set `GITHUB_TOKEN` (or `GH_TOKEN`).
- To browse another index, set `"skills_hub": "owner/repo"` in the config.
