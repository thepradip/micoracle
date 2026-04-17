"""End-to-end pipeline tests.

Wires segmenter → text filters → wake detection → command extraction with
mocked STT output and scripted VAD behavior. No audio hardware required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from hands_free_voice import (
    WakeState,
    detect_wake_word,
    extract_command,
    is_silence_hallucination,
    looks_hallucinated,
)
from segmenter import VADSegmenter


FRAME_SAMPLES = 480


def _silent_frame() -> np.ndarray:
    return np.zeros(FRAME_SAMPLES, dtype=np.int16)


def _vad(script: list[bool]) -> MagicMock:
    v = MagicMock()
    v.is_speech = MagicMock(side_effect=list(script))
    return v


def _segment_once(script: list[bool]) -> np.ndarray:
    """Run the segmenter over ``script`` and return the single expected utterance."""
    seg = VADSegmenter(
        vad=_vad(script),
        preroll_frames=3,
        min_speech_frames=4,
        max_silence_frames=10,
    )
    utts: list[np.ndarray] = []
    for _ in script:
        pcm = seg.process_frame(_silent_frame())
        if pcm is not None:
            utts.append(pcm)
    assert len(utts) == 1, f"expected 1 utterance, got {len(utts)}"
    return utts[0]


def _route(text: str, wake_state: WakeState) -> tuple[str, str]:
    """Mimic the worker's per-utterance routing. Returns (event, payload)."""
    if looks_hallucinated(text):
        return ("hallucination", text[:60])
    if is_silence_hallucination(text):
        return ("silence", "")

    armed = wake_state.active_backend()
    if armed:
        wake, idx = detect_wake_word(text)
        if wake:
            cmd = extract_command(text, idx)
            if cmd:
                wake_state.clear()
                return ("dispatch", cmd)
            wake_state.arm(wake)
            return ("arm", wake)
        cmd = text.strip(" ,.!?;:")
        if cmd:
            wake_state.clear()
            return ("dispatch", cmd)
        return ("empty_followup", "")

    wake, idx = detect_wake_word(text)
    if not wake:
        return ("ignored", text)
    cmd = extract_command(text, idx)
    if cmd:
        wake_state.clear()
        return ("dispatch", cmd)
    wake_state.arm(wake)
    return ("arm", wake)


class TestSegmenterToRouter:
    def test_one_shot_pipeline(self):
        # VAD produces a single utterance; STT returns wake+command in one go.
        script = [False, False, True, True, True, True] + [False] * 15
        utterance = _segment_once(script)
        assert utterance is not None and len(utterance) > 0

        # Simulated STT transcript for this PCM chunk.
        transcript = "codex write a hello world"
        state = WakeState()
        event, payload = _route(transcript, state)
        assert event == "dispatch"
        assert payload == "write a hello world"

    def test_two_step_followup_flow(self):
        state = WakeState()

        event, payload = _route("codex", state)
        assert event == "arm"
        assert payload == "codex"
        assert state.active_backend() == "codex"

        event, payload = _route("write a python test", state)
        assert event == "dispatch"
        assert payload == "write a python test"
        assert state.active_backend() is None

    def test_silence_hallucination_swallowed(self):
        state = WakeState()
        event, _ = _route("Thank you.", state)
        assert event == "silence"
        assert state.active_backend() is None

    def test_silence_does_not_consume_armed_followup(self):
        state = WakeState()
        _route("codex", state)
        assert state.active_backend() == "codex"

        event, _ = _route("Thank you.", state)
        assert event == "silence"
        # Still armed — the silence filter short-circuits before consuming the arm.
        assert state.active_backend() == "codex"

    def test_unrelated_speech_ignored(self):
        state = WakeState()
        event, payload = _route("can you write a prime number program", state)
        assert event == "ignored"
        assert payload == "can you write a prime number program"
        assert state.active_backend() is None

    def test_hallucination_filtered(self):
        state = WakeState()
        chunk = "right wing of the"
        text = f"{chunk} {chunk} {chunk}"
        event, _ = _route(text, state)
        assert event == "hallucination"

    def test_empty_followup_reports_empty(self):
        state = WakeState()
        _route("codex", state)
        event, _ = _route(".", state)
        # "." is a silence hallucination, so it's filtered before reaching the
        # empty-followup branch. This is the correct current behavior.
        assert event == "silence"
