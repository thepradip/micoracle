#!/usr/bin/env python3
"""Global VoiceCode — hands-free voice input, cross-platform.

Flow:
  1. Continuously listens to the mic (no fixed windows).
  2. WebRTC VAD detects speech start/stop with a 300 ms preroll buffer.
  3. A pluggable STT backend (MLX / faster-whisper / OpenAI / Azure) transcribes
     the captured utterance.
  4. If the text starts with "claude" / "codex" (fuzzy), the remainder is pasted
     into the target app via the platform adapter (macOS / Linux / Windows).
  5. Short spoken status cues ("listening", "sent", "error") go out via the
     pluggable TTS backend (macOS say / pyttsx3 / OpenAI / Azure).

OS is auto-detected. Backends default to the best local option per platform,
override via CLI flag or env var.
"""

from __future__ import annotations

import argparse
import difflib
import os
import platform
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

import platform_adapter as _pa
import stt as _stt
import tts as _tts
from segmenter import VADSegmenter


# ──────────────────────────── .env loader ─────────────────────────


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(Path(__file__).resolve().parent / ".env")


# ──────────────────────────── constants ───────────────────────────


SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

MIN_SPEECH_FRAMES = 4          # ~120 ms before we lock in speech state
MAX_SILENCE_FRAMES = 28        # ~840 ms of silence ends an utterance
MAX_UTTERANCE_FRAMES = 600     # ~18 s cap per utterance
PREROLL_FRAMES = 10            # ~300 ms of audio retained before onset
VAD_AGGRESSIVENESS = 2         # 0-3; higher = stricter silence filtering

FOLLOWUP_TIMEOUT_SECS = 8.0    # how long to stay armed after a bare wake word
WAKE_FUZZY_THRESHOLD = 0.78
CHECK_FIRST_N_WORDS = 3

WAKE_VARIANTS: dict[str, set[str]] = {
    "claude": {
        "claude", "clawed", "cloud", "clod", "cloudy", "clyde", "claud",
    },
    "codex": {
        "codex", "codec", "codecs", "coded", "coed", "goodx", "godex",
        "cortex", "kodak",
    },
}

# Phrases Whisper hallucinates on silence. Swallowed before wake-word routing
# so they never consume an armed follow-up slot.
SILENCE_HALLUCINATIONS = {
    "thank you", "thank you.", "thanks", "thanks.", "thanks for watching",
    "thanks for watching.", "amen", "amen.", "you", "you.", "bye", "bye.",
    ".", "okay", "okay.", "ok", "ok.",
}


# ─────────────────────────── text utilities ───────────────────────


def detect_wake_word(text: str) -> tuple[str | None, int]:
    words = [w.strip(",.!?;:\"'") for w in text.split() if w.strip(",.!?;:\"'")]
    for idx, word in enumerate(words[:CHECK_FIRST_N_WORDS]):
        lw = word.lower()
        for wake, variants in WAKE_VARIANTS.items():
            if lw in variants:
                return wake, idx
            for variant in variants:
                if difflib.SequenceMatcher(None, lw, variant).ratio() >= WAKE_FUZZY_THRESHOLD:
                    return wake, idx
    return None, -1


def extract_command(text: str, wake_idx: int) -> str:
    return " ".join(text.split()[wake_idx + 1:]).strip(" ,.!?;:")


def looks_hallucinated(text: str) -> bool:
    words = text.lower().split()
    if len(words) < 9:
        return False
    for size in (3, 4, 5, 6):
        if len(words) < size * 3:
            continue
        for i in range(len(words) - size * 3 + 1):
            chunk = words[i:i + size]
            if (words[i + size:i + 2 * size] == chunk
                    and words[i + 2 * size:i + 3 * size] == chunk):
                return True
    return False


def is_silence_hallucination(text: str) -> bool:
    return text.strip().lower() in SILENCE_HALLUCINATIONS


# ──────────────────────────── wake state ──────────────────────────


class WakeState:
    """Remembers which wake word is 'armed' for a follow-up utterance."""

    def __init__(self) -> None:
        self._backend: str | None = None
        self._expires_at: float = 0.0

    def arm(self, backend: str, timeout_secs: float = FOLLOWUP_TIMEOUT_SECS) -> None:
        self._backend = backend
        self._expires_at = time.monotonic() + timeout_secs

    def clear(self) -> None:
        self._backend = None
        self._expires_at = 0.0

    def active_backend(self) -> str | None:
        if not self._backend:
            return None
        if time.monotonic() > self._expires_at:
            self.clear()
            return None
        return self._backend


# ──────────────────────── audio / device helpers ──────────────────


