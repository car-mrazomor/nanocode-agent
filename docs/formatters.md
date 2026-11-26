# Formatters

After the model writes or edits a file, nanocode runs your project's formatter on it, so its code matches yours. The model is told ("formatted with black"), so it reads the file again before editing the changed lines.

A formatter runs only if your project uses it:

| Files | Formatter | When |
|---|---|---|
| `.py` | `ruff format` | `ruff.toml`, `[tool.ruff]` in `pyproject.toml`, or `ruff-format` in `.pre-commit-config.yaml` |
| `.py` | `black` | `[tool.black]` in `pyproject.toml`, or black in pre-commit, `setup.cfg` or `tox.ini` |
| `.js` `.ts` `.jsx` `.tsx` `.css` `.json` `.md` `.yaml` `.html` | `prettier` | A prettier config file, or prettier in `package.json` |
| `.go` | `gofmt` | Always, if installed |
| `.rs` | `rustfmt` | Always, if installed (uses the edition from `Cargo.toml`) |

nanocode looks for the tool in the project first (`node_modules/.bin`, `.venv`), then on your PATH. It never downloads anything. If a formatter fails (for example on a syntax error), the model sees the error.

A formatter that is the project's own code runs only in a project you trust: one from `node_modules/.bin` or a virtualenv in the project, and prettier (its config and plugins can be JavaScript). nanocode asks once per project, even with `/auto`, and remembers the answer in `"trusted_projects"` in your config; until then (and always when running headless in an untrusted project) that formatter is skipped. Formatters you set yourself in `"formatters"` don't ask.

## Settings

`"formatters": false` turns this off. To choose the command for an extension, give a map; `{file}` is the file's path (added at the end if left out), and `false` turns one extension off:

```json
{"formatters": {".py": "black -q {file}", ".md": false}}
```
