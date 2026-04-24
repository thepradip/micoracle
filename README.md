<div align="center">

<img src="./logo.svg" alt="micoracle" width="400"/>


**Hands-free voice input for AI coding assistants — on macOS, Linux, and Windows.**


Say *"Codex, refactor this function"* → your speech is transcribed and pasted into the focused terminal with Enter pressed. No push-to-talk. No cloud required.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![macOS](https://img.shields.io/badge/macOS-✓-brightgreen?logo=apple)](https://www.apple.com/macos/)
[![Linux](https://img.shields.io/badge/Linux-✓-brightgreen?logo=linux&logoColor=white)](https://www.linux.org/)
[![Windows](https://img.shields.io/badge/Windows-✓-brightgreen?logo=windows)](https://www.microsoft.com/windows)

</div>

---

## What is micoracle?

micoracle is a cross-platform voice agent that listens continuously in the background. When you say a wake word (`"Claude, ..."` or `"Codex, ..."`), it captures your speech, transcribes it, and types it directly into whatever terminal or app you have focused — pressing Enter automatically.

- Works with **Claude Code**, **Codex CLI**, **OpenCode**, and any terminal
- Fully **local by default** — no cloud account needed
- STT and TTS backends are **swappable** via a single flag or env var

---

## Features

| | Feature | Detail |
|---|---|---|
| 🌐 | **Cross-platform** | Auto-selects macOS (AppleScript), Linux (xdotool / wtype), or Windows (pywin32 + pyautogui) |
| 🎙️ | **4 STT backends** | MLX Whisper · faster-whisper · OpenAI · Azure |
| 🔊 | **4 TTS backends** | macOS `say` · pyttsx3 · OpenAI TTS · Azure Speech TTS |
| 🔉 | **Continuous listening** | WebRTC VAD + 300 ms preroll buffer — wake words are never clipped at onset |
| 💬 | **Wake-word gate** | `"Claude, …"` / `"Codex, …"` with fuzzy mishear tolerance |
| ⏱️ | **Two-step follow-up** | Say wake word alone → you hear *"listening"* → speak prompt within 8 s |
| 🚫 | **Hallucination filter** | Whisper artifacts like *"Thank you."* / *"Amen."* are silently dropped |
| 🔒 | **Target-aware dispatch** | macOS / Windows can reactivate the startup target; Linux dispatches to the focused window |
| 📋 | **Clipboard-conscious** | Clipboard paste paths restore your original content immediately after dispatch |

---

## Architecture

![micoracle architecture](./assets/architecture.svg)

### Simple flow

When micoracle is running, it behaves like a quiet voice remote for your coding assistant:

1. **You speak a command** — for example, `Codex, refactor this function`.
2. **micoracle listens for real speech** — short background noises are ignored.
3. **The audio is transcribed** — using a local or cloud STT backend.
4. **The wake word is checked** — only commands that begin with `Claude` or `Codex` are accepted.
5. **The clean prompt is sent** — micoracle pastes the command into the target app and presses Enter.
6. **A short status cue can play** — for example, `listening`, `sent`, or `error`.

The important safety idea: random speech is ignored unless it passes the wake-word gate.

### Module overview

| Module | Responsibility |
|---|---|
| `hands_free_voice.py` | Main entry point — mic capture, VAD wiring, wake-word gate, dispatch loop |
| `segmenter.py` | `VADSegmenter` — frame-by-frame VAD state machine, preroll ring buffer, emits complete utterances |
| `stt.py` | `STTBackend` ABC + 4 implementations + `STTConfig` dataclass + OS-aware auto factory |
| `tts.py` | `TTSBackend` ABC + 4 implementations + `TTSConfig` + auto factory |
| `platform_adapter.py` | `PlatformAdapter` ABC + `MacAdapter` / `LinuxAdapter` / `WindowsAdapter` + `get_platform_adapter()` factory |

### VAD state machine

```
IDLE ──(speech frames ≥ 4)──▶ CAPTURING ──(silence ≥ 840 ms OR 18 s cap)──▶ EMIT utterance ──▶ IDLE
 ▲                                 │
 └──(speech_run decays on silence)─┘
```

---

## Platform & Backend Matrix

| Platform | STT default | TTS default | Focus & paste method | Notes |
|---|---|---|---|---|
| macOS (Apple Silicon) | `mlx` | `say` | AppleScript | Lowest local latency |
| macOS (Intel) | `faster` | `say` | AppleScript | |
| Linux X11 | `faster` | `pyttsx3` | focused window via `xdotool type` | Keep the target window focused |
| Linux Wayland | `faster` | `pyttsx3` | focused window via `wtype` + `wl-copy` | `--target-app` required; keep target focused |
| Windows 10/11 | `faster` | `pyttsx3` | pywin32 + pyautogui | Requires extra pip packages |

All defaults can be overridden via `--stt-backend` / `--tts-backend` or environment variables.

---

## Install

![macOS install commands shown in a terminal](./assets/macos-install-terminal.svg)

### Step 1 — Core dependencies (all platforms)

```bash
git clone https://github.com/thepradip/micoracle.git
cd micoracle
pip install -r requirements.txt
```

### Step 2 — Pick an STT backend

| Backend | Best for | Install |
|---|---|---|
| `mlx` | Apple Silicon (fastest local) | `pip install mlx-whisper` |
| `faster` | Cross-platform local | `pip install faster-whisper` |
| `openai` | Cloud (OpenAI Whisper API) | `pip install openai` |
| `azure` | Cloud (Azure OpenAI Whisper) | `pip install openai` + set Azure env vars |

### Step 3 — Pick a TTS backend _(optional)_

| Backend | Best for | Install |
|---|---|---|
| `say` | macOS (built-in) | nothing |
| `pyttsx3` | Linux / Windows offline | `pip install pyttsx3` + `sudo apt install espeak` |
| `openai` | Cloud (OpenAI TTS) | `pip install openai` |
| `azure` | Cloud (Azure Speech) | set Azure Speech env vars |

### Step 4 — Platform-specific system packages

**macOS (Apple Silicon):**
```bash
brew install portaudio

# Default STT backend on Apple Silicon:
pip install mlx-whisper
```

**macOS (Intel):**
```bash
brew install portaudio

# Default STT backend on Intel Mac:
pip install faster-whisper
```

**Linux (X11):**
```bash
sudo apt install xdotool portaudio19-dev python3-dev

# Default STT backend on Linux:
pip install faster-whisper
```

**Linux (Wayland):**
```bash
sudo apt install wtype wl-clipboard portaudio19-dev python3-dev

# Default STT backend on Linux:
pip install faster-whisper
```

**Windows:**
```bash
pip install pyperclip pyautogui pywin32 psutil

# Default STT backend on Windows:
pip install faster-whisper
```

### Step 5 — Configure

```bash
cp .env.example .env
# Set API keys if using cloud backends; adjust default backends and target app
```

---

## Quickstart

```bash
# 1. Focus the app you want to type into (Claude Code, Codex CLI, a terminal…)
# 2. Launch micoracle:
./run_hands_free.sh          # macOS / Linux
run_hands_free.bat           # Windows
```

**One-shot:** *"Codex, write a Python hello world."*
→ transcribed and pasted with Enter.

**Two-step:** *"Codex."* → you hear *"listening"* → say the prompt within 8 s → pasted.

**Override backends at launch:**
```bash
./run_hands_free.sh --stt-backend openai --tts-backend openai
```

**Pin the target app (required on Wayland):**
```bash
./run_hands_free.sh --target-app gnome-terminal
```

---

## CLI Reference

| Flag | Default | Description |
|---|---|---|
| `--device <id\|name>` | system default mic | Audio input device |
| `--list-devices` | — | Print available input devices and exit |
| `--target-app <name>` | frontmost app at startup | Lock the dispatch target |
| `--stt-backend` | `auto` | `auto` / `mlx` / `faster` / `openai` / `azure` |
| `--tts-backend` | `auto` | `auto` / `say` / `pyttsx3` / `openai` / `azure` / `none` |
| `--no-speak` | — | Alias for `--tts-backend none` |

---

## Environment Variables

See [`.env.example`](./.env.example) for the full commented list.

| Variable | Purpose |
|---|---|
| `VOICE_AGENT_STT_BACKEND` | Default STT backend (`auto` / `mlx` / `faster` / `openai` / `azure`) |
| `VOICE_AGENT_TTS_BACKEND` | Default TTS backend (`auto` / `say` / `pyttsx3` / `openai` / `azure` / `none`) |
| `VOICE_AGENT_TARGET_APP` | Default dispatch target app name |
| `VOICE_AGENT_INPUT_DEVICE` | Default microphone device (name fragment or numeric id) |
| `VOICE_AGENT_MLX_REPO` | MLX Whisper HuggingFace repo (Apple Silicon) |
| `VOICE_AGENT_FASTER_MODEL` | faster-whisper model (`tiny.en` / `base.en` / `small.en` / `medium.en` / `large-v3`) |
| `VOICE_AGENT_FASTER_DEVICE` | faster-whisper device (`auto` / `cpu` / `cuda`) |
| `VOICE_AGENT_FASTER_COMPUTE` | faster-whisper compute type (`int8` / `float16` / `int8_float16`) |
| `VOICE_AGENT_TTS_VOICE` | macOS `say` voice name (e.g. `Samantha`) |
| `VOICE_AGENT_OPENAI_STT_MODEL` | OpenAI STT model name (default: `whisper-1`) |
| `VOICE_AGENT_OPENAI_TTS_VOICE` | OpenAI TTS voice (`alloy` / `echo` / `fable` / `onyx` / `nova` / `shimmer`) |
| `VOICE_AGENT_AZURE_TTS_VOICE` | Azure Speech TTS voice (default: `en-US-AriaNeural`) |
| `HF_HUB_ENABLE_HF_TRANSFER` | Set to `1` for faster HuggingFace model downloads |
| `OPENAI_API_KEY` | OpenAI STT / TTS backends |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI Whisper endpoint |
| `AZURE_OPENAI_KEY` | Azure OpenAI key |
| `AZURE_WHISPER_DEPLOYMENT` | Azure Whisper deployment name (default: `whisper`) |
| `AZURE_SPEECH_KEY` | Azure Speech TTS key |
| `AZURE_SPEECH_REGION` | Azure Speech TTS region (e.g. `eastus`) |

---

## Troubleshooting

**No input devices shown.**
Grant microphone permission to your terminal. macOS: *Privacy & Security → Microphone*. Linux: check PulseAudio / PipeWire. Windows: *Settings → Privacy → Microphone*.

**Wake word never fires.**
Confirm the right mic with `--list-devices`. Say *"Codex"* slowly — fuzzy matching covers mishears, but low mic gain can strip initial consonants.

**`[dispatch error]` on Wayland.**
Wayland blocks programmatic window focus. Pass `--target-app <name>` and keep that window focused manually.

**Windows: keystrokes go to the wrong window.**
Windows' focus-stealing prevention can block `SetForegroundWindow`. Give the target window focus manually before speaking, or use AutoHotkey to relax focus-stealing permissions.

**macOS: keystrokes ignored.**
Accessibility + Automation permissions are missing for your terminal. *System Settings → Privacy & Security → Accessibility / Automation*.

---

## Privacy & Security

- **Local backends are fully on-device.** MLX Whisper and faster-whisper make zero network calls at inference time.
- **Cloud backends upload audio** (OpenAI, Azure). Use only if you are comfortable with that.
- **Clipboard may be temporarily overwritten** on clipboard-based dispatch paths; your original contents are restored immediately after.
- **No telemetry. No analytics. No phone-home.**
- **Accessibility / Automation permissions are powerful** — the agent types into the focused app and presses Enter. Review the source before granting.

---

## Future Scope

- **Additional STT backends:** ElevenLabs Scribe and Google Gemini are good candidates for future cloud transcription support.
- **Stronger Linux target locking:** improve X11 / Wayland dispatch so Linux behavior can more closely match macOS and Windows target reactivation.
- **Packaged installers:** provide a smoother setup path for macOS, Linux, and Windows with platform-specific dependency checks.
- **Tray / menu bar control:** add quick controls for pause, resume, backend selection, and target status.
- **Custom wake words:** allow users to configure wake words beyond `Claude` and `Codex`.
- **Structured command history:** optionally show recent accepted prompts locally for debugging and auditability.

---

## License

[MIT](./LICENSE) © 2026 Pradip Tivhale

---

## Acknowledgements

- [MLX Whisper](https://github.com/ml-explore/mlx-examples) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [py-webrtcvad](https://github.com/wiseman/py-webrtcvad)
- [sounddevice](https://python-sounddevice.readthedocs.io/) · [soundfile](https://python-soundfile.readthedocs.io/)
- [xdotool](https://github.com/jordansissel/xdotool) · [wtype](https://github.com/atx/wtype) · [pyautogui](https://pyautogui.readthedocs.io/)
- [pyttsx3](https://pyttsx3.readthedocs.io/)
