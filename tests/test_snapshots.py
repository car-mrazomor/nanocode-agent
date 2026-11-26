import shutil
from pathlib import Path

import pytest

from test_nanocode import nc, plain, run
from nanokit import snapshots

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")


async def turn(text, change):
    """A turn as send() runs it: the hooks around whatever the model changes."""
    await snapshots.turn_start(text)
    change()
    await snapshots.turn_end(text, None)


def edit(files):
    def change():
        for name, content in files.items():
            path = Path(name)
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
    return change


@needs_git
def test_undo_redo_whole_turn(sandbox):
    Path("a.py").write_text("a = 1\n")
    Path("gone.txt").write_text("bye\n")
    Path(".gitignore").write_text("build/\n")
    nc.st.backend = "api"

    async def scenario():
        await turn("refactor", edit({"a.py": "a = 2\n", "new/b.py": "b = 1\n", "gone.txt": None,
                                     "build/out.bin": "x"}))
        assert "checkpoint" in plain(nc.st.output)
        await snapshots.undo("")
        assert Path("a.py").read_text() == "a = 1\n"
        assert not Path("new").exists() and Path("gone.txt").read_text() == "bye\n"
        assert Path("build/out.bin").exists()                  # ignored files are left alone
        assert "I undid all your file changes" in nc.st.messages[-2]["content"]
        await snapshots.redo("")
        assert Path("a.py").read_text() == "a = 2\n" and Path("new/b.py").exists()
        assert not Path("gone.txt").exists()
        await snapshots.redo("")
        assert "nothing to redo" in plain(nc.st.output)
    run(scenario())
    assert not (sandbox / ".git").exists()                     # the project's own repo is never made or touched


@needs_git
def test_caches_and_packages_are_not_part_of_checkpoints(sandbox):
    Path("a.py").write_text("a = 1\n")                        # no .gitignore at all
    nc.st.backend = "api"

    async def scenario():
        await turn("change", edit({"a.py": "a = 2\n", "__pycache__/a.cpython-314.pyc": "x",
                                   "node_modules/pkg/i.js": "y"}))
        assert "__pycache__" not in plain(nc.st.output) and "node_modules" not in plain(nc.st.output)
        await snapshots.undo("")
        assert Path("a.py").read_text() == "a = 1\n" and Path("__pycache__/a.cpython-314.pyc").exists()
    run(scenario())


@needs_git
def test_rollback_lists_and_restores(sandbox, monkeypatch):
    Path("f.txt").write_text("v0\n")
    asked = []

    async def yes(tool, preview, dangerous):
        asked.append((tool, dangerous))
        return True

    monkeypatch.setattr(nc, "ask_permission", yes)               # asks even though the tests run in /auto

    async def scenario():
        await turn("first change", edit({"f.txt": "v1\n"}))
        await turn("second change", edit({"f.txt": "v2\n", "g.txt": "g\n"}))
        await turn("just reading", lambda: None)               # no changes: no checkpoint
        await snapshots.rollback("")
        listing = plain(nc.st.output)
        assert listing.index("second change") < listing.index("first change")
        await snapshots.rollback("9")
        assert "no checkpoint 9" in plain(nc.st.output)
        await snapshots.rollback("2")
        assert Path("f.txt").read_text() == "v0\n" and not Path("g.txt").exists()
        assert asked == [("rollback", True)]                     # it deletes g.txt: flagged as destructive
        await snapshots.undo("")                                # the rollback itself can be undone
        assert Path("f.txt").read_text() == "v2\n" and Path("g.txt").exists()
    run(scenario())


@needs_git
def test_stopped_turn_is_recorded_by_the_next(sandbox):
    Path("x.txt").write_text("1\n")

    async def scenario():
        await snapshots.turn_start("stopped")
        Path("x.txt").write_text("2\n")                        # Esc: no turn_end
        await turn("next", lambda: None)
        await snapshots.undo("")
        assert Path("x.txt").read_text() == "1\n"
    run(scenario())


def test_falls_back_to_core_undo(sandbox, monkeypatch):
    monkeypatch.setattr(snapshots.shutil, "which", lambda name: None)
    nc.st.config["snapshots"] = True
    run(nc.run_tool("write", {"path": "a.txt", "content": "hi\n"}))
    run(nc.run_input("/undo"))
    assert not Path("a.txt").exists() and "Undone" in plain(nc.st.output)
    run(nc.run_input("/rollback"))
    assert "need git" in plain(nc.st.output)


