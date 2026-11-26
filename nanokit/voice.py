"""Voice: replies read aloud (/voice on) and speech input (/listen, or ctrl+x v).

Speaking uses what the machine has: macOS `say`, Windows' built-in voices (PowerShell), Linux `espeak-ng` or
`spd-say`; the `edge-tts` command if it's installed (nicer voices); or ElevenLabs with a key in the config.
Listening records from the microphone with `sox`, `ffmpeg` or `arecord`, then turns the recording into text with
a local `whisper` / whisper.cpp if installed, else an OpenAI-compatible transcription endpoint.

`speak(text)` and `transcribe(path)` are also used by other features (the gateway transcribes voice messages).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import httpx

import nanocode as nc
from nanokit import command, on

VOICE_MAX_SECONDS = 60
VOICE_MAX_CHARS = 1500  # longer replies are cut: nobody wants to listen to a whole file
OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
ELEVENLABS_VOICE = "21m00Tcm4TlvDq8ikWAM"
AUDIO_TYPES = {".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
               ".m4a": "audio/mp4", ".wav": "audio/wav", ".webm": "audio/webm", ".aac": "audio/aac"}
NO_TTS = ("no text-to-speech tool found: install espeak-ng (Linux) or edge-tts (pip install edge-tts), "
          "or set \"voice\": {\"tts\": \"elevenlabs\", \"elevenlabs_key\": ...} in ~/.nanocode/config.json")
NO_MIC = ("no recorder found: install sox (macOS: brew install sox · Linux: apt install sox · "
          "Windows: winget install ChrisBagwell.SoX) or ffmpeg")
NO_STT = ("no speech-to-text: install whisper (pip install openai-whisper), or set OPENAI_API_KEY, "
          "or \"voice\": {\"stt_url\": ..., \"stt_key\": ...} in ~/.nanocode/config.json")


class VoiceError(Exception):
    pass


def which(name):
    return shutil.which(name)


def run(argv, timeout=600, env=None):
    """Runs a voice tool and waits (one place, so tests can replace it)."""
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, env=env)


def config():
    return nc.st.config.get("voice") or {}


# --- Speaking ---

def speech_text(markdown, limit=None):
    """What's worth saying out loud from a markdown reply: no code, no link targets, no markup."""
    text = re.sub(r"```.*?(```|$)", " (code) ", markdown or "", flags=re.S)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[`*_~|]", "", text)
    text = " ".join(text.split())
    limit = limit or int(config().get("max_chars") or VOICE_MAX_CHARS)
    if len(text) > limit:
        cut = text.rfind(". ", 0, limit)
        text = text[:cut + 1 if cut > limit // 2 else limit] + " …and more on screen."
    return text


def audio_player():
    """A command that plays an mp3 file (the path is added at the end), or None."""
    if sys.platform == "darwin":
        return ["afplay"]
    for argv in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["mpv", "--no-video", "--really-quiet"],
                 ["mpg123", "-q"]):
        if which(argv[0]):
            return argv
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-Command",
                "Add-Type -AssemblyName PresentationCore; $p = New-Object System.Windows.Media.MediaPlayer; "
                "$p.Open([uri]$args[0]); $p.Play(); Start-Sleep -Milliseconds 800; "
                "while (-not $p.NaturalDuration.HasTimeSpan) { Start-Sleep -Milliseconds 100 }; "
                "Start-Sleep -Milliseconds ([int]$p.NaturalDuration.TimeSpan.TotalMilliseconds)"]
    return None


def tts_engine():
    """The voice to use: the config's "tts" if it's available, else the nicest one this machine has."""
    choice = config().get("tts") or "auto"
    if choice == "elevenlabs":
        return "elevenlabs" if (config().get("elevenlabs_key") or os.environ.get("ELEVENLABS_API_KEY")) \
            and audio_player() else None
    if choice == "edge-tts" or (choice == "auto" and which("edge-tts") and audio_player()):
        return "edge-tts" if which("edge-tts") and audio_player() else None
    if choice in ("auto", "system"):
        if sys.platform == "darwin" and which("say"):
            return "say"
        if os.name == "nt":
            return "windows"
        for name in ("espeak-ng", "espeak", "spd-say"):
            if which(name):
                return name
        return None
    if choice == "windows":
        return "windows" if os.name == "nt" else None
    return choice if which(choice) else None


def speech_steps(engine, text, folder):
    """The commands that say `text` with `engine` ([argv], env), run one after another."""
    cfg = config()
    voice = cfg.get("voice")
    text_file = Path(folder) / "speech.txt"
    text_file.write_text(text, encoding="utf-8")
    audio = str(Path(folder) / "speech.mp3")
    if engine == "say":
        return [(["say"] + (["-v", voice] if voice else []) + ["-f", str(text_file)], None)]
    if engine in ("espeak-ng", "espeak"):
        return [([engine] + (["-v", voice] if voice else []) + ["-f", str(text_file)], None)]
    if engine == "spd-say":
        return [(["spd-say", "-w", "--", text], None)]
    if engine == "windows":
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$s.Speak([IO.File]::ReadAllText($env:NANOCODE_SPEECH))")
        return [(["powershell", "-NoProfile", "-Command", script], {**os.environ, "NANOCODE_SPEECH": str(text_file)})]
    if engine == "edge-tts":
        return [(["edge-tts", "--file", str(text_file), "--write-media", audio] + (["--voice", voice] if voice else []),
                 None), (audio_player() + [audio], None)]
    if engine == "elevenlabs":
        return [("elevenlabs", None), (audio_player() + [audio], None)]
    return [([engine, text], None)]


def elevenlabs_audio(text, path):
    cfg = config()
    key = cfg.get("elevenlabs_key") or os.environ.get("ELEVENLABS_API_KEY")
    resp = httpx.post(ELEVENLABS_URL.format(voice=cfg.get("voice_id") or ELEVENLABS_VOICE),
                      headers={"xi-api-key": key, "accept": "audio/mpeg"}, timeout=60,
                      json={"text": text, "model_id": cfg.get("elevenlabs_model") or "eleven_multilingual_v2"})
    if resp.status_code != 200:
        raise VoiceError(f"ElevenLabs: HTTP {resp.status_code} {resp.text[:200]}")
    Path(path).write_bytes(resp.content)


SPEAKING = {"proc": None, "generation": 0}


def stop_speaking():
    SPEAKING["generation"] += 1
    proc = SPEAKING["proc"]
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def _speak(engine, text, generation):
    with tempfile.TemporaryDirectory(prefix="nanocode-voice-") as folder:
        try:
            for argv, env in speech_steps(engine, text, folder):
                if SPEAKING["generation"] != generation:
                    return                           # stopped, or a newer reply is being read
                if argv == "elevenlabs":
                    elevenlabs_audio(text, Path(folder) / "speech.mp3")
                    continue
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, env=env)
                SPEAKING["proc"] = proc
                proc.wait()
        except (OSError, httpx.HTTPError, VoiceError) as e:
            nc.log_error(e)


def speak(text, wait=False):
    """Reads text (markdown is fine) aloud, stopping anything still being read. Returns None, or why it can't."""
    text = speech_text(text)
    if not text:
        return None
    engine = tts_engine()
    if engine is None:
        return NO_TTS
    stop_speaking()
    job = threading.Thread(target=_speak, args=(engine, text, SPEAKING["generation"]), daemon=True)
    job.start()
    if wait:
        job.join()
    return None


# --- Listening ---

def windows_mic():
    """The first microphone ffmpeg sees on Windows (DirectShow needs its name)."""
    try:
        done = run(["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'"([^"]+)"\s*\(audio\)', done.stderr.decode(errors="replace"))
    return found.group(1) if found else None


def recorder(path, seconds=VOICE_MAX_SECONDS):
    """(argv, how to stop it early) that records the microphone into a 16 kHz mono wav file, or None."""
    sox = which("sox") or which("rec")
    if sox:
        device = ["-t", "waveaudio", "default"] if os.name == "nt" else ["-d"]
        # stops by itself after 2 seconds of quiet, once you've started talking
        return ([sox, "-q"] + device + ["-r", "16000", "-c", "1", str(path), "silence", "1", "0.1", "1%",
                                        "1", "2.0", "1%", "trim", "0", str(seconds)], "signal")
    if which("ffmpeg"):
        if sys.platform == "darwin":
            source = ["-f", "avfoundation", "-i", ":0"]
        elif os.name == "nt":
            mic = config().get("mic") or windows_mic()
            if not mic:
                return None
            source = ["-f", "dshow", "-i", f"audio={mic}"]
        else:
            source = ["-f", "pulse", "-i", "default"] if which("pactl") else ["-f", "alsa", "-i", "default"]
        return (["ffmpeg", "-hide_banner", "-loglevel", "error"] + source +
                ["-t", str(seconds), "-ac", "1", "-ar", "16000", "-y", str(path)], "q")
    if which("arecord"):
        return (["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", str(seconds), str(path)], "signal")
    return None


async def record(path, stop, seconds=VOICE_MAX_SECONDS):
    """Records until `stop` (an asyncio.Event) is set, the recorder stops by itself, or `seconds` pass."""
    found = recorder(path, seconds)
    if found is None:
        raise VoiceError(NO_MIC)
    argv, how = found
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    waiter, stopper = asyncio.ensure_future(proc.wait()), asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait([waiter, stopper], timeout=seconds + 5, return_when=asyncio.FIRST_COMPLETED)
        if proc.returncode is None:              # stopped early: let it finish the file
            try:
                if how == "q":
                    proc.stdin.write(b"q")
                    await proc.stdin.drain()
                elif os.name == "nt":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
                await asyncio.wait_for(proc.wait(), 5)
            except (OSError, asyncio.TimeoutError):
                proc.kill()
    finally:
        stopper.cancel()
        if proc.returncode is None:
            proc.kill()
    if not Path(path).is_file() or Path(path).stat().st_size < 1000:
        error = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
        raise VoiceError(f"nothing was recorded{f': {error[:200]}' if error else ''} (is the microphone allowed?)")
    return path


def whisper_cpp():
    """(executable, model) for whisper.cpp, when it and a model are there."""
    exe = next((which(n) for n in ("whisper-cli", "whisper-cpp", "whisper.cpp") if which(n)), None)
    model = config().get("whisper_cpp_model") or os.environ.get("WHISPER_CPP_MODEL")
    return (exe, model) if exe and model and Path(model).expanduser().is_file() else None


def local_transcribe(path):
    """Text from a local whisper, or None when there's none."""
    cfg = config()
    language = cfg.get("language")
    with tempfile.TemporaryDirectory(prefix="nanocode-stt-") as folder:
        if which("whisper"):
            argv = ["whisper", str(path), "--model", cfg.get("whisper_model") or "base", "--output_format", "txt",
                    "--output_dir", folder, "--fp16", "False"] + (["--language", language] if language else [])
            done = run(argv)
            out = Path(folder) / (Path(path).stem + ".txt")
        else:
            found = whisper_cpp()
            if not found:
                return None
            wav = Path(path)
            if wav.suffix.lower() != ".wav":       # whisper.cpp reads 16 kHz wav files
                if not which("ffmpeg"):
                    return None
                wav = Path(folder) / "audio.wav"
                run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-ar", "16000", "-ac", "1", str(wav)])
            exe, model = found
            done = run([exe, "-m", str(Path(model).expanduser()), "-f", str(wav), "-nt", "-otxt",
                        "-of", str(Path(folder) / "out")] + (["-l", language] if language else []))
            out = Path(folder) / "out.txt"
        if not out.is_file():
            raise VoiceError(f"whisper failed: {done.stderr.decode(errors='replace').strip()[-300:]}")
        return " ".join(out.read_text(encoding="utf-8", errors="replace").split())


async def api_transcribe(path):
    cfg = config()
    url = cfg.get("stt_url") or OPENAI_STT_URL
    key = cfg.get("stt_key") or (os.environ.get("OPENAI_API_KEY") if url == OPENAI_STT_URL else None)
    if url == OPENAI_STT_URL and not key:
        raise VoiceError(NO_STT)
    path = Path(path)
    data = {"model": cfg.get("stt_model") or "whisper-1"}
    if cfg.get("language"):
        data["language"] = cfg["language"]
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers={"Authorization": f"Bearer {key}"} if key else {}, data=data,
                                     files={"file": (path.name, path.read_bytes(),
                                                     AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))})
    except httpx.HTTPError as e:
        raise VoiceError(f"transcription failed: {e}")
    if resp.status_code != 200:
        raise VoiceError(f"transcription failed: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        return " ".join(str(resp.json().get("text", "")).split())
    except ValueError:
        return " ".join(resp.text.split())


async def transcribe(path):
    """The words spoken in an audio file (wav, ogg, mp3...). Raises VoiceError with a clear message."""
    if (config().get("stt") or "auto") != "api":
        try:
            text = await asyncio.to_thread(local_transcribe, path)
        except (OSError, subprocess.SubprocessError) as e:
            raise VoiceError(f"whisper failed: {e}")
        if text is not None:
            return text
        if config().get("stt") == "local":
            raise VoiceError("no local whisper found: pip install openai-whisper, or whisper.cpp with a model")
    return await api_transcribe(path)


# --- In the app ---

LISTENING = {"stop": None}


async def listen():
    """Records a message, transcribes it and puts the text in the prompt to check and send."""
    if nc.st.headless:
        nc.out(f"{nc.RED}⏺ /listen needs the app (a microphone and a prompt){nc.RESET}\n")
        return
    if LISTENING["stop"] is not None:
        LISTENING["stop"].set()
        return
    folder = Path(tempfile.mkdtemp(prefix="nanocode-listen-"))
    path = folder / "speech.wav"
    if recorder(path) is None:
        nc.out(f"{nc.RED}⏺ {NO_MIC}{nc.RESET}\n")
        return
    LISTENING["stop"] = asyncio.Event()
    nc.out(f"{nc.CYAN}⏺ Listening…{nc.RESET} {nc.DIM}speak, then press Enter (or pause; {VOICE_MAX_SECONDS}s at most)"
           f"{nc.RESET}")
    nc.invalidate()
    try:
        await record(path, LISTENING["stop"])
        LISTENING["stop"] = None
        nc.out(f"  {nc.DIM}⎿  transcribing…{nc.RESET}")
        text = await transcribe(path)
    except VoiceError as e:
        nc.out(f"{nc.RED}⏺ {e}{nc.RESET}\n")
        return
    finally:
        LISTENING["stop"] = None
        shutil.rmtree(folder, ignore_errors=True)
    if not text:
        nc.out(f"  {nc.DIM}⎿  didn't catch anything{nc.RESET}\n")
        return
    nc.out(f"  {nc.DIM}⎿  in the prompt: check it and press Enter to send{nc.RESET}\n")
    box = nc.st.input_box
    if box is not None:
        box.text = (box.text.rstrip() + " " if box.text.strip() else "") + text
        box.buffer.cursor_position = len(box.text)
    nc.invalidate()


@command("/listen", "speak a message: records until Enter or a pause, then puts the text in the prompt")
async def cmd_listen(arg):
    await listen()


@command("/voice", "read replies aloud: /voice on|off, /voice test", anytime=True)
async def cmd_voice(arg):
    cfg = dict(config())
    if arg in ("on", "off"):
        cfg["speak"] = arg == "on"
        nc.st.config["voice"] = cfg
        nc.save_config()
        if arg == "off":
            stop_speaking()
    elif arg == "stop":
        stop_speaking()
        return
    engine = tts_engine()
    if arg == "test":
        problem = speak("Hello! This is how nanocode sounds.")
        nc.out(f"{nc.RED}⏺ {problem}{nc.RESET}\n" if problem else f"{nc.GREEN}⏺ Speaking{nc.RESET} {nc.DIM}({engine})"
               f"{nc.RESET}\n")
        return
    state = f"{nc.GREEN}on{nc.RESET}" if cfg.get("speak") else f"{nc.DIM}off{nc.RESET}"
    stt = "local whisper" if which("whisper") or whisper_cpp() else \
        "API" if cfg.get("stt_url") or cfg.get("stt_key") or os.environ.get("OPENAI_API_KEY") else "none"
    mic = recorder(Path(tempfile.gettempdir()) / "x.wav")
    nc.out(f"⏺ Voice replies {state} {nc.DIM}· voice: {engine or 'none'} · listening: "
           f"{Path(mic[0][0]).name if mic else 'no recorder'} → {stt} · /voice on|off|test, /listen, ctrl+x v{nc.RESET}\n")
    if not engine and cfg.get("speak"):
        nc.out(f"  {nc.YELLOW}{NO_TTS}{nc.RESET}\n")


TURNS = {}  # tab -> the reply before the turn, to only read a new one


@on("turn_start")
def before_turn(text):
    TURNS[id(nc.st.current_tab())] = nc.st.last_reply


@on("turn_end")
def after_turn(text, error):
    before = TURNS.pop(id(nc.st.current_tab()), None)
    if nc.st.headless or error or not config().get("speak"):
        return
    reply = nc.st.last_reply
    if reply and reply != before and nc.st.current_tab() is nc.st.active_tab:
        problem = speak(reply)
        if problem:
            nc.out(f"  {nc.DIM}⎿  {problem}{nc.RESET}")


@on("keybindings")
def voice_keys(kb, ui):
    from prompt_toolkit.filters import Condition
    recording = Condition(lambda: LISTENING["stop"] is not None)

    @kb.add("enter", filter=recording)
    def _(event):
        LISTENING["stop"].set()

    @kb.add("c-x", "v")
    def _(event):
        if LISTENING["stop"] is not None:
            LISTENING["stop"].set()
        else:
            nc.start_input("/listen")


@on("shutdown")
def quiet():
    stop_speaking()
