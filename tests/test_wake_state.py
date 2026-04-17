"""Unit tests for WakeState (two-step follow-up timer)."""

from __future__ import annotations

import time

from hands_free_voice import WakeState


class TestWakeState:
    def test_initially_no_active_backend(self):
        assert WakeState().active_backend() is None

    def test_arm_returns_backend(self):
        state = WakeState()
        state.arm("codex", timeout_secs=5.0)
        assert state.active_backend() == "codex"

    def test_clear_deactivates(self):
        state = WakeState()
        state.arm("codex", timeout_secs=5.0)
        state.clear()
        assert state.active_backend() is None

    def test_expiry(self):
        state = WakeState()
        state.arm("codex", timeout_secs=0.001)
        time.sleep(0.02)
        assert state.active_backend() is None

    def test_rearm_resets_timer(self):
        state = WakeState()
        state.arm("codex", timeout_secs=0.001)
        time.sleep(0.02)
        # Now expired — re-arm should bring it back.
        state.arm("codex", timeout_secs=5.0)
        assert state.active_backend() == "codex"

    def test_arm_switches_backend(self):
        state = WakeState()
        state.arm("codex", timeout_secs=5.0)
        state.arm("claude", timeout_secs=5.0)
        assert state.active_backend() == "claude"
