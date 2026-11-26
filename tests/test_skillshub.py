import io
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import skillshub  # noqa: E402
from test_nanocode import mock_http, plain, run  # noqa: E402


def skill_md(name, description="Does a thing"):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\nSteps.\n"


def repo_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, text in files.items():
            z.writestr(f"owner-repo-abc123/{path}", text)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    skillshub.HUB_CACHE.clear()

    async def yes(tool, preview, dangerous):
        return True

    monkeypatch.setattr(nc, "ask_permission", yes)


def test_parse_source():
    assert skillshub.parse_source("owner/repo") == ("owner", "repo", None, "")
    assert skillshub.parse_source("owner/repo/skills/pdf") == ("owner", "repo", None, "skills/pdf")
    assert skillshub.parse_source("https://github.com/o/r/tree/main/skills/pdf") == ("o", "r", "main", "skills/pdf")
    assert skillshub.parse_source("github.com/o/r.git") == ("o", "r", None, "")
    assert skillshub.parse_source("https://github.com/o/r/blob/dev/x/SKILL.md") == ("o", "r", "dev", "x")
    assert skillshub.parse_source("pdf") is None


def test_skills_in_zip_validates_and_skips_unsafe_paths():
    data = repo_zip({"skills/pdf/SKILL.md": skill_md("pdf"), "skills/pdf/scripts/run.py": "print(1)",
                     "skills/pdf/../evil.txt": "x", "skills/docx/SKILL.md": skill_md("docx"), "README.md": "hi"})
    found = skillshub.skills_in_zip(data, "skills/pdf")
    assert [s["name"] for s in found] == ["pdf"]
    assert set(found[0]["files"]) == {"SKILL.md", "scripts/run.py"}
    assert {s["name"] for s in skillshub.skills_in_zip(data)} == {"pdf", "docx"}
    with pytest.raises(skillshub.HubError, match="no SKILL.md"):
        skillshub.skills_in_zip(data, "nothing")
    with pytest.raises(skillshub.HubError, match="name"):
        skillshub.skills_in_zip(repo_zip({"a/SKILL.md": skill_md("Bad Name")}))
    with pytest.raises(skillshub.HubError, match="frontmatter"):
        skillshub.skills_in_zip(repo_zip({"a/SKILL.md": "no header"}))


def test_install_from_a_repo_and_remove(sandbox, monkeypatch):
    data = repo_zip({"skills/pdf/SKILL.md": skill_md("pdf", "Work with PDFs"), "skills/pdf/ref.md": "ref"})
    urls = []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(200, content=data)

    mock_http(monkeypatch, handler)
    run(skillshub.skills_command("install https://github.com/owner/repo/tree/main/skills/pdf"))
    assert urls == ["https://api.github.com/repos/owner/repo/zipball/main"]
    target = nc.home_dir() / "skills" / "pdf"
    assert (target / "SKILL.md").exists() and (target / "ref.md").read_text() == "ref"
    assert "pdf" in nc.list_skills() and "Installed" in plain(nc.st.output)
    run(skillshub.skills_command("install owner/repo/skills/pdf --project"))
    assert (nc.project_dir() / "skills" / "pdf" / "SKILL.md").exists()
    run(skillshub.skills_command("remove pdf --project"))
    assert not (nc.project_dir() / "skills" / "pdf").exists() and target.exists()
    run(skillshub.skills_command("remove pdf"))
    assert not target.exists()
    run(skillshub.skills_command("remove pdf"))
    assert "no installed skill named 'pdf'" in plain(nc.st.output)


def test_install_asks_and_can_be_declined(sandbox, monkeypatch):
    async def no(tool, preview, dangerous):
        return False

    monkeypatch.setattr(nc, "ask_permission", no)
    mock_http(monkeypatch, lambda req: httpx.Response(200, content=repo_zip({"SKILL.md": skill_md("solo")})))
    run(skillshub.skills_command("install owner/solo"))
    assert not (nc.home_dir() / "skills" / "solo").exists()


def test_hub_lists_and_installs_by_name(sandbox, monkeypatch):
    tree = {"tree": [{"path": "skills/pdf/SKILL.md", "type": "blob"}, {"path": "skills/xlsx/SKILL.md", "type": "blob"},
                     {"path": "README.md", "type": "blob"}]}
    data = repo_zip({"skills/pdf/SKILL.md": skill_md("pdf", "Work with PDFs"),
                     "skills/xlsx/SKILL.md": skill_md("xlsx", "Spreadsheets")})

    def handler(req):
        url = str(req.url)
        if "/git/trees/HEAD" in url:
            return httpx.Response(200, json=tree)
        if url.startswith(skillshub.RAW):
            name = url.split("/")[-2]
            return httpx.Response(200, text=skill_md(name, "Work with PDFs" if name == "pdf" else "Spreadsheets"))
        if "/zipball" in url:
            assert "anthropics/skills" in url
            return httpx.Response(200, content=data)
        return httpx.Response(404)

    mock_http(monkeypatch, handler)
    run(skillshub.skills_command("hub pdf"))
    screen = plain(nc.st.output)
    assert "pdf" in screen and "Work with PDFs" in screen and "Spreadsheets" not in screen
    run(skillshub.skills_command("install xlsx"))
    assert (nc.home_dir() / "skills" / "xlsx" / "SKILL.md").exists()
    assert not (nc.home_dir() / "skills" / "pdf").exists()
    run(skillshub.skills_command("install nope"))
    assert "no skill named 'nope'" in plain(nc.st.output)


def test_errors_and_plain_listing(sandbox, monkeypatch):
    mock_http(monkeypatch, lambda req: httpx.Response(404))
    run(skillshub.skills_command("install owner/missing"))
    assert "not found on GitHub" in plain(nc.st.output)
    run(nc.run_input("/skills"))
    assert "Skills" in plain(nc.st.output) and "/skills hub" in plain(nc.st.output)


def test_download_is_capped_while_streaming(sandbox, monkeypatch):
    monkeypatch.setattr(skillshub, "MAX_ZIP", 1000)
    sent, mode = [], ["stream"]

    async def body():
        for _ in range(100):
            sent.append(1)
            yield b"x" * 100

    def handler(req):
        if mode[0] == "stream":
            return httpx.Response(200, content=body())
        if mode[0] == "header":
            return httpx.Response(200, content=b"x", headers={"Content-Length": "5000"})
        return httpx.Response(200, content=b"x" * 500)

    mock_http(monkeypatch, handler)
    with pytest.raises(skillshub.HubError, match="too big"):
        run(skillshub.download("o", "r"))
    assert len(sent) < 20                                        # it stopped early
    mode[0] = "header"
    with pytest.raises(skillshub.HubError, match="too big"):
        run(skillshub.download("o", "r"))
    mode[0] = "small"
    assert run(skillshub.download("o", "r")) == b"x" * 500
