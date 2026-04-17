"""Text-to-speech backends.

All backends share the same non-blocking ``speak`` signature. They are used
only for short status cues ("listening", "sent", "error") so latency and
voice quality tradeoffs are small.

- ``say``      — macOS built-in. Zero deps, instant. macOS only.
- ``pyttsx3``  — cross-platform wrapper over SAPI (Windows) / NSSpeech (macOS)
                 / espeak (Linux). No network.
- ``openai``   — OpenAI TTS cloud API (natural voices).
- ``azure``    — Azure Cognitive Services Speech TTS.
- ``none``     — silent no-op, for --no-speak.

Playback is delegated to an OS audio command: ``afplay`` (macOS),
``paplay`` / ``aplay`` (Linux), or PowerShell's ``(New-Object Media.SoundPlayer)``
(Windows). All are fire-and-forget ``Popen`` calls.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class TTSBackend(ABC):
    """Abstract TTS interface. Implementations must be non-blocking."""

    name: str = "base"

    @abstractmethod
    def speak(self, phrase: str) -> None:
        """Start speaking ``phrase``. Must return immediately."""


# ─────────────────────────── Silent ───────────────────────────────


class SilentTTS(TTSBackend):
    name = "none"

    def speak(self, phrase: str) -> None:  # noqa: ARG002
        return


# ─────────────────────────── macOS say ────────────────────────────


class MacSayTTS(TTSBackend):
    name = "say"

    def __init__(self, voice: str | None = None) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("`say` is macOS only. Use --tts-backend pyttsx3 elsewhere.")
        self.voice = voice

    def speak(self, phrase: str) -> None:
        if not phrase:
            return
        cmd = ["say"]
        if self.voice:
            cmd += ["-v", self.voice]
        cmd.append(phrase)
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ─────────────────────────── pyttsx3 ──────────────────────────────


class Pyttsx3TTS(TTSBackend):
    name = "pyttsx3"

    def __init__(self) -> None:
        try:
            import pyttsx3  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "pyttsx3 not installed. Install with `pip install pyttsx3`."
            ) from exc
        # Pyttsx3 engines are not safe to share across threads, so we create
        # a new engine per call. Cost is small — a few ms.
        self._pyttsx3 = pyttsx3

    def speak(self, phrase: str) -> None:
        if not phrase:
            return
        threading.Thread(target=self._say, args=(phrase,), daemon=True).start()

    def _say(self, phrase: str) -> None:
        try:
            engine = self._pyttsx3.init()
            engine.say(phrase)
            engine.runAndWait()
            engine.stop()
        except Exception:
            pass


# ───────────────────────── Cloud: OpenAI ──────────────────────────


class OpenAITTS(TTSBackend):
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "tts-1",
        voice: str = "alloy",
    ) -> None:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed. Install with `pip install openai`."
            ) from exc
        key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self._client = OpenAI(api_key=key)
        self._model = model
        self._voice = voice

    def speak(self, phrase: str) -> None:
        if not phrase:
            return
        threading.Thread(target=self._speak_sync, args=(phrase,), daemon=True).start()

    def _speak_sync(self, phrase: str) -> None:
        try:
            response = self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=phrase,
            )
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                wav_path = Path(tmp.name)
            try:
                response.stream_to_file(str(wav_path))
                _play_audio_file(wav_path)
            finally:
                wav_path.unlink(missing_ok=True)
        except Exception:
            pass


# ─────────────────────── Cloud: Azure Speech ──────────────────────


class AzureTTS(TTSBackend):
    name = "azure"

    def __init__(
        self,
        subscription_key: str | None = None,
        region: str | None = None,
        voice: str = "en-US-AriaNeural",
    ) -> None:
        self._subscription_key = (
            subscription_key or os.environ.get("AZURE_SPEECH_KEY", "")
        ).strip()
        self._region = (
            region or os.environ.get("AZURE_SPEECH_REGION", "")
        ).strip()
        if not self._subscription_key or not self._region:
            raise RuntimeError(
                "Azure TTS requires AZURE_SPEECH_KEY and AZURE_SPEECH_REGION env vars."
            )
        self._voice = voice

    def speak(self, phrase: str) -> None:
        if not phrase:
            return
        threading.Thread(target=self._speak_sync, args=(phrase,), daemon=True).start()

    def _speak_sync(self, phrase: str) -> None:
        try:
            import requests  # type: ignore
        except ImportError:
            return
        url = f"https://{self._region}.tts.speech.microsoft.com/cognitiveservices/v1"
        ssml = (
            '<speak version="1.0" xml:lang="en-US">'
            f'<voice xml:lang="en-US" name="{self._voice}">{phrase}</voice>'
            "</speak>"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self._subscription_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
            "User-Agent": "voicecode",
        }
        try:
            resp = requests.post(url, headers=headers, data=ssml.encode("utf-8"), timeout=10)
            resp.raise_for_status()
        except Exception:
            return
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            wav_path.write_bytes(resp.content)
            _play_audio_file(wav_path)
        finally:
            wav_path.unlink(missing_ok=True)


# ────────────────────────── OS audio play ─────────────────────────


def _play_audio_file(path: Path) -> None:
    """Play an audio file with whatever OS tool is available. Non-blocking."""
    if sys.platform == "darwin":
        cmd = ["afplay", str(path)]
    elif sys.platform.startswith("linux"):
        if shutil.which("paplay"):
            cmd = ["paplay", str(path)]
        elif shutil.which("aplay"):
            cmd = ["aplay", "-q", str(path)]
        elif shutil.which("mpv"):
            cmd = ["mpv", "--really-quiet", "--no-video", str(path)]
        else:
            return
    elif sys.platform in ("win32", "cygwin"):
        # Use PowerShell's SoundPlayer for WAV; mp3 needs Windows Media Player.
        if path.suffix.lower() == ".wav":
            cmd = [
                "powershell", "-NoProfile", "-Command",
                f"(New-Object Media.SoundPlayer '{path}').PlaySync()",
            ]
        else:
            cmd = ["powershell", "-NoProfile", "-Command", f"Start-Process -Wait '{path}'"]
    else:
        return
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ───────────────────────── Factory / auto ─────────────────────────


@dataclass
class TTSConfig:
    backend: str = "auto"
    voice: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "tts-1"
    openai_voice: str = "alloy"
    azure_key: str | None = None
    azure_region: str | None = None
    azure_voice: str = "en-US-AriaNeural"


def auto_select_tts_backend() -> str:
    if sys.platform == "darwin":
        return "say"
    # pyttsx3 works on Linux (via espeak) and Windows (SAPI)
    return "pyttsx3"


def make_tts_backend(config: TTSConfig) -> TTSBackend:
    backend = config.backend.lower()
    if backend in ("auto", ""):
        backend = auto_select_tts_backend()

    if backend in ("none", "off", "silent"):
        return SilentTTS()
    if backend == "say":
        return MacSayTTS(voice=config.voice)
    if backend == "pyttsx3":
        return Pyttsx3TTS()
    if backend == "openai":
        return OpenAITTS(
            api_key=config.openai_api_key,
            model=config.openai_model,
            voice=config.openai_voice,
        )
    if backend == "azure":
        return AzureTTS(
            subscription_key=config.azure_key,
            region=config.azure_region,
            voice=config.azure_voice,
        )
    raise ValueError(
        f"Unknown TTS backend: {backend!r}. "
        "Valid: say, pyttsx3, openai, azure, none, auto"
    )