def list_input_devices() -> list[tuple[int, dict]]:
    import sounddevice as sd
    return [
        (idx, d) for idx, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


def resolve_input_device(device_hint: str) -> int | None:
    import sounddevice as sd
    devices = list_input_devices()
    if not devices:
        raise RuntimeError(
            "No audio input devices found. Grant microphone access to your "
            "terminal and run with --list-devices to verify."
        )
    if device_hint:
        if device_hint.isdigit():
            idx = int(device_hint)
            info = sd.query_devices(idx)
            if info["max_input_channels"] <= 0:
                raise RuntimeError(f"Device {idx} is not an input.")
            return idx
        lowered = device_hint.lower()
        for idx, d in devices:
            if lowered in d["name"].lower():
                return idx
        raise RuntimeError(
            f"No input device matched '{device_hint}'. Run with --list-devices."
        )
    default = sd.default.device[0]
    if isinstance(default, int) and default >= 0:
        info = sd.query_devices(default)
        if info["max_input_channels"] > 0:
            return default
    return devices[0][0]


# ────────────────────────── CLI parsing ───────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hands-free voice input for terminal code assistants. "
                    "Cross-platform (macOS / Linux / Windows), OS auto-detected.",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("VOICE_AGENT_INPUT_DEVICE", "").strip(),
        help="Microphone id or name fragment (see --list-devices).",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print available input devices and exit.",
    )
    parser.add_argument(
        "--target-app",
        default=os.environ.get("VOICE_AGENT_TARGET_APP", "").strip(),
        help="Pin dispatch to a specific app name. Defaults to the frontmost "
             "app at startup. On Wayland, this flag is required.",
    )
    parser.add_argument(
        "--stt-backend",
        default=os.environ.get("VOICE_AGENT_STT_BACKEND", "auto").strip() or "auto",
        choices=["auto", "mlx", "faster", "openai", "azure"],
        help="Speech-to-text backend. 'auto' picks MLX on Apple Silicon, "
             "faster-whisper elsewhere.",
    )
    parser.add_argument(
        "--tts-backend",
        default=os.environ.get("VOICE_AGENT_TTS_BACKEND", "auto").strip() or "auto",
        choices=["auto", "say", "pyttsx3", "openai", "azure", "none"],
        help="Text-to-speech backend for status cues. 'auto' picks 'say' on "
             "macOS, 'pyttsx3' elsewhere.",
    )
    parser.add_argument(
        "--no-speak",
        action="store_true",
        help="Equivalent to --tts-backend none.",
    )
    return parser.parse_args()


# ───────────────────────────── main loop ──────────────────────────


