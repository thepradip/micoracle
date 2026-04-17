"""Speech-to-text backends.

Four concrete backends plus a factory with OS-aware auto-selection:

- ``mlx``     — MLX Whisper, Apple Silicon only, fastest on-device
- ``faster``  — faster-whisper (CTranslate2), cross-platform local
- ``openai``  — OpenAI Whisper cloud API
- ``azure``   — Azure OpenAI Whisper cloud API

All backends share the same ``transcribe`` signature and can be swapped at
runtime via ``--stt-backend`` or ``VOICE_AGENT_STT_BACKEND``. Heavy deps are
imported lazily inside each backend so users only need packages they actually
use.
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


class MLXWhisperBackend(STTBackend):
    name = "mlx"

    def __init__(self, repo: str = "mlx-community/whisper-medium.en-mlx-4bit") -> None:
        if sys.platform != "darwin" or platform.machine() not in ("arm64", "aarch64"):
            raise RuntimeError(
                "MLX Whisper runs only on macOS with Apple Silicon. "
                "Use --stt-backend faster for other platforms."
            )
        self.repo = repo

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
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

    def transcribe(self, pcm_int16: np.ndarray, sample_rate: int) -> str:
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
    raise ValueError(
        f"Unknown STT backend: {backend!r}. Valid: mlx, faster, openai, azure, auto"
    )
