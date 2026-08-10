"""Speech-to-text backends.

Five concrete backends plus a factory with OS-aware auto-selection:

- ``mlx``      — MLX Whisper, Apple Silicon only, fastest on-device
- ``faster``   — faster-whisper (CTranslate2), cross-platform local
- ``openai``   — OpenAI Whisper cloud API (batch)
- ``azure``    — Azure OpenAI Whisper cloud API (batch)
- ``realtime`` — OpenAI GPT-4o Realtime transcription (WebSocket streaming)
                 Cost-saving design: activate only after wake-word detection.

All backends share the same ``transcribe`` signature and can be swapped at
runtime via ``--stt-backend`` / ``--command-stt-backend`` or env vars. Heavy
deps are imported lazily inside each backend so users only need packages they
actually use.
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class STTBackend(ABC):
    """Abstract STT interface."""

    name: str = "base"

    @abstractmethod
    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        """Transcribe a mono PCM-16 numpy buffer. Returns the text (may be empty)."""


# ───────────────────── Local: MLX Whisper ─────────────────────────


# Bias the Whisper decoder toward the wake words. "Micoracle" is an invented
# word Whisper has never seen in training, so without this hint it snaps to
# the nearest real phrases ("Meek Oracle", "Mike what I", "kinetics").
WAKE_WORD_PROMPT = (
    "Voice commands for the MicOracle assistant. Commands start with the wake "
    "words Micoracle, Claude, or Codex. Example: Micoracle, take a screenshot."
)


class MLXWhisperBackend(STTBackend):
    name = "mlx"

    def __init__(self, repo: str = "mlx-community/whisper-medium.en-mlx-4bit") -> None:
        if sys.platform != "darwin" or platform.machine() not in ("arm64", "aarch64"):
            raise RuntimeError(
                "MLX Whisper runs only on macOS with Apple Silicon. "
                "Use --stt-backend faster for other platforms."
            )
        self.repo = repo

    # Whisper hallucinates or hangs on clips shorter than this threshold.
    _MIN_DURATION_S = 0.75

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        if len(pcm_int16) / sample_rate < self._MIN_DURATION_S:
            return ""

        import mlx_whisper  # type: ignore
        import soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            sf.write(str(wav_path), pcm_int16, sample_rate, subtype="PCM_16")
            result = mlx_whisper.transcribe(
                str(wav_path),
                path_or_hf_repo=self.repo,
                language="en",
                fp16=False,
                initial_prompt=WAKE_WORD_PROMPT,
            )
            return " ".join(result.get("text", "").split())
        finally:
            wav_path.unlink(missing_ok=True)


# ─────────────────── Local: faster-whisper ────────────────────────


class FasterWhisperBackend(STTBackend):
    name = "faster"

    def __init__(
        self,
        model: str = "small.en",
        device: str = "auto",
        compute_type: str = "int8",
    ) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper not installed. Install with `pip install faster-whisper`."
            ) from exc
        self._model = WhisperModel(model, device=device, compute_type=compute_type)

    _MIN_DURATION_S = 0.75

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        if len(pcm_int16) / sample_rate < self._MIN_DURATION_S:
            return ""

        # faster-whisper accepts a float32 numpy array at 16 kHz directly.
        audio_f32 = pcm_int16.astype(np.float32) / 32768.0
        if sample_rate != 16000:
            # Simple resample via numpy if needed (rare — VoiceCode captures at 16 kHz).
            from fractions import Fraction
            from math import gcd
            g = gcd(sample_rate, 16000)
            up = 16000 // g
            down = sample_rate // g
            audio_f32 = _resample_linear(audio_f32, up, down)
        segments, _info = self._model.transcribe(
            audio_f32, language="en", beam_size=1, vad_filter=False,
            initial_prompt=WAKE_WORD_PROMPT,
        )
        return " ".join(s.text.strip() for s in segments).strip()


def _resample_linear(audio: np.ndarray, up: int, down: int) -> np.ndarray:
    if up == 1 and down == 1:
        return audio
    # Linear interpolation resampler — fine for speech; not studio-grade.
    x = np.arange(len(audio))
    x_new = np.arange(0, len(audio) * up, down) / up
    return np.interp(x_new, x, audio).astype(np.float32)


# ───────────────────── Cloud: OpenAI Whisper ──────────────────────


class OpenAIWhisperBackend(STTBackend):
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "whisper-1",
    ) -> None:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed. Install with `pip install openai`."
            ) from exc
        key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env or export it."
            )
        self._client = OpenAI(api_key=key)
        self._model = model

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        import soundfile as sf
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            sf.write(str(wav_path), pcm_int16, sample_rate, subtype="PCM_16")
            with open(wav_path, "rb") as fh:
                result = self._client.audio.transcriptions.create(
                    model=self._model,
                    file=fh,
                    language="en",
                )
            return (result.text or "").strip()
        finally:
            wav_path.unlink(missing_ok=True)


# ─────────────────── Cloud: Azure OpenAI Whisper ──────────────────


class AzureWhisperBackend(STTBackend):
    name = "azure"

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        deployment: str | None = None,
        api_version: str = "2024-06-01",
    ) -> None:
        try:
            from openai import AzureOpenAI  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "openai package (>=1.0) not installed. Install with `pip install openai`."
            ) from exc
        endpoint = (endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")).strip()
        api_key = (api_key or os.environ.get("AZURE_OPENAI_KEY", "")).strip()
        deployment = (deployment or os.environ.get("AZURE_WHISPER_DEPLOYMENT", "whisper")).strip()
        missing = [
            n for n, v in (
                ("AZURE_OPENAI_ENDPOINT", endpoint),
                ("AZURE_OPENAI_KEY", api_key),
            ) if not v
        ]
        if missing:
            raise RuntimeError(
                f"Azure STT requires these env vars: {', '.join(missing)}. "
                "Add them to .env or export."
            )
        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
        self._deployment = deployment

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        import soundfile as sf
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            sf.write(str(wav_path), pcm_int16, sample_rate, subtype="PCM_16")
            with open(wav_path, "rb") as fh:
                result = self._client.audio.transcriptions.create(
                    model=self._deployment,
                    file=fh,
                    language="en",
                )
            return (result.text or "").strip()
        finally:
            wav_path.unlink(missing_ok=True)


# ───────────────────── Shared HTTP helpers ────────────────────────


def _wav_bytes(pcm_int16: np.ndarray, sample_rate: int) -> bytes:
    """Encode a PCM-16 numpy array to an in-memory WAV blob."""
    import io
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, pcm_int16, sample_rate, subtype="PCM_16", format="WAV")
    return buf.getvalue()


def _multipart(
    boundary: str,
    fields: dict,
    file_field: str,
    file_bytes: bytes,
    filename: str = "audio.wav",
    mime: str = "audio/wav",
) -> bytes:
    """Build a multipart/form-data body."""
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; "
        f'name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def _http_post(url: str, body: bytes, headers: dict, timeout: int = 60) -> dict:
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code}: {exc.read().decode(errors='replace')}"
        ) from exc


def _http_get(url: str, headers: dict, timeout: int = 30) -> dict:
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code}: {exc.read().decode(errors='replace')}"
        ) from exc


# ─────────────────── Cloud: 60dB.ai STT ──────────────────────────


class SixtyDBBackend(STTBackend):
    """60dB.ai cloud STT — non-hallucinating, 39 languages, <300 ms latency.

    Sends a WAV file to ``POST https://api.60db.ai/stt`` and returns the
    plain transcript text.  No extra packages needed beyond the stdlib.

    Requires: ``SIXTYDB_API_KEY`` in env / .env.
    """

    name = "60db"
    _ENDPOINT = "https://api.60db.ai/stt"

    def __init__(
        self,
        api_key: str | None = None,
        language: str = "en",
    ) -> None:
        key = (api_key or os.environ.get("SIXTYDB_API_KEY", "")).strip()
        if not key:
            raise RuntimeError(
                "SIXTYDB_API_KEY is not set. Add it to .env or export it."
            )
        self._api_key = key
        self._language = language

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        boundary = "micoracle60db"
        body = _multipart(
            boundary,
            {"language": self._language},
            "file",
            _wav_bytes(pcm_int16, sample_rate),
        )
        data = _http_post(
            self._ENDPOINT,
            body,
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        return " ".join((data.get("text") or "").split())


# ─────────────── Cloud: ElevenLabs Scribe ────────────────────────


class ElevenLabsBackend(STTBackend):
    """ElevenLabs Scribe STT — high accuracy, 99 languages.

    Requires: ``ELEVENLABS_API_KEY`` in env / .env.
    """

    name = "elevenlabs"
    _ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "scribe_v2",
        language: str = "en",
    ) -> None:
        key = (api_key or os.environ.get("ELEVENLABS_API_KEY", "")).strip()
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set. Add it to .env or export it.")
        self._api_key = key
        self._model = model
        self._language = language

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        boundary = "micoracle_el"
        body = _multipart(
            boundary,
            {"model_id": self._model, "language_code": self._language},
            "file",
            _wav_bytes(pcm_int16, sample_rate),
        )
        data = _http_post(
            self._ENDPOINT,
            body,
            {
                "xi-api-key": self._api_key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        return " ".join((data.get("text") or "").split())


# ─────────────── Cloud: Deepgram Nova ────────────────────────────


class DeepgramBackend(STTBackend):
    """Deepgram Nova-2/3 — streaming-grade accuracy, <300 ms.

    Sends raw WAV bytes (not multipart) to the pre-recorded endpoint.
    Requires: ``DEEPGRAM_API_KEY`` in env / .env.
    """

    name = "deepgram"
    _BASE = "https://api.deepgram.com/v1/listen"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "nova-2",
        language: str = "en",
    ) -> None:
        key = (api_key or os.environ.get("DEEPGRAM_API_KEY", "")).strip()
        if not key:
            raise RuntimeError("DEEPGRAM_API_KEY is not set. Add it to .env or export it.")
        self._api_key = key
        self._model = model
        self._language = language

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        url = (
            f"{self._BASE}?model={self._model}"
            f"&language={self._language}&smart_format=true"
        )
        data = _http_post(
            url,
            _wav_bytes(pcm_int16, sample_rate),
            {
                "Authorization": f"Token {self._api_key}",
                "Content-Type": "audio/wav",
            },
        )
        try:
            text = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError):
            text = ""
        return " ".join((text or "").split())


# ─────────────── Cloud: AssemblyAI ───────────────────────────────


class AssemblyAIBackend(STTBackend):
    """AssemblyAI Universal-2 — upload → transcript → poll.

    Requires: ``ASSEMBLYAI_API_KEY`` in env / .env.
    """

    name = "assemblyai"
    _BASE = "https://api.assemblyai.com"

    def __init__(
        self,
        api_key: str | None = None,
        language_code: str = "en",
    ) -> None:
        key = (api_key or os.environ.get("ASSEMBLYAI_API_KEY", "")).strip()
        if not key:
            raise RuntimeError("ASSEMBLYAI_API_KEY is not set. Add it to .env or export it.")
        self._api_key = key
        self._language = language_code

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        import json
        import time

        auth = {"Authorization": self._api_key}

        # 1 — upload raw WAV
        upload = _http_post(
            f"{self._BASE}/v2/upload",
            _wav_bytes(pcm_int16, sample_rate),
            {**auth, "Content-Type": "application/octet-stream"},
        )

        # 2 — create transcript job
        body = json.dumps({
            "audio_url": upload["upload_url"],
            "language_code": self._language,
        }).encode()
        created = _http_post(
            f"{self._BASE}/v2/transcript",
            body,
            {**auth, "Content-Type": "application/json"},
        )
        tid = created["id"]

        # 3 — poll until done
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = _http_get(f"{self._BASE}/v2/transcript/{tid}", auth)
            status = result.get("status")
            if status == "completed":
                return " ".join((result.get("text") or "").split())
            if status == "error":
                raise RuntimeError(f"AssemblyAI error: {result.get('error')}")
            time.sleep(1.5)
        raise RuntimeError("AssemblyAI transcript timed out after 120 s.")


# ─────────────── Cloud: Groq Whisper ─────────────────────────────


class GroqWhisperBackend(STTBackend):
    """Groq-hosted Whisper — OpenAI-compatible endpoint, very low latency.

    Model default: ``whisper-large-v3-turbo``.
    Requires: ``GROQ_API_KEY`` in env / .env.
    """

    name = "groq"
    _ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "whisper-large-v3-turbo",
        language: str = "en",
    ) -> None:
        key = (api_key or os.environ.get("GROQ_API_KEY", "")).strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set. Add it to .env or export it.")
        self._api_key = key
        self._model = model
        self._language = language

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        boundary = "micoracle_groq"
        body = _multipart(
            boundary,
            {
                "model": self._model,
                "language": self._language,
                "response_format": "json",
            },
            "file",
            _wav_bytes(pcm_int16, sample_rate),
        )
        data = _http_post(
            self._ENDPOINT,
            body,
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        return " ".join((data.get("text") or "").split())


# ─────────────── Cloud: Gladia ───────────────────────────────────


class GladiaBackend(STTBackend):
    """Gladia v2 — upload → pre-recorded job → poll.

    Requires: ``GLADIA_API_KEY`` in env / .env.
    """

    name = "gladia"
    _BASE = "https://api.gladia.io"

    def __init__(self, api_key: str | None = None) -> None:
        key = (api_key or os.environ.get("GLADIA_API_KEY", "")).strip()
        if not key:
            raise RuntimeError("GLADIA_API_KEY is not set. Add it to .env or export it.")
        self._api_key = key

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        import json
        import time

        auth = {"x-gladia-key": self._api_key}

        # 1 — upload raw WAV
        upload = _http_post(
            f"{self._BASE}/v2/upload",
            _wav_bytes(pcm_int16, sample_rate),
            {**auth, "Content-Type": "audio/wav"},
        )
        audio_url = upload["audio_url"]

        # 2 — create pre-recorded job
        job = _http_post(
            f"{self._BASE}/v2/pre-recorded",
            json.dumps({"audio_url": audio_url}).encode(),
            {**auth, "Content-Type": "application/json"},
        )
        result_url = job["result_url"]

        # 3 — poll until done
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = _http_get(result_url, auth)
            status = result.get("status")
            if status == "done":
                try:
                    text = result["result"]["result"]["transcription"]["full_transcript"]
                except (KeyError, TypeError):
                    text = ""
                return " ".join((text or "").split())
            if status == "error":
                raise RuntimeError(f"Gladia error: {result.get('error')}")
            time.sleep(1.5)
        raise RuntimeError("Gladia transcript timed out after 120 s.")


# ─────────────── Cloud: OpenAI GPT-4o Realtime (WebSocket) ───────


class RealtimeWhisperBackend(STTBackend):
    """OpenAI GPT-4o Realtime transcription via WebSocket.

    Sends a VAD-segmented PCM buffer to the Realtime API and returns the
    final transcript.  Designed to be used only after wake-word detection so
    that continuous-listening audio is never billed.

    Requires: ``pip install websockets``  (and ``OPENAI_API_KEY``).
    """

    name = "realtime"
    _WS_URL = "wss://api.openai.com/v1/realtime"
    _TARGET_SR = 24000  # Realtime API requires 24 kHz PCM16

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-transcribe",
    ) -> None:
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "websockets not installed. Install with `pip install websockets`."
            ) from exc
        key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env or export it."
            )
        self._api_key = key
        self._model = model

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        import asyncio
        return asyncio.run(self._transcribe_async(pcm_int16, sample_rate))

    async def _transcribe_async(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
        import asyncio
        import base64
        import json
        import websockets

        # Resample from 16 kHz (mic default) to 24 kHz (Realtime API requirement)
        if sample_rate != self._TARGET_SR:
            from math import gcd
            g = gcd(sample_rate, self._TARGET_SR)
            audio_f32 = _resample_linear(
                pcm_int16.astype(np.float32) / 32768.0,
                self._TARGET_SR // g,
                sample_rate // g,
            )
            audio = (audio_f32 * 32768.0).clip(-32768, 32767).astype(np.int16)
        else:
            audio = pcm_int16

        url = f"{self._WS_URL}?model={self._model}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        transcript = ""
        async with websockets.connect(url, additional_headers=headers, open_timeout=15) as ws:
            # Wait for session.created before sending anything
            _session_event = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

            # Disable server VAD — we commit the already-segmented buffer manually
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "input_audio_format": "pcm16",
                    "input_audio_transcription": {
                        "model": self._model,
                        "language": "en",
                    },
                    "turn_detection": None,
                },
            }))

            # Send the complete buffer then commit to signal end of turn
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(audio.tobytes()).decode(),
            }))
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

            # Ask the model to produce a response (triggers transcription)
            await ws.send(json.dumps({"type": "response.create"}))

            # Collect events until transcription arrives or timeout
            timeout = max(30.0, len(audio) / self._TARGET_SR * 3)
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    break
                event = json.loads(raw)
                etype = event.get("type", "")
                if etype == "conversation.item.input_audio_transcription.completed":
                    transcript = event.get("transcript", "").strip()
                    break
                if etype == "response.done":
                    # Fallback: extract transcript from response output items
                    for item in event.get("response", {}).get("output", []):
                        for content in item.get("content", []):
                            if content.get("type") == "input_audio":
                                transcript = content.get("transcript", "").strip()
                    break
                if etype == "error":
                    err = event.get("error", {})
                    raise RuntimeError(
                        f"Realtime API error: {err.get('message', str(event))}"
                    )

        return " ".join(transcript.split())


# ───────────────────────── Factory / auto ─────────────────────────


@dataclass
class STTConfig:
    backend: str = "auto"
    mlx_repo: str = "mlx-community/whisper-medium.en-mlx-4bit"
    faster_model: str = "small.en"
    faster_device: str = "auto"
    faster_compute_type: str = "int8"
    openai_api_key: str | None = None
    openai_model: str = "whisper-1"
    azure_endpoint: str | None = None
    azure_api_key: str | None = None
    azure_deployment: str | None = None
    realtime_api_key: str | None = None
    realtime_model: str = "gpt-4o-transcribe"
    sixtydb_api_key: str | None = None
    sixtydb_language: str = "en"
    elevenlabs_api_key: str | None = None
    elevenlabs_model: str = "scribe_v2"
    elevenlabs_language: str = "en"
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-2"
    deepgram_language: str = "en"
    assemblyai_api_key: str | None = None
    assemblyai_language: str = "en"
    groq_api_key: str | None = None
    groq_model: str = "whisper-large-v3-turbo"
    groq_language: str = "en"
    gladia_api_key: str | None = None


def auto_select_stt_backend() -> str:
    """Choose a sensible default backend for the current platform."""
    if sys.platform == "darwin" and platform.machine() in ("arm64", "aarch64"):
        return "mlx"
    return "faster"


def make_stt_backend(config: STTConfig) -> STTBackend:
    backend = config.backend.lower()
    if backend == "auto":
        backend = auto_select_stt_backend()

    if backend in ("mlx", "mlx-whisper"):
        return MLXWhisperBackend(repo=config.mlx_repo)
    if backend in ("faster", "faster-whisper"):
        return FasterWhisperBackend(
            model=config.faster_model,
            device=config.faster_device,
            compute_type=config.faster_compute_type,
        )
    if backend == "openai":
        return OpenAIWhisperBackend(
            api_key=config.openai_api_key, model=config.openai_model,
        )
    if backend == "azure":
        return AzureWhisperBackend(
            endpoint=config.azure_endpoint,
            api_key=config.azure_api_key,
            deployment=config.azure_deployment,
        )
    if backend == "realtime":
        return RealtimeWhisperBackend(
            api_key=config.realtime_api_key,
            model=config.realtime_model,
        )
    if backend in ("60db", "sixtydb"):
        return SixtyDBBackend(
            api_key=config.sixtydb_api_key,
            language=config.sixtydb_language,
        )
    if backend in ("elevenlabs", "eleven", "scribe"):
        return ElevenLabsBackend(
            api_key=config.elevenlabs_api_key,
            model=config.elevenlabs_model,
            language=config.elevenlabs_language,
        )
    if backend in ("deepgram", "nova"):
        return DeepgramBackend(
            api_key=config.deepgram_api_key,
            model=config.deepgram_model,
            language=config.deepgram_language,
        )
    if backend in ("assemblyai", "assembly"):
        return AssemblyAIBackend(
            api_key=config.assemblyai_api_key,
            language_code=config.assemblyai_language,
        )
    if backend in ("groq",):
        return GroqWhisperBackend(
            api_key=config.groq_api_key,
            model=config.groq_model,
            language=config.groq_language,
        )
    if backend in ("gladia",):
        return GladiaBackend(api_key=config.gladia_api_key)
    raise ValueError(
        f"Unknown STT backend: {backend!r}. "
        "Valid: mlx, faster, openai, azure, realtime, "
        "60db, elevenlabs, deepgram, assemblyai, groq, gladia, auto"
    )
