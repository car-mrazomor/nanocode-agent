import sys
from pathlib import Path

from test_nanocode import call, nc, plain
from nanokit import formatters

# A stand-in formatter: puts spaces around "=" (like black would), so the tests don't need black installed.
FAKE = "import sys, re; p = sys.argv[-1]; s = open(p).read(); open(p, 'w').write(re.sub(r'\\s*=\\s*', ' = ', s))"


def fake_which(installed):
    return lambda name, path=None: f"/bin/{name}" if name in installed and path is None else None


def test_formats_after_write_and_edit(sandbox):
    script = sandbox / "fmt.py"
    script.write_text(FAKE)
    nc.st.config["formatters"] = {".py": [sys.executable, str(script)]}
    text, err = call("write", path="a.py", content="x=1\n")
    assert not err and "formatted with" in text
    assert Path("a.py").read_text() == "x = 1\n"
    text, err = call("edit", path="a.py", old="x = 1", new="y=2")   # no stale-file error: its stamp was updated
    assert not err and Path("a.py").read_text() == "y = 2\n"
    text, err = call("write", path="b.py", content="ok = 1\n")       # already formatted: no note
    assert "formatted" not in text
    text, err = call("write", path="c.txt", content="a=b\n")         # no formatter for .txt
    assert Path("c.txt").read_text() == "a=b\n"


def test_off_and_failing(sandbox):
    nc.st.config["formatters"] = False
    assert formatters.formatter_for("a.py") is None
    nc.st.config["formatters"] = {".py": [sys.executable, "-c", "import sys; sys.exit('bad syntax')", "{file}"]}
    text, err = call("write", path="a.py", content="x=1\n")
    assert not err and "failed" in text and "bad syntax" in text
    nc.st.config["formatters"] = {".py": "no-such-formatter-xyz {file}"}
    text, _ = call("write", path="a.py", content="x=2\n")
    assert "isn't installed" in text and "failed" in plain(nc.st.output)


def test_detection(sandbox, monkeypatch):
    monkeypatch.setattr(formatters.shutil, "which", fake_which({"ruff", "black", "prettier", "gofmt", "rustfmt"}))
    assert formatters.formatter_for("a.py") is None                  # the project doesn't use a formatter
    Path("pyproject.toml").write_text("[tool.black]\nline-length = 100\n")
    assert formatters.formatter_for("a.py")[0] == "black"
    Path("ruff.toml").write_text("")
    assert formatters.formatter_for("src/a.py")[0] == "ruff"
    assert formatters.formatter_for("a.ts") is None
    Path("package.json").write_text('{"devDependencies": {"prettier": "^3"}}')
    assert formatters.formatter_for("web/a.ts")[0] == "prettier"
    assert formatters.formatter_for("main.go")[0] == "gofmt"
    Path("Cargo.toml").write_text('[package]\nedition = "2021"\n')
    assert formatters.formatter_for("lib.rs") == ("rustfmt", ["/bin/rustfmt", "--edition", "2021"])
    nc.st.config["formatters"] = {".go": False}
    assert formatters.formatter_for("main.go") is None
    monkeypatch.setattr(formatters.shutil, "which", fake_which(set()))
    assert formatters.formatter_for("a.py") is None                  # configured but not installed


def test_project_formatters_need_trust(sandbox, monkeypatch):
    local = sandbox / "node_modules" / ".bin"
    local.mkdir(parents=True)
    tool = local / "prettier"
    tool.write_text("#!/bin/sh\necho ran > ran.txt\n")
    tool.chmod(0o755)
    assert formatters.runs_project_code("black", [str(tool)])
    assert formatters.runs_project_code("prettier", ["/usr/bin/prettier"])
    assert not formatters.runs_project_code("ruff", ["/usr/bin/ruff"])
    monkeypatch.setattr(formatters, "formatter_for", lambda path: ("prettier", [str(tool)]))
    asked = []

    async def no(what, preview, dangerous):
        asked.append(what)
        return False

    monkeypatch.setattr(nc, "ask_permission", no)
    call("write", path="a.js", content="x=1\n")
    call("write", path="b.js", content="x=1\n")
    assert asked == ["project_tools"] and not Path("ran.txt").exists()   # asked once, never ran
    formatters.DECLINED.clear()
    nc.st.headless = True
    call("write", path="c.js", content="x=1\n")
    assert asked == ["project_tools"]                                  # nobody to ask: not trusted
    nc.st.config["trusted_projects"] = [str(Path.cwd().resolve())]
    assert formatters.project_trusted()
