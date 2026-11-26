from pathlib import Path

from test_nanocode import nc, run


def test_init_asks_the_model_to_write_agents_md(sandbox, monkeypatch):
    sent = []

    async def fake_send(text, display=None, images=None):
        sent.append((text, display))
    monkeypatch.setattr(nc, "send", fake_send)
    run(nc.run_input("/init"))
    text, display = sent[-1]
    assert display == "/init" and "create AGENTS.md" in text and "single test" in text
    Path("AGENTS.md").write_text("# Notes\n")
    run(nc.run_input("/init mention the docs folder"))
    text, display = sent[-1]
    assert "already exists" in text and "mention the docs folder" in text
    assert display == "/init mention the docs folder"
