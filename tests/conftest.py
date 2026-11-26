import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    userhome = tmp_path / "userhome"
    project = tmp_path / "project"
    userhome.mkdir()
    project.mkdir()
    monkeypatch.setenv("NANOCODE_HOME", str(tmp_path / "nanocode-home"))
    monkeypatch.setenv("HOME", str(userhome))
    monkeypatch.setenv("USERPROFILE", str(userhome))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(userhome / ".config"))
    for var in ("MODEL", "OPENROUTER_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(project)
    nc.st.__init__()
    nc._config_base = {}
    nc.st.auto_approve = True
    nc.apply_theme({})
    yield project
