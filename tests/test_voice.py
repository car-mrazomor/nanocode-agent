import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import voice  # noqa: E402
from test_nanocode import mock_http  # noqa: E402


def only(*names):
    return lambda name: f"/usr/bin/{name}" if name in names else None


def test_speech_text_skips_code_and_markup():
    text = voice.speech_text("## Done\n**Fixed** the bug in `app.py`, see [the PR](https://x.y/1).\n"
                             "```py\nprint(1)\n```\n- one\n- two https://a.b/c")
    assert text == "Done Fixed the bug in app.py, see the PR. (code) one two a link"
    long = voice.speech_text("This is a sentence. " * 200, limit=100)
    assert len(long) < 140 and long.endswith("…and more on screen.")


def test_choosing_a_voice(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(voice.os, "name", "posix")
    monkeypatch.setattr(voice, "which", only())
    assert voice.tts_engine() is None and voice.speak("hello") == voice.NO_TTS
    monkeypatch.setattr(voice, "which", only("espeak-ng"))
    assert voice.tts_engine() == "espeak-ng"
    monkeypatch.setattr(voice, "which", only("espeak-ng", "edge-tts", "mpv"))
    assert voice.tts_engine() == "edge-tts"
    nc.st.config["voice"] = {"tts": "elevenlabs"}
    assert voice.tts_engine() is None                    # no key
    nc.st.config["voice"] = {"tts": "elevenlabs", "elevenlabs_key": "k"}
    assert voice.tts_engine() == "elevenlabs"
    monkeypatch.setattr(sys, "platform", "darwin")
    nc.st.config["voice"] = {}
    monkeypatch.setattr(voice, "which", only("say"))
    assert voice.tts_engine() == "say"


def test_speech_steps(tmp_path, monkeypatch):
    steps = voice.speech_steps("say", "hi -there", tmp_path)
    assert steps[0][0][:2] == ["say", "-f"] and (tmp_path / "speech.txt").read_text() == "hi -there"
    win = voice.speech_steps("windows", "hi", tmp_path)
    assert "$env:NANOCODE_SPEECH" in win[0][0][-1] and win[0][1]["NANOCODE_SPEECH"].endswith("speech.txt")
    monkeypatch.setattr(voice, "which", only("mpv"))
    monkeypatch.setattr(sys, "platform", "linux")
    edge = voice.speech_steps("edge-tts", "hi", tmp_path)
    assert edge[0][0][0] == "edge-tts" and edge[1][0][0] == "mpv"


def test_speak_runs_the_voice(monkeypatch):
    ran = []

    class Proc:
        def __init__(self, argv, **kw):
            ran.append((argv, Path(argv[-1]).read_text() if argv[-2] == "-f" else None))

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(voice, "tts_engine", lambda: "espeak-ng")
    monkeypatch.setattr(voice.subprocess, "Popen", Proc)
    assert voice.speak("**Hello** there", wait=True) is None
    assert ran == [(["espeak-ng", "-f", ran[0][0][-1]], "Hello there")]


def test_recorders(monkeypatch, tmp_path):
    monkeypatch.setattr(voice.os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(voice, "which", only())
    assert voice.recorder(tmp_path / "a.wav") is None
    monkeypatch.setattr(voice, "which", only("arecord"))
    assert voice.recorder(tmp_path / "a.wav")[0][0] == "arecord"
    monkeypatch.setattr(voice, "which", only("ffmpeg", "pactl"))
    argv, how = voice.recorder(tmp_path / "a.wav", 30)
    assert how == "q" and argv[argv.index("-f") + 1] == "pulse" and "30" in argv
    monkeypatch.setattr(voice, "which", only("sox", "ffmpeg"))
    argv, how = voice.recorder(tmp_path / "a.wav")
    assert argv[0].endswith("sox") and "silence" in argv and how == "signal"


def test_transcribe_with_a_local_whisper(monkeypatch, tmp_path):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OGG")

    def fake_run(argv, timeout=600, env=None):
        assert argv[0] == "whisper" and argv[1] == str(audio)
        out = Path(argv[argv.index("--output_dir") + 1]) / "voice.txt"
        out.write_text(" hello\n world ")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(voice, "which", only("whisper"))
    monkeypatch.setattr(voice, "run", fake_run)
    assert asyncio.run(voice.transcribe(audio)) == "hello world"


def test_transcribe_with_an_api(monkeypatch, tmp_path):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OGG")
    monkeypatch.setattr(voice, "which", only())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(voice.VoiceError, match="no speech-to-text"):
        asyncio.run(voice.transcribe(audio))
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "  turn on the lights "})

    mock_http(monkeypatch, handler)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-1")
    assert asyncio.run(voice.transcribe(audio)) == "turn on the lights"
    assert str(seen[0].url) == voice.OPENAI_STT_URL and seen[0].headers["authorization"] == "Bearer sk-1"
    assert b'name="model"' in seen[0].content and b"whisper-1" in seen[0].content and b"OGG" in seen[0].content
    nc.st.config["voice"] = {"stt_url": "http://localhost:9000/v1/audio/transcriptions", "stt": "api"}
    asyncio.run(voice.transcribe(audio))
    assert seen[-1].url.host == "localhost" and "authorization" not in seen[-1].headers


def test_voice_command_and_spoken_replies(sandbox, monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", lambda text, wait=False: spoken.append(text))
    asyncio.run(voice.cmd_voice("on"))
    assert nc.read_config_file()["voice"]["speak"] is True
    nc.st.headless = False
    nc.st.last_reply = "old"
    voice.before_turn("hi")
    nc.st.last_reply = "A new answer."
    voice.after_turn("hi", None)
    voice.before_turn("again")
    voice.after_turn("again", None)            # nothing new to read
    assert spoken == ["A new answer."]
    asyncio.run(voice.cmd_voice("off"))
    assert nc.read_config_file()["voice"]["speak"] is False


def test_listen_needs_the_app(sandbox, capsys):
    nc.st.headless = True
    asyncio.run(voice.listen())
    assert "needs the app" in capsys.readouterr().err


def test_record_stops_when_asked(monkeypatch, tmp_path):
    path = tmp_path / "speech.wav"
    script = f"import time; open({str(path)!r}, 'wb').write(b'0' * 2000); time.sleep(30)"
    monkeypatch.setattr(voice, "recorder", lambda p, seconds=60: ([sys.executable, "-c", script], "signal"))

    async def main():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.5, stop.set)
        return await voice.record(path, stop)

    assert asyncio.run(main()) == path and path.stat().st_size == 2000
