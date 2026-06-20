"""Tests for dynamic dispatch targeting (empty target_app = current frontmost)."""

from __future__ import annotations

import sys

import pytest

import platform_adapter as _pa


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS adapter")
class TestMacDynamic:
    def test_pinned_target_activates_app(self):
        adapter = _pa.MacAdapter()
        script = adapter._build_dispatch_script("Terminal")
        assert 'tell application "Terminal" to activate' in script
        assert 'keystroke "v" using command down' in script

    def test_empty_target_does_not_activate(self):
        adapter = _pa.MacAdapter()
        script = adapter._build_dispatch_script("")
        assert "activate" not in script              # dynamic: no app activation
        assert 'keystroke "v" using command down' in script  # still pastes
        assert "key code 36" in script                       # still presses Return
