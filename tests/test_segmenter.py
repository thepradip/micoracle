"""Integration harness for the VAD segmenter.

The segmenter's state machine is the trickiest piece of the system. These
tests inject a mock VAD whose ``is_speech`` responses are scripted frame by
frame, letting us simulate any audio pattern (long silence, short blips,
trailing tail, hard cap) without a real microphone.

Frame payloads carry a per-frame marker value so we can assert exactly which
frames end up in each emitted utterance (preroll preservation, etc.).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from segmenter import VADSegmenter


FRAME_SAMPLES = 480  # 30 ms @ 16 kHz


def _silent_frame() -> np.ndarray:
    return np.zeros(FRAME_SAMPLES, dtype=np.int16)


def _marker_frame(value: int) -> np.ndarray:
    """Produce a frame whose samples all equal ``value`` — trivially trackable."""
    return np.full(FRAME_SAMPLES, value, dtype=np.int16)


def _vad_with_script(script: list[bool]) -> MagicMock:
    vad = MagicMock()
    vad.is_speech = MagicMock(side_effect=list(script))
    return vad


def _run(seg: VADSegmenter, frames: list[np.ndarray]) -> list[np.ndarray]:
    out: list[np.ndarray] = []
    for f in frames:
        pcm = seg.process_frame(f)
        if pcm is not None:
            out.append(pcm)
    return out


class TestPureSilence:
    def test_no_utterances_emitted(self):
        script = [False] * 20
        seg = VADSegmenter(
            vad=_vad_with_script(script),
            preroll_frames=5,
            min_speech_frames=4,
            max_silence_frames=10,
        )
        utts = _run(seg, [_silent_frame() for _ in script])
        assert utts == []
        assert not seg.in_speech


class TestShortBlipBelowThreshold:
    def test_two_speech_frames_dont_lock_in(self):
        # 2 True frames, below min_speech_frames=4
        script = [False, False, True, True, False] + [False] * 20
        seg = VADSegmenter(
            vad=_vad_with_script(script),
            preroll_frames=3,
            min_speech_frames=4,
            max_silence_frames=10,
        )
        utts = _run(seg, [_silent_frame() for _ in script])
        assert utts == []
        assert not seg.in_speech
        assert seg.speech_run == 0  # decayed back to zero during trailing silence


class TestFullUtterance:
    def test_speech_then_silence_emits_one_utterance(self):
        # 4 speech frames (lock-in), 2 more speech, 10 silence (end).
        script = [True] * 4 + [True] * 2 + [False] * 10
        seg = VADSegmenter(
            vad=_vad_with_script(script),
            preroll_frames=3,
            min_speech_frames=4,
            max_silence_frames=10,
        )
        frames = [_marker_frame(i) for i in range(len(script))]
        utts = _run(seg, frames)
        assert len(utts) == 1

    def test_post_utterance_state_clean(self):
        script = [True] * 4 + [False] * 10
        seg = VADSegmenter(
            vad=_vad_with_script(script),
            preroll_frames=3,
            min_speech_frames=4,
            max_silence_frames=10,
        )
        _run(seg, [_silent_frame() for _ in script])
        assert not seg.in_speech
        assert seg.speech_run == 0
        assert seg.silence_run == 0
        assert seg.utterance_length_frames == 0


class TestPreroll:
    def test_preroll_frames_included_in_utterance(self):
        # Lock-in at frame idx 5 (after 4 True). With preroll_frames=3, utterance
        # should start at frame 3 (since preroll keeps the last 3 frames seen).
        script = [False, False, True, True, True, True] + [False] * 20
        seg = VADSegmenter(
            vad=_vad_with_script(script),
            preroll_frames=3,
            min_speech_frames=4,
            max_silence_frames=10,
        )
        frames = [_marker_frame(i) for i in range(len(script))]
        utts = _run(seg, frames)
        assert len(utts) == 1
        first_sample = int(utts[0][0])
        assert first_sample == 3, (
            f"expected utterance to start at marker 3 (preroll tail), got {first_sample}"
        )

    def test_preroll_with_zero_size_still_emits(self):
        script = [True] * 4 + [False] * 10
        seg = VADSegmenter(
            vad=_vad_with_script(script),
            preroll_frames=0,
            min_speech_frames=4,
            max_silence_frames=10,
        )
        utts = _run(seg, [_silent_frame() for _ in script])
        assert len(utts) == 1


class TestMaxUtteranceCap:
    def test_hard_cap_forces_emission(self):
        # Never stops speaking — must be cut off at max_utterance_frames.
        script = [True] * 50
        seg = VADSegmenter(
            vad=_vad_with_script(script),
            preroll_frames=2,
            min_speech_frames=4,
            max_silence_frames=100,   # big so silence never triggers
            max_utterance_frames=20,  # cap kicks in first
        )
        utts = _run(seg, [_silent_frame() for _ in script])
        assert len(utts) >= 1
        for u in utts:
            assert len(u) <= 20 * FRAME_SAMPLES


class TestVADErrors:
    def test_vad_exception_is_swallowed(self):
        vad = MagicMock()
        vad.is_speech = MagicMock(side_effect=Exception("boom"))
        seg = VADSegmenter(vad=vad)
        for _ in range(5):
            assert seg.process_frame(_silent_frame()) is None
        assert not seg.in_speech


class TestInterleavedSpeech:
    def test_brief_dip_does_not_end_utterance(self):
        # Lock in, then a short silence (< max_silence_frames) followed by more
        # speech. Should still produce ONE utterance ending only at the final tail.
        script = (
            [True] * 4           # lock in
            + [True, True]       # continued speech
            + [False] * 5        # short silence (below max_silence=10)
            + [True] * 3         # speech resumes
            + [False] * 10       # long silence — ends utterance
        )
        seg = VADSegmenter(
            vad=_vad_with_script(script),
            preroll_frames=2,
            min_speech_frames=4,
            max_silence_frames=10,
        )
        utts = _run(seg, [_silent_frame() for _ in script])
        assert len(utts) == 1
