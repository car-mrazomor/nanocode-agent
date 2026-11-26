"""The installer downloads exactly the files in MANIFEST: every app file must be listed, and nothing missing."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def app_files():
    files = {"nanocode.py"} | {p.relative_to(ROOT).as_posix() for p in (ROOT / "api").glob("*.py")}
    for p in (ROOT / "nanokit").rglob("*"):
        if p.is_file() and p.suffix in (".py", ".html", ".json", ".js", ".css") and "__pycache__" not in p.parts:
            files.add(p.relative_to(ROOT).as_posix())
    return files


def test_manifest_lists_every_app_file():
    listed = [line.strip() for line in (ROOT / "MANIFEST").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    assert len(listed) == len(set(listed)), "a file is listed twice"
    assert set(listed) == app_files(), "MANIFEST is out of date: list every file under nanokit/ and api/"
    assert all((ROOT / f).is_file() for f in listed)
