# Voice

nanocode can read its replies aloud, and you can speak your messages instead of typing them. The code is in [`nanokit/voice.py`](../nanokit/voice.py).

## Replies out loud

```
/voice on       read each finished reply aloud
/voice off      stop
/voice test     hear the voice
/voice          what's set up
```

Code blocks and links are skipped, and long replies are cut (`"max_chars"`, 1500 by default). The voice is the best one found:

| Voice | Where |
|---|---|
| `edge-tts` | If installed (`pip install edge-tts`) and a player is there (`ffplay`, `mpv`, `mpg123`; `afplay` on macOS): natural voices |
| `say` | macOS, built in |
| Windows voices | Windows, built in (PowerShell) |
| `espeak-ng`, `espeak`, `spd-say` | Linux (`sudo apt install espeak-ng`) |
| ElevenLabs | With `"voice": {"tts": "elevenlabs", "elevenlabs_key": "…", "voice_id": "…"}` |

To pick one, set `"tts"` to `say`, `windows`, `espeak-ng`, `spd-say`, `edge-tts` or `elevenlabs`. `"voice"` chooses a voice name for `say`, `espeak-ng` and `edge-tts` (e.g. `en-GB-SoniaNeural`).

## Speak a message

`/listen` (or **ctrl+x v**) records from your microphone. Press **Enter** when you're done (or just pause, with `sox`; 60 seconds at most). The words go into the prompt, so you can check them before you send.

Recording needs one of:

| Tool | Install |
|---|---|
| `sox` (best: stops when you pause) | macOS `brew install sox` · Linux `sudo apt install sox` · Windows `winget install ChrisBagwell.SoX` |
| `ffmpeg` | macOS `brew install ffmpeg` · Linux `sudo apt install ffmpeg` · Windows `winget install ffmpeg` |
| `arecord` | Linux (alsa-utils) |

On macOS, allow your terminal to use the microphone the first time (System Settings → Privacy & Security → Microphone).

## Speech to text

Recordings (and voice messages sent to the [gateway](gateway.md)) are turned into text by:

1. A local **whisper**, if installed: `pip install openai-whisper` (needs `ffmpeg`), or [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (`whisper-cli`) with a model file set in `"whisper_cpp_model"`. Nothing leaves your computer.
2. Otherwise an **OpenAI-compatible transcription API**: OpenAI's, with `OPENAI_API_KEY`, or any other (Groq, a local server) with `"stt_url"` and `"stt_key"`.

## Settings

All under `"voice"` in `~/.nanocode/config.json`:

| Key | Default | What it does |
|---|---|---|
| `speak` | `false` | Read replies aloud (`/voice on` or `off`) |
| `tts` | `auto` | Which voice (see above) |
| `voice` | | Voice name for `say`, `espeak-ng`, `edge-tts` |
| `max_chars` | `1500` | Longest text read aloud |
| `elevenlabs_key`, `voice_id`, `elevenlabs_model` | | ElevenLabs (or `ELEVENLABS_API_KEY`) |
| `stt` | `auto` | `local` (whisper only), `api` (API only), or `auto` |
| `whisper_model` | `base` | Model for the `whisper` command |
| `whisper_cpp_model` | | Model file for whisper.cpp (or `WHISPER_CPP_MODEL`) |
| `stt_url`, `stt_key`, `stt_model` | OpenAI, `OPENAI_API_KEY`, `whisper-1` | The transcription API |
| `language` | auto-detected | Language you speak, e.g. `en` |
| `mic` | first one found | Windows with ffmpeg: the microphone's name |
