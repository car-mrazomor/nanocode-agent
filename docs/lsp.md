# Language servers (LSP)

After the model writes or edits a file, nanocode asks a language server for errors and warnings in it, and adds them to what the model sees:

```
LSP (pyright): 2 errors
  app.py:12:5 error: "user" is possibly unbound
  app.py:30:1 error: Import "reqests" could not be resolved
```

So the model fixes type errors and typos in the same turn, before you run anything.

| Language | Servers (the first installed is used) |
|---|---|
| Python | `pyright-langserver` (`npm i -g pyright`), `basedpyright-langserver`, `pylsp` (`pip install python-lsp-server`) |
| JavaScript, TypeScript | `typescript-language-server` (`npm i -g typescript typescript-language-server`) |
| Go | `gopls` |
| Rust | `rust-analyzer` |
| C, C++ | `clangd` |

A server starts the first time a file of its language is changed, and stops when nanocode quits. nanocode waits at most 3 seconds for it after an edit, so a slow server (or one still starting) never holds up a turn; its findings show up on the next edit. A server that crashes, stops reading what nanocode sends, or sends something unreadable is stopped and restarted once, then left off until you restart nanocode.

Some servers run the project's own code: `rust-analyzer` runs build scripts and proc macros, and `typescript-language-server` loads the project's TypeScript and its plugins. These start only in a project you trust: nanocode asks once per project (even with `/auto`, and before the `diagnostics` tool would start one) and remembers it in `"trusted_projects"` in your config. Headless, they start only in a project already listed there.

The model can also call the `diagnostics` tool to check any file, or list what's been found so far.

## Settings

`"lsp": false` turns this off. To choose the server for a language (`python`, `typescript`, `go`, `rust`, `c`):

```json
{"lsp": {"python": "pylsp"}}
```
