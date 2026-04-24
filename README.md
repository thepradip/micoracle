# micoracle

Hands-free voice input for **Claude Code**, **Codex CLI**, **OpenCode**, and any
terminal — on **macOS, Linux, and Windows**. OS is auto-detected at launch; STT
and TTS backends are pluggable (local or cloud).

Say *"Codex, refactor this function"* → your speech is captured, transcribed,
and pasted into the focused terminal with Enter pressed. No push-to-talk, no
cloud required.

## Features

- **Cross-platform** — `platform_adapter.py` auto-selects macOS (AppleScript) /
  Linux (xdotool on X11, wtype on Wayland) / Windows (pywin32 + pyautogui).
- **4 STT backends** — MLX Whisper (Apple Silicon), faster-whisper
  (cross-platform local), OpenAI Whisper API, Azure OpenAI Whisper.
- **4 TTS backends** — macOS `say`, `pyttsx3` (cross-platform offline), OpenAI
  TTS, Azure Speech TTS. Or `none` to stay silent.
- **Continuous listening** with WebRTC VAD + 300 ms preroll buffer (wake words
  never get chopped by onset detection).
- **Wake-word gate** — `"Claude, …"` / `"Codex, …"` with fuzzy mishear tolerance.
- **Two-step follow-up** — say the wake word alone, then the prompt within 8 s.
- **Silence-hallucination filter** — Whisper's *"Thank you."* / *"Amen."*
  artifacts are dropped silently.
- **Locked dispatch target** — frontmost app captured at startup (or pin via
  `--target-app`), stays fixed regardless of where you click.
- **Clipboard-safe** — text is pasted via clipboard; your original contents are
  restored afterwards.

## OS / backend matrix

| Platform | STT default | TTS default | Focus & paste | Notes |
|---|---|---|---|---|
| macOS (Apple Silicon) | `mlx` | `say` | AppleScript | Best local latency |
| macOS (Intel) | `faster` | `say` | AppleScript | |
| Linux X11 | `faster` | `pyttsx3` | `xdotool` + `xclip` | Full support |
| Linux Wayland | `faster` | `pyttsx3` | `wtype` + `wl-copy` | `--target-app` required; auto-focus limited |
| Windows 10/11 | `faster` | `pyttsx3` | pywin32 + pyautogui | Requires `pip install pyperclip pyautogui pywin32 psutil` |

All defaults can be overridden via `--stt-backend` / `--tts-backend` or
environment variables.

## Install

### Common (all platforms)

```bash
git clone https://github.com/thepradip/micoracle.git
cd micoracle
python3 -m pip install -r requirements.txt
```

### Pick an STT backend

**Apple Silicon Mac (best local):**
```bash
pip install mlx-whisper
```

**Cross-platform local:**
```bash
pip install faster-whisper
```

**Cloud (OpenAI or Azure):**
```bash
pip install openai
```

### Pick a TTS backend (optional — only if you want spoken status cues)

**macOS:** `say` is built-in, no install needed.

**Linux / Windows offline:**
```bash
pip install pyttsx3
# Linux also needs: sudo apt install espeak  (or distro equivalent)
```

**Cloud:**
```bash
pip install openai          # OpenAI TTS
```

### Platform-specific

**macOS:**
```bash
brew install portaudio      # required by sounddevice
```

**Linux (X11):**
```bash
sudo apt install xdotool xclip portaudio19-dev python3-dev
```

**Linux (Wayland):**
```bash
sudo apt install wtype wl-clipboard portaudio19-dev python3-dev
```

**Windows:**
```bash
pip install pyperclip pyautogui pywin32 psutil
```

### Configure

```bash
cp .env.example .env
# Edit .env to set API keys (only if using cloud backends), pick backends, etc.
```

## Quickstart

```bash
# Focus the app you want to dispatch to (e.g. Claude Code window), then:
./run_hands_free.sh          # macOS / Linux
run_hands_free.bat           # Windows

# Override STT/TTS at launch:
./run_hands_free.sh --stt-backend azure --tts-backend openai

# Pin the target app (required on Wayland):
./run_hands_free.sh --target-app gnome-terminal
```

Speak:

- **One-shot:** *"Codex, write a Python hello world."* → transcribed + pasted
  into the focused app.