@needs_git
def test_off_and_too_big(sandbox, monkeypatch):
    monkeypatch.setattr(snapshots, "_too_big", {})
    nc.st.config["snapshots"] = False
    run(snapshots.turn_start("x"))
    assert not snapshots.repo_dir().exists()
    nc.st.config.update(snapshots=True, snapshots_max_files=2)
    for i in range(3):
        Path(f"f{i}").write_text("")
    run(snapshots.turn_start("x"))
    assert "snapshots are off here" in plain(nc.st.output)


@pytest.fixture
def fresh_tabs():
    snapshots.OPEN.clear()
    snapshots.RECENT.clear()
    yield
    snapshots.OPEN.clear()
    snapshots.RECENT.clear()


@needs_git
def test_undo_never_deletes_files_that_may_have_existed(sandbox, fresh_tabs):
    Path(".gitignore").write_text("secrets.env\n")
    Path("secrets.env").write_text("API_KEY=precious\n")
    Path("a.py").write_text("x = 1\n")

    async def scenario():
        await turn("t", edit({".gitignore": "", "a.py": "x = 2\n", "new.py": "n\n"}))
        entry = snapshots._state()["undo"][-1]
        assert "secrets.env" in entry["files"] and entry["created"] == ["new.py"]
        await snapshots.undo("")
        assert Path("secrets.env").read_text() == "API_KEY=precious\n"     # was ignored, not absent
        assert not Path("new.py").exists() and Path("a.py").read_text() == "x = 1\n"
        assert "kept secrets.env" in plain(nc.st.output)
        await snapshots.redo("")
        assert Path("new.py").exists() and Path("secrets.env").exists()
    run(scenario())


@needs_git
def test_unreadable_and_big_files_are_skipped(sandbox, fresh_tabs, monkeypatch):
    nc.st.config["snapshots_max_file_size"] = 1000
    Path("big.bin").write_bytes(b"x" * 5000)
    Path("small.txt").write_text("s\n")
    tree, skipped = snapshots._snapshot()
    assert "big.bin" in skipped and snapshots._in_tree(tree, ["big.bin", "small.txt"]) == {"small.txt"}
    Path("big.bin").write_bytes(b"x")                              # small again: snapshotted again
    tree, skipped = snapshots._snapshot()
    assert "big.bin" not in skipped and "big.bin" in snapshots._in_tree(tree, ["big.bin"])

    async def scenario():                                           # created during a turn, but too big
        await turn("t", edit({"huge.bin": "y" * 5000, "small.txt": "t\n"}))
        await snapshots.undo("")
        assert Path("huge.bin").exists() and Path("small.txt").read_text() == "s\n"
    run(scenario())


@needs_git
def test_stale_index_lock_is_removed(sandbox, fresh_tabs, monkeypatch):
    Path("a.txt").write_text("a\n")
    assert snapshots._snapshot()
    lock = snapshots.repo_dir() / "index.lock"
    lock.write_text("")                                             # a git that was killed mid-way
    assert snapshots._snapshot() and not lock.exists()

    def slow(*args, **kwargs):
        lock.write_text("")
        raise snapshots.subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(snapshots.subprocess, "run", slow)
    with pytest.raises(snapshots.SnapshotError, match="took over"):
        snapshots._git("status")
    assert not lock.exists()


@needs_git
def test_undo_asks_before_reverting_another_tabs_files(sandbox, fresh_tabs, monkeypatch):
    Path("mine.txt").write_text("1\n")
    Path("theirs.txt").write_text("1\n")
    nc.st.tabs.append(nc.Tab())
    one, two = nc.st.tabs[0], nc.st.tabs[1]
    asked = []

    async def no(tool, preview, dangerous):
        asked.append(preview)
        return False

    monkeypatch.setattr(nc, "ask_permission", no)

    async def in_tab(tab, coro):
        token = nc.CURRENT_TAB.set(tab)
        try:
            return await coro
        finally:
            nc.CURRENT_TAB.reset(token)

    async def scenario():
        await in_tab(one, snapshots.turn_start("mine"))
        await in_tab(two, snapshots.turn_start("theirs"))          # both turns run at the same time
        Path("theirs.txt").write_text("2\n")
        await in_tab(two, snapshots.turn_end("theirs", None))
        Path("mine.txt").write_text("2\n")
        await in_tab(one, snapshots.turn_end("mine", None))
        await in_tab(one, snapshots.undo(""))
    run(scenario())
    assert asked and "theirs.txt" in asked[0]
    assert Path("mine.txt").read_text() == "1\n" and Path("theirs.txt").read_text() == "2\n"
    assert "another tab's turn ran at the same time" in plain(one.output)
