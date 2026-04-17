"""VAD-driven utterance segmenter.

Extracted from the main loop so the state machine can be unit-tested without
a live microphone or webrtcvad dependency (a mock VAD can be injected).

Usage inside the main loop::

    segmenter = VADSegmenter()
    while True:
        frame = audio_q.get()
        pcm = segmenter.process_frame(frame)
        if pcm is not None:
            utterance_q.put(pcm)
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

import numpy as np


DEFAULT_SAMPLE_RATE = 16000
DEFAULT_PREROLL_FRAMES = 10          # ~300 ms at 30 ms frames
DEFAULT_MIN_SPEECH_FRAMES = 4        # ~120 ms to lock in speech
DEFAULT_MAX_SILENCE_FRAMES = 28      # ~840 ms of silence ends the utterance
DEFAULT_MAX_UTTERANCE_FRAMES = 600   # ~18 s cap per utterance


class VADLike(Protocol):
    """Minimal protocol for anything exposing ``is_speech(bytes, sr) -> bool``."""

    def is_speech(self, frame_bytes: bytes, sample_rate: int) -> bool: ...


class VADSegmenter:
    """Stream-in-one-frame-at-a-time VAD segmenter.

    Invariants:
      * When idle, maintains a rolling preroll buffer so wake words spoken
        before VAD locks in are preserved in the final utterance.
      * Lock-in requires ``min_speech_frames`` consecutive-ish speech frames;
        the count decays on silence to avoid false triggers on short pops.
      * Utterance ends on ``max_silence_frames`` of trailing silence OR when
        it reaches ``max_utterance_frames`` (hard cap).
    """

    def __init__(
        self,
        vad: VADLike,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        preroll_frames: int = DEFAULT_PREROLL_FRAMES,
        min_speech_frames: int = DEFAULT_MIN_SPEECH_FRAMES,
        max_silence_frames: int = DEFAULT_MAX_SILENCE_FRAMES,
        max_utterance_frames: int = DEFAULT_MAX_UTTERANCE_FRAMES,
    ) -> None:
        self._vad = vad
        self._sample_rate = sample_rate
        self._min_speech = min_speech_frames
        self._max_silence = max_silence_frames
        self._max_utterance = max_utterance_frames

        self._preroll: deque[np.ndarray] = deque(maxlen=preroll_frames)
        self._utterance: list[np.ndarray] = []
        self._speech_run = 0
        self._silence_run = 0
        self._in_speech = False

    def process_frame(self, frame: np.ndarray) -> np.ndarray | None:
        """Feed one PCM16 mono frame.

        Returns the completed utterance as a concatenated int16 numpy array
        when one has just ended, otherwise ``None``.
        """
        self._preroll.append(frame)
        try:
            is_speech = bool(self._vad.is_speech(frame.tobytes(), self._sample_rate))
        except Exception:
            return None

        if self._in_speech:
            self._utterance.append(frame)
            if is_speech:
                self._silence_run = 0
            else:
                self._silence_run += 1
            if (self._silence_run >= self._max_silence
                    or len(self._utterance) >= self._max_utterance):
                pcm = np.concatenate(self._utterance)
                self._reset()
                return pcm
            return None

        if is_speech:
            self._speech_run += 1
            if self._speech_run >= self._min_speech:
                self._in_speech = True
                self._utterance = list(self._preroll)
                self._silence_run = 0
        else:
            self._speech_run = max(0, self._speech_run - 1)
        return None

    def _reset(self) -> None:
        self._utterance = []
        self._in_speech = False
        self._silence_run = 0
        self._speech_run = 0

    # ——— test / introspection helpers ———

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def speech_run(self) -> int:
        return self._speech_run

    @property
    def silence_run(self) -> int:
        return self._silence_run

    @property
    def utterance_length_frames(self) -> int:
        return len(self._utterance)