- **Two-step:** *"Codex."* → you hear *"listening"* → within 8 s say the
  prompt → pasted.

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--device <id\|name>` | system default mic | Audio input device. |
| `--list-devices` | — | Print available input devices and exit. |
| `--target-app <name>` | frontmost at startup | Lock dispatch target. |
| `--stt-backend` | `auto` | `auto` / `mlx` / `faster` / `openai` / `azure`. |
| `--tts-backend` | `auto` | `auto` / `say` / `pyttsx3` / `openai` / `azure` / `none`. |
| `--no-speak` | — | Alias for `--tts-backend none`. |

## Environment variables

See [`.env.example`](./.env.example) for the full commented list. Highlights:

| Variable | Purpose |
|---|---|
| `VOICE_AGENT_STT_BACKEND` | Default STT backend |
| `VOICE_AGENT_TTS_BACKEND` | Default TTS backend |
| `VOICE_AGENT_TARGET_APP` | Default dispatch target |
| `VOICE_AGENT_INPUT_DEVICE` | Default mic |
| `VOICE_AGENT_MLX_REPO` | MLX Whisper HF repo |
| `VOICE_AGENT_FASTER_MODEL` | faster-whisper model name |
| `OPENAI_API_KEY` | For `openai` STT/TTS backends |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_KEY` / `AZURE_WHISPER_DEPLOYMENT` | For `azure` STT |
| `AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION` | For `azure` TTS |

## Architecture

```
[ mic ] ──▶ sounddevice callback ──▶ audio_q ──▶ main loop
                                                   │
                                                   ▼  (VAD + preroll buffer)
                                               utterance_q
                                                   │
                                                   ▼
                  worker: STTBackend → wake/cleanup → PlatformAdapter → TTSBackend
```

- `stt.py` — `STTBackend` interface + 4 implementations + OS-aware factory.
- `tts.py` — `TTSBackend` interface + 4 implementations + factory.
- `platform_adapter.py` — `PlatformAdapter` interface + `MacAdapter`,
  `LinuxAdapter`, `WindowsAdapter` + `get_platform_adapter()` factory.
- `hands_free_voice.py` — mic capture, VAD state machine, wake-state, wiring.

## Troubleshooting

**No input devices shown.** OS microphone permission for your terminal is
missing. Grant it (macOS: Privacy & Security → Microphone; Linux: check
PulseAudio / PipeWire; Windows: Settings → Privacy → Microphone) and relaunch
the terminal.

**Wake word never fires.** Confirm the right mic via `--list-devices`. Say
*"Codex"* slowly and clearly — fuzzy matching covers most mishears but a low
input gain can strip initial consonants.

**`[dispatch error]` on Wayland.** Wayland blocks programmatic window focusing.
Pass `--target-app` and keep that window focused yourself.

**Windows: keystrokes sent to the wrong window.** Windows' focus-stealing
prevention can block `SetForegroundWindow`. Give the target window focus
manually, or use [AutoHotkey] to nudge focus-stealing permissions.

**macOS: keystrokes ignored.** Accessibility + Automation permissions missing
for the terminal. System Settings → Privacy & Security → Accessibility /
Automation.

## Privacy & security

- **Local backends keep audio on-device.** MLX Whisper and faster-whisper do
  not make any network calls at inference time.
- **Cloud backends upload audio** (OpenAI / Azure). Use only if you're comfortable.
- **Clipboard** is temporarily overwritten with each dispatch; original
  contents are restored immediately after.
- **No telemetry.** No analytics. No phone-home.
- **Accessibility / Automation permissions are powerful** — the agent types
  into the focused app and presses Enter. Review the source before granting.

## License

[MIT](./LICENSE) © 2026 Pradip Tivhale

## Acknowledgements

- [MLX Whisper](https://github.com/ml-explore/mlx-examples), [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [py-webrtcvad](https://github.com/wiseman/py-webrtcvad)
- [sounddevice](https://python-sounddevice.readthedocs.io/)
- [xdotool](https://github.com/jordansissel/xdotool), [wtype](https://github.com/atx/wtype), [pyautogui](https://pyautogui.readthedocs.io/)
- [pyttsx3](https://pyttsx3.readthedocs.io/)