def main() -> int:
    args = parse_args()

    if args.list_devices:
        devs = list_input_devices()
        if not devs:
            print("No input devices found.", file=sys.stderr)
            return 1
        print("Input devices:")
        for idx, d in devs:
            print(
                f"  [{idx}] {d['name']} "
                f"(inputs={d['max_input_channels']}, "
                f"default_sr={int(d['default_samplerate'])})"
            )
        return 0

    # Platform adapter — auto-picks MacAdapter / LinuxAdapter / WindowsAdapter.
    try:
        adapter = _pa.get_platform_adapter()
    except RuntimeError as exc:
        print(f"Platform adapter error: {exc}", file=sys.stderr)
        return 1

    # Target app lock.
    target_app = args.target_app.strip()
    if not target_app:
        try:
            target_app = adapter.get_frontmost_app()
        except RuntimeError as exc:
            print(f"Unable to capture frontmost app: {exc}", file=sys.stderr)
            print(
                "Pass --target-app <name> to pin the dispatch target explicitly.",
                file=sys.stderr,
            )
            return 1
    if target_app not in adapter.supported_apps:
        print(
            f"[warn] target app '{target_app}' is not in the known list for this OS "
            f"(known: {', '.join(sorted(adapter.supported_apps)) or 'none'}); "
            "dispatch may still work.",
            flush=True,
        )

    # STT backend.
    try:
        stt_backend = _stt.make_stt_backend(_stt.STTConfig(
            backend=args.stt_backend,
            mlx_repo=os.environ.get(
                "VOICE_AGENT_MLX_REPO", "mlx-community/whisper-medium.en-mlx-4bit",
            ),
            faster_model=os.environ.get("VOICE_AGENT_FASTER_MODEL", "small.en"),
            faster_device=os.environ.get("VOICE_AGENT_FASTER_DEVICE", "auto"),
            faster_compute_type=os.environ.get("VOICE_AGENT_FASTER_COMPUTE", "int8"),
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            openai_model=os.environ.get("VOICE_AGENT_OPENAI_STT_MODEL", "whisper-1"),
            azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT"),
            azure_api_key=os.environ.get("AZURE_OPENAI_KEY"),
            azure_deployment=os.environ.get("AZURE_WHISPER_DEPLOYMENT", "whisper"),
        ))
    except Exception as exc:
        print(f"STT backend init failed: {exc}", file=sys.stderr)
        return 1

    # TTS backend.
    tts_choice = "none" if args.no_speak else args.tts_backend
    try:
        tts_backend = _tts.make_tts_backend(_tts.TTSConfig(
            backend=tts_choice,
            voice=os.environ.get("VOICE_AGENT_TTS_VOICE") or None,
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            openai_voice=os.environ.get("VOICE_AGENT_OPENAI_TTS_VOICE", "alloy"),
            azure_key=os.environ.get("AZURE_SPEECH_KEY"),
            azure_region=os.environ.get("AZURE_SPEECH_REGION"),
            azure_voice=os.environ.get("VOICE_AGENT_AZURE_TTS_VOICE", "en-US-AriaNeural"),
        ))
    except Exception as exc:
        print(f"[warn] TTS backend init failed ({exc}); running silent.", flush=True)
        tts_backend = _tts.SilentTTS()

    # Audio device.
    try:
        device = resolve_input_device(args.device)
    except RuntimeError as exc:
        print(f"Device error: {exc}", file=sys.stderr)
        return 1

    import sounddevice as sd
    import webrtcvad

    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    audio_q: queue.Queue[np.ndarray] = queue.Queue()
    utterance_q: queue.Queue[np.ndarray] = queue.Queue()
    wake_state = WakeState()

    def worker() -> None:
        while True:
            pcm = utterance_q.get()
            try:
                text = stt_backend.transcribe(pcm, SAMPLE_RATE)
            except Exception as exc:
                print(f"[transcribe error] {exc}", flush=True)
                tts_backend.speak("error")
                continue
            if not text:
                continue
            if looks_hallucinated(text):
                print(f"[hallucination] {text[:60]}...", flush=True)
                continue
            if is_silence_hallucination(text):
                continue

            armed = wake_state.active_backend()
            if armed:
                wake, idx = detect_wake_word(text)
                if wake:
                    cmd = extract_command(text, idx)
                    if cmd:
                        _dispatch(adapter, target_app, wake, cmd, tts_backend)
                        wake_state.clear()
                    else:
                        wake_state.arm(wake)
                        print(f"[{wake}] listening...", flush=True)
                        tts_backend.speak("listening")
                    continue
                cmd = text.strip(" ,.!?;:")
                if cmd:
                    _dispatch(adapter, target_app, armed, cmd, tts_backend)
                    wake_state.clear()
                else:
                    print(f"[{armed}] empty command, ignored", flush=True)
                    tts_backend.speak("empty")
                continue

            wake, idx = detect_wake_word(text)
            if not wake:
                print(f"[ignored] {text}", flush=True)
                continue
            cmd = extract_command(text, idx)
            if cmd:
                _dispatch(adapter, target_app, wake, cmd, tts_backend)
                wake_state.clear()
            else:
                wake_state.arm(wake)
                print(f"[{wake}] listening...", flush=True)
                tts_backend.speak("listening")

    threading.Thread(target=worker, daemon=True).start()

    def audio_cb(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            print(f"[audio status] {status}", file=sys.stderr, flush=True)
        audio_q.put(indata.copy().flatten())

    stream = sd.InputStream(
        device=device,
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        callback=audio_cb,
    )
    device_info = sd.query_devices(device)

    print(f"Global VoiceCode ready on {platform.system()} ({platform.machine()}).")
    print(f"  Input device: {device_info['name']}")
    print(f"  STT backend:  {stt_backend.name}")
    print(f"  TTS backend:  {tts_backend.name}")
    print(f"  Target app (locked): {target_app}")
    print("  Say:  'claude, <your prompt>'  or  'codex, <your prompt>'")
    print("  (non-wake-word speech is ignored)")
    print()

    segmenter = VADSegmenter(
        vad=vad,
        sample_rate=SAMPLE_RATE,
        preroll_frames=PREROLL_FRAMES,
        min_speech_frames=MIN_SPEECH_FRAMES,
        max_silence_frames=MAX_SILENCE_FRAMES,
        max_utterance_frames=MAX_UTTERANCE_FRAMES,
    )

    with stream:
        while True:
            frame = audio_q.get()
            pcm = segmenter.process_frame(frame)
            if pcm is not None:
                utterance_q.put(pcm)


def _dispatch(
    adapter: _pa.PlatformAdapter,
    target_app: str,
    wake: str,
    command: str,
    tts: _tts.TTSBackend,
) -> None:
    print(f"[{wake}] -> {command}", flush=True)
    try:
        adapter.paste_and_return(command, target_app)
    except Exception as exc:
        print(f"[dispatch error] {exc}", flush=True)
        tts.speak("error")
        return
    print(f"[{wake}] sent to {target_app}", flush=True)
    tts.speak("sent")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:  # pragma: no cover
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
