"""Snapshots: undo, redo and roll back everything a turn changed, in every file.

The core's /undo restores one file the model wrote. This keeps a shadow git repository of the project in
.nanocode/snapshots (its own GIT_DIR and index; the work tree is the project folder), takes a snapshot when a
turn starts and another when it ends, and remembers which files changed in between. That catches changes made
by commands too (a codemod, `npm install` rewriting a lockfile), not just the edit tools. The user's own .git
is never touched, and .gitignore is respected, so build output and dependencies aren't copied.

Commands: /undo (the last turn's changes), /redo, /rollback (list checkpoints, or restore one). Without git
installed, /undo falls back to the core's per-file undo.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import nanocode as nc
from nanokit import command, on

MAX_FILES = 20_000      # bigger projects aren't snapshotted (the first snapshot copies every file); configurable
MAX_FILE_SIZE = 20 * 1024 * 1024   # bigger files aren't copied into snapshots (and never deleted by undo)
MAX_CHECKPOINTS = 200
MAX_SKIPPED = 2000      # paths remembered as "there, but not in the snapshot"; more means "don't know"
GIT_TIMEOUT = 60
_lock = threading.Lock()  # one git command at a time: tabs share the shadow index
_too_big = {}  # work tree -> (file count, limit), for projects skipped because they're too big
OPEN = {}      # id(tab) -> the tab's running turn: {"id", "tab", "start", "concurrent": set of turn ids}
RECENT = []    # this session's checkpoints, every tab's (to find turns that ran at the same time)
_turn_ids = iter(range(1, 1 << 62))


class SnapshotError(Exception):
    pass


def enabled():
    return nc.st.config.get("snapshots", True) is not False and shutil.which("git") is not None


def repo_dir():
    return nc.project_dir() / "snapshots"


def log_file():
    return repo_dir() / "nanocode-checkpoints.json"


class _locked:
    """One snapshot operation at a time: across this process's tabs (threads) and across nanocode processes
    in the same project (a file lock, like nanocode's config_lock). Holding it, any index.lock left in the
    shadow repository is stale (a git that was killed), so it's removed."""
    def __enter__(self):
        _lock.acquire()
        self.f = None
        try:
            repo_dir().mkdir(parents=True, exist_ok=True)
            self.f = open(repo_dir() / "nanocode.lock", "a+")
            if os.name == "nt":
                import msvcrt
                for _ in range(600):
                    try:
                        msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                        self.locked = True
                        break
                    except OSError:
                        time.sleep(0.1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        except (OSError, ImportError):
            pass
        _remove_stale_lock()
        return self

    def __exit__(self, *exc):
        try:
            if self.f is not None:
                if os.name == "nt" and getattr(self, "locked", False):
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                self.f.close()
        except OSError:
            pass
        finally:
            _lock.release()


def _remove_stale_lock():
    try:
        (repo_dir() / "index.lock").unlink()
    except OSError:
        pass


def _git(*args, input=None, timeout=GIT_TIMEOUT):
    """Runs git on the shadow repository. GIT_DIR and GIT_WORK_TREE point it there, so the project's own
    repository (if any) is invisible to it; --literal-pathspecs keeps file names with * or : literal."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_DIR=str(repo_dir()), GIT_WORK_TREE=os.getcwd(), GIT_TERMINAL_PROMPT="0")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(["git", "--literal-pathspecs", *args], input=input, capture_output=True, env=env,
                              cwd=os.getcwd(), timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        _remove_stale_lock()                     # the killed git can't remove its own lock
        raise SnapshotError(f"git {args[0]} took over {timeout}s")
    except OSError as e:
        raise SnapshotError(str(e))
    if done.returncode != 0:
        raise SnapshotError(done.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return done.stdout.decode("utf-8", errors="surrogateescape")


# Never part of a checkpoint, even in a project without a .gitignore: caches, installed packages and build output
# (big, rebuilt by tools, and noise in "/undo reverted …").
BASE_EXCLUDE = "".join(f"{p}\n" for p in (
    "/.nanocode/", "__pycache__/", "*.pyc", "*.pyo", ".mypy_cache/", ".pytest_cache/", ".ruff_cache/", ".tox/",
    "node_modules/", ".venv/", "venv/", ".next/", ".nuxt/", ".cache/", ".gradle/", ".terraform/", "target/",
    ".DS_Store", ".hg/", ".svn/"))


def _ensure_repo():
    if (repo_dir() / "HEAD").exists():
        return
    repo_dir().mkdir(parents=True, exist_ok=True)
    _git("init", "--quiet")
    for key, value in [("core.autocrlf", "false"), ("core.safecrlf", "false"), ("core.longpaths", "true"),
                       ("core.untrackedCache", "true"), ("gc.auto", "0"), ("user.name", "nanocode"),
                       ("user.email", "nanocode@localhost"), ("commit.gpgsign", "false")]:
        _git("config", key, value)          # byte-for-byte copies; never garbage-collect old snapshots
    exclude = repo_dir() / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(BASE_EXCLUDE, encoding="utf-8")


def _lines(text):
    return [p for p in text.split("\0") if p]


def _chunks(files, size=200):
    for i in range(0, len(files), size):         # chunks: Windows limits a command line to 32k characters
        yield files[i:i + size]


def _escape(name):
    """A path as an anchored .gitignore line that matches only it."""
    return "/" + "".join("\\" + c if c in "\\*?[]!# " else c for c in name)


def _skip_big_files():
    """Keeps files over the size limit out of snapshots: they're listed in the shadow info/exclude (rewritten
    only when the list changes), and dropped from its index if they were in it."""
    limit = nc.st.config.get("snapshots_max_file_size", MAX_FILE_SIZE)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        limit = MAX_FILE_SIZE
    big_file = repo_dir() / "nanocode-big.json"
    try:
        before = json.loads(big_file.read_text(encoding="utf-8"))
        before = set(before) if isinstance(before, list) else set()
    except (OSError, ValueError):
        before = set()
    modified = set(_lines(_git("ls-files", "-z", "--modified")))
    candidates = set(_lines(_git("ls-files", "-z", "--others", "--exclude-standard"))) | modified | before
    root = Path(os.getcwd())
    big = set()
    for name in candidates:
        try:
            if "\n" not in name and os.lstat(root / name).st_size > limit:
                big.add(name)
        except OSError:
            pass
    if big != before:
        (repo_dir() / "info").mkdir(parents=True, exist_ok=True)
        (repo_dir() / "info" / "exclude").write_text(BASE_EXCLUDE + "".join(_escape(n) + "\n" for n in sorted(big)),
                                                     encoding="utf-8")
        big_file.write_text(json.dumps(sorted(big)), encoding="utf-8")
    tracked = sorted(big & modified)
    for chunk in _chunks(tracked):
        _git("rm", "--cached", "--quiet", "--ignore-unmatch", "--", *chunk)


def _snapshot():
    """Snapshots the work tree: (tree hash, skipped), or None when this project isn't snapshotted. `skipped` are
    the paths that exist but aren't in the tree (ignored, too big, unreadable; a folder ends in "/"), or None
    when there are too many to list: such a path is never deleted by undo, since it may have been there."""
    cwd = os.getcwd()
    if cwd in _too_big or Path(cwd) == Path.home() or Path(cwd).parent == Path(cwd):
        return None
    with _locked():
        _ensure_repo()
        if not (repo_dir() / "index").exists():          # first snapshot here: is the project small enough?
            limit = nc.st.config.get("snapshots_max_files", MAX_FILES)
            count = len(_lines(_git("ls-files", "-z", "--others", "--exclude-standard")))
            if isinstance(limit, int) and count > limit:
                _too_big[cwd] = (count, limit)
                return None
        _skip_big_files()
        _git("add", "--all", "--ignore-errors", ".")
        tree = _git("write-tree").strip()
        skipped = set(_lines(_git("ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")))
        skipped |= set(_lines(_git("ls-files", "-z", "--others", "--exclude-standard")))   # git couldn't add
        return tree, (sorted(skipped) if len(skipped) <= MAX_SKIPPED else None)


def _take():
    """Just the tree hash of a new snapshot (or None)."""
    snap = _snapshot()
    return snap[0] if snap else None


def _covered(name, skipped):
    """True when `name` may have existed though it isn't in the snapshot."""
    if skipped is None:
        return True
    return name in skipped or any(name.startswith(p) for p in skipped if p.endswith("/"))


def _keep(tree, message):
    """Commits a tree onto the shadow branch, so git never treats it as garbage."""
    with _locked():
        try:
            parent = ["-p", _git("rev-parse", "--verify", "-q", "HEAD").strip()]
        except SnapshotError:
            parent = []
        commit = _git("commit-tree", tree, *parent, "-m", message or "snapshot").strip()
        _git("update-ref", "HEAD", commit)


def _changed(tree_a, tree_b):
    if tree_a == tree_b:
        return []
    with _locked():
        return _lines(_git("diff", "--name-only", "--no-renames", "-z", tree_a, tree_b))


def _present(tree, files):
    """The ones of these files that are in the tree. Call with the lock held."""
    present = set()
    for chunk in _chunks(files):
        present.update(_lines(_git("ls-tree", "-r", "-z", "--name-only", tree, "--", *chunk)))
    return present


def _in_tree(tree, files):
    with _locked():
        return _present(tree, files)


def _restore(tree, files, deletable=()):
    """Puts the files back as they are in `tree`. A file that isn't in the tree is deleted only when it's in
    `deletable` (it provably didn't exist then); any other is left alone. Returns the files left alone."""
    deletable = set(deletable)
    with _locked():
        present = _present(tree, files)
        for chunk in _chunks([f for f in files if f in present]):
            _git("checkout", tree, "--", *chunk)
    root = Path(os.getcwd())
    kept = []
    for name in files:
        if name in present:
            continue
        path = root / name
        if name not in deletable:
            if path.exists() or path.is_symlink():
                kept.append(name)
            continue
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            parent = path.parent
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()                       # folders the turn made, now empty
                parent = parent.parent
        except OSError:
            pass
    for name in files:
        nc.remember_stamp(root / name)
    return kept


def _load_log():
    try:
        data = json.loads(log_file().read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_log(entries):
    log_file().parent.mkdir(parents=True, exist_ok=True)
    log_file().write_text(json.dumps(entries[-MAX_CHECKPOINTS:], indent=1), encoding="utf-8")


def _record(before, after, message, before_skipped=None, after_skipped=None, turn=None):
    """Saves a checkpoint (a turn that changed files) and returns it, or None if nothing changed. It remembers
    which files the turn created and deleted, counting only files that provably weren't there (not ignored,
    too big or unreadable at the time): only those may be deleted by /undo and /redo."""
    files = _changed(before, after)
    if not files:
        return None
    with _locked():
        in_before, in_after = _present(before, files), _present(after, files)
    created = [f for f in files if f not in in_before and not _covered(f, before_skipped)]
    deleted = [f for f in files if f not in in_after and not _covered(f, after_skipped)]
    _keep(before, f"before: {message}")
    _keep(after, message)
    entry = {"time": time.time(), "message": message, "before": before, "after": after, "files": files,
             "created": created, "deleted": deleted, "before_skipped": before_skipped}
    if turn:
        entry.update(turn=turn["id"], start=turn["start"], concurrent=sorted(turn["concurrent"]))
    with _locked():
        _save_log(_load_log() + [entry])
    return entry


def _state(tab=None):
    tab = tab or nc.st.current_tab()
    if not isinstance(getattr(tab, "snapshots", None), dict):
        tab.snapshots = {"pending": None, "undo": [], "redo": []}
    return tab.snapshots


async def _run(fn, *args):
    return await asyncio.to_thread(fn, *args)


def _open_turn(tab):
    """Notes that a turn started in this tab, and which other tabs' turns are running alongside it."""
    for key in [k for k, t in OPEN.items() if not any(t["tab"] is x for x in nc.st.tabs)]:
        OPEN.pop(key, None)                                  # closed tabs
    turn = {"id": next(_turn_ids), "tab": tab, "start": time.time(), "concurrent": set()}
    for other in OPEN.values():
        if other["tab"] is not tab:
            other["concurrent"].add(turn["id"])
            turn["concurrent"].add(other["id"])
    OPEN[id(tab)] = turn
    return turn


async def _finish(tab, after=None):
    """Ends the tab's open turn: records what changed since its snapshot (also for a turn that was stopped,
    which doesn't get a turn_end). `after` is a snapshot already taken, (tree, skipped)."""
    state = _state(tab)
    pending, state["pending"] = state["pending"], None
    OPEN.pop(id(tab), None)
    if not pending:
        return None
    before, message = pending[0], pending[1]
    before_skipped = pending[2] if len(pending) > 2 else None
    turn = pending[3] if len(pending) > 3 else None
    after = after or await _run(_snapshot)
    entry = after and await _run(_record, before, after[0], message, before_skipped, after[1], turn)
    if entry:
        state["undo"].append(entry)
        del state["undo"][:-50]
        state["redo"].clear()
        RECENT.append(entry)
        del RECENT[:-200]
    return entry


@on("turn_start")
async def turn_start(text):
    if not enabled():
        return
    tab = nc.st.current_tab()
    known = os.getcwd() in _too_big
    try:
        snap = await _run(_snapshot)
        if not known and os.getcwd() in _too_big:           # said once per project and session
            count, limit = _too_big[os.getcwd()]
            nc.out(f"  {nc.fg('muted')}⎿  snapshots are off here: {count:,} files is more than "
                   f"snapshots_max_files ({limit:,}){nc.RESET}")
        if _state(tab)["pending"]:
            await _finish(tab, snap)
        if snap:
            _state(tab)["pending"] = (snap[0], " ".join(text.split())[:80], snap[1], _open_turn(tab))
    except SnapshotError as e:
        nc.log_error(e)


@on("turn_end")
async def turn_end(text, error):
    if not enabled():
        return
    try:
        entry = await _finish(nc.st.current_tab())
    except SnapshotError as e:
        nc.log_error(e)
        return
    if entry and not nc.st.headless:
        names = ", ".join(entry["files"][:3]) + (f" +{len(entry['files']) - 3}" if len(entry["files"]) > 3 else "")
        nc.out(f"  {nc.fg('faint')}⎿  checkpoint: {names} · /undo reverts{nc.RESET}")


async def _confirm(what, preview, dangerous=True):
    """Asks every time, even with /auto (and says no when there's nobody to ask)."""
    if nc.CONFIRMED.get():                      # already asked in the person's app (the web page)
        return True
    if nc.st.headless:
        nc.out(f"  {nc.DIM}⎿  {what} not done: it needs your OK, and nobody can answer here{nc.RESET}")
        return False
    if nc.st.permission_lock is None:
        nc.st.permission_lock = asyncio.Lock()
    async with nc.st.permission_lock:
        return await nc.ask_permission(what, preview, dangerous)


def _others(entry):
    """For a checkpoint whose turn ran while other tabs' turns did: (files another of those turns recorded as
    its own, whether one of them is still running)."""
    concurrent = set(entry.get("concurrent") or ())
    if not concurrent:
        return set(), False
    theirs = set()
    for other in RECENT:
        if other is not entry and (other.get("turn") in concurrent or entry.get("turn") in (other.get("concurrent") or ())):
            theirs.update(other.get("files") or ())
    running = any(t["id"] in concurrent for t in OPEN.values())
    return theirs & set(entry["files"]), running


def _tell_model(note):
    if nc.st.backend != "sdk":
        nc.st.messages.append({"role": "user", "content": f"[{note} Read files again before editing them.]"})
        nc.st.messages.append({"role": "assistant", "content": "Noted."})


def _names(files, limit=4):
    return ", ".join(files[:limit]) + (f" and {len(files) - limit} more" if len(files) > limit else "")


def _drop_core_undo(files):
    """The core's per-file undo entries for these files are stale once the whole turn is undone."""
    root = Path(os.getcwd())
    gone = {os.path.abspath(root / f) for f in files}
    nc.st.undo[:] = [e for e in nc.st.undo if os.path.abspath(e["path"]) not in gone]


@command("/undo", "undo the last turn's file changes (all files; /redo puts them back)")
async def undo(arg):
    if not enabled():
        nc.cmd_undo()
        return
    tab = nc.st.current_tab()
    state = _state(tab)
    try:
        await _finish(tab)
        if not state["undo"]:
            if nc.st.undo:                         # changes from before snapshots were on (or a skipped project)
                nc.cmd_undo()
            else:
                nc.out(f"{nc.DIM}nothing to undo{nc.RESET} {nc.fg('muted')}(/rollback lists older checkpoints)"
                       f"{nc.RESET}\n")
            return
        entry = state["undo"][-1]
        files = list(entry["files"])
        overlap, running = _others(entry)
        if entry.get("concurrent"):                # all tabs share one snapshot of the project
            nc.out(f"  {nc.fg('warning')}⎿  another tab's turn ran at the same time{' and is still running' * running}"
                   f": this checkpoint may include its changes{nc.RESET}")
        if overlap:
            names = sorted(overlap)
            if not await _confirm("undo", f"also revert {len(names)} file{'s' * (len(names) != 1)} another tab's "
                                          f"turn changed at the same time: {_names(names, 3)}", False):
                files = [f for f in files if f not in overlap]
                nc.out(f"  {nc.DIM}⎿  leaving {_names(names, 3)} as they are{nc.RESET}")
        state["undo"].pop()
        if not files:
            nc.out(f"{nc.DIM}nothing undone: every file in that checkpoint is another tab's too{nc.RESET}\n")
            return
        now = await _run(_snapshot) or (entry["after"], None)
        kept = await _run(_restore, entry["before"], files, entry.get("created") or ())
    except SnapshotError as e:
        nc.out(f"{nc.RED}⏺ undo failed: {e}{nc.RESET}\n")
        return
    deleted = [f for f in (entry.get("deleted") or ()) if f in files and not _covered(f, now[1])]
    state["redo"].append(dict(entry, after=now[0], files=files, deleted=deleted))
    _drop_core_undo(files)
    note = f" {nc.fg('muted')}(kept {_names(kept, 3)}: it may have been there before){nc.RESET}" if kept else ""
    nc.out(f"{nc.GREEN}⏺ Undone:{nc.RESET} {_names(files)} "
           f"{nc.DIM}(from “{entry['message'][:50]}”; /redo puts it back){nc.RESET}{note}\n")
    _tell_model(f"I undid all your file changes from that turn ({_names(files, 10)}).")


@command("/redo", "put back what /undo reverted")
async def redo(arg):
    state = _state()
    if not enabled() or not state["redo"]:
        nc.out(f"{nc.DIM}nothing to redo{nc.RESET}\n")
        return
    entry = state["redo"].pop()
    try:
        await _run(_restore, entry["after"], entry["files"], entry.get("deleted") or ())
    except SnapshotError as e:
        nc.out(f"{nc.RED}⏺ redo failed: {e}{nc.RESET}\n")
        return
    state["undo"].append(entry)
    nc.out(f"{nc.GREEN}⏺ Redone:{nc.RESET} {_names(entry['files'])}\n")
    _tell_model(f"I re-applied your file changes ({_names(entry['files'], 10)}) that I had undone.")


def _when(seconds):
    return time.strftime("%b %d %H:%M", time.localtime(seconds))


@command("/rollback", "checkpoints: list them, or /rollback <n> restores the project to before turn n")
async def rollback(arg):
    if not enabled():
        why = "they're off (\"snapshots\": false)" if shutil.which("git") else "they need git, which isn't installed"
        nc.out(f"{nc.DIM}no checkpoints: {why}{nc.RESET}\n")
        return
    entries = list(reversed(_load_log()))           # newest first: /rollback 1 is the last turn
    if not arg:
        if not entries:
            nc.out(f"{nc.DIM}no checkpoints yet: one is saved for each turn that changes files{nc.RESET}\n")
            return
        lines = [f"{nc.BOLD}Checkpoints{nc.RESET} {nc.fg('muted')}(newest first){nc.RESET}"]
        for i, e in enumerate(entries[:30], 1):
            lines.append(f"  {nc.CYAN}{i:>3}{nc.RESET}  {nc.fg('muted')}{_when(e['time'])}{nc.RESET}  "
                         f"{e['message'][:50]}  {nc.fg('faint')}{len(e['files'])} file{'s' * (len(e['files']) != 1)}: "
                         f"{_names(e['files'], 3)}{nc.RESET}")
        lines.append(f"\n  {nc.DIM}/rollback <n> restores every file to how it was before that turn "
                     f"(/undo reverts the rollback){nc.RESET}")
        nc.out("\n".join(lines) + "\n")
        return
    if not arg.isdigit() or not 1 <= int(arg) <= len(entries):
        nc.out(f"{nc.RED}⏺ no checkpoint {arg}{nc.RESET} {nc.DIM}(/rollback lists them){nc.RESET}\n")
        return
    target = entries[int(arg) - 1]
    tab = nc.st.current_tab()
    try:
        await _finish(tab)
        snap = await _run(_snapshot)
        now, now_skipped = snap if snap else (None, None)
        files = await _run(_changed, now, target["before"]) if now else []
        if not files:
            nc.out(f"{nc.DIM}nothing to restore: the files already match that checkpoint{nc.RESET}\n")
            return
        # A file missing from that checkpoint is deleted only if it provably wasn't there (not ignored, too
        # big or unreadable then); older checkpoints don't know, so they delete nothing.
        target_skipped = target.get("before_skipped")
        present = await _run(_in_tree, target["before"], files)
        deletes = [f for f in files if f not in present and not _covered(f, target_skipped)]
        what = (f"restore {len(files)} file{'s' * (len(files) != 1)} to before “{target['message'][:60]}” "
                f"({_when(target['time'])}): {_names(files, 3)}")
        if deletes:
            what += f"; deletes {_names(deletes, 3)}"
        if not await _confirm("rollback", what, dangerous=bool(deletes)):   # always asks, even with /auto
            return
        await _run(_restore, target["before"], files, deletes)
        entry = await _run(_record, now, target["before"], f"rollback to “{target['message'][:60]}”", now_skipped,
                           target_skipped)
    except SnapshotError as e:
        nc.out(f"{nc.RED}⏺ rollback failed: {e}{nc.RESET}\n")
        return
    if entry:
        _state(tab)["undo"].append(entry)
    _drop_core_undo(files)
    nc.out(f"{nc.GREEN}⏺ Rolled back{nc.RESET} {len(files)} file{'s' * (len(files) != 1)} to before “{target['message'][:50]}” "
           f"{nc.DIM}(/undo reverts this){nc.RESET}\n")
    _tell_model(f"I rolled the project back to before an earlier turn (“{target['message'][:60]}”), which "
                f"restored {_names(files, 10)}.")


nc.COMMANDS["/undo"] = "undo the last turn's file changes, in every file (/redo puts them back)"
