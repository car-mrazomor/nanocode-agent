# Undo, redo and checkpoints

nanocode takes a snapshot of your project when a turn starts and another when it ends. If the turn changed any files, that's a checkpoint. It catches every change: the model's edits, and also what its commands did (a codemod, a formatter, a rewritten lockfile).

| Command | What it does |
|---|---|
| `/undo` | Reverts every file the last turn changed, in one go |
| `/redo` | Puts back what `/undo` reverted |
| `/rollback` | Lists checkpoints: time, your message, the files changed |
| `/rollback <n>` | Restores the project to how it was before turn `n` (always asks first, even with `/auto`; `/undo` reverts it) |

The model is told what was undone, so it reads the files again before its next edit.

## How it works

Snapshots live in a separate git repository in `.nanocode/snapshots/`. Your own `.git` is never touched, and your project doesn't need to be a git repository. Files your `.gitignore` excludes (dependencies, build output) aren't copied. Snapshots are incremental, so they're quick even on big projects.

Undo, redo and rollback only delete a file when it provably didn't exist at the time they go back to: one the turn created. A file that wasn't in the snapshot because it was ignored then, too big, or unreadable is left alone (and named), even if the turn's changes to `.gitignore` make it look new.

Files over 20 MB aren't copied into snapshots (`snapshots_max_file_size` changes the limit). Several nanocode windows on the same project take turns with the snapshot repository, and a lock left by a git that timed out is cleaned up.

All tabs share one snapshot of the project. If another tab's turn ran at the same time as the one you `/undo`, nanocode warns that the checkpoint may include its changes, and asks before reverting files that the other turn recorded as its own.

Projects with more than 20,000 files aren't snapshotted (the first snapshot copies every file). Change the limit with `snapshots_max_files`.

Without git installed, `/undo` falls back to reverting the model's file edits one at a time.

## Settings

| Key | Default | What it does |
|---|---|---|
| `snapshots` | `true` | `false` turns snapshots off (`/undo` then reverts one edit at a time) |
| `snapshots_max_files` | `20000` | Skip projects with more files than this |
| `snapshots_max_file_size` | `20971520` | Don't snapshot files bigger than this many bytes |
