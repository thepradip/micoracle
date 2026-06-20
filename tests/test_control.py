"""Tests for the computer-control parser (pure) and screenshot execution."""

from __future__ import annotations

import os
import sys

import pytest

import control


class TestParse:
    def test_screenshot_variants(self):
        for cmd in ["take a screenshot", "screenshot", "grab a screen shot",
                    "capture screenshot", "take screenshot now"]:
            assert control.parse(cmd) == control.Intent("screenshot")

    def test_open_app_alias(self):
        assert control.parse("open safari") == control.Intent("open_app", "Safari")
        assert control.parse("open the browser") == control.Intent("open_app", "Safari")
        assert control.parse("open vs code") == control.Intent("open_app", "Visual Studio Code")

    def test_open_unknown_app_titlecased(self):
        assert control.parse("open slack") == control.Intent("open_app", "Slack")

    def test_open_url(self):
        assert control.parse("open github.com") == control.Intent("open_url", "github.com")
        assert control.parse("go to https://example.com") == control.Intent("open_url", "https://example.com")

    def test_web_search(self):
        assert control.parse("search for weather in delhi") == control.Intent("web_search", "weather in delhi")
        assert control.parse("google python asyncio") == control.Intent("web_search", "python asyncio")

    def test_focus_app(self):
        assert control.parse("switch to chrome") == control.Intent("focus_app", "Google Chrome")
        assert control.parse("activate terminal") == control.Intent("focus_app", "Terminal")

    def test_type_text_preserves_case(self):
        assert control.parse("type Hello World") == control.Intent("type_text", "Hello World")

    def test_non_command_returns_none(self):
        assert control.parse("what is the capital of France") is None
        assert control.parse("explain how recursion works") is None
        assert control.parse("") is None

    def test_trailing_punctuation_stripped(self):
        assert control.parse("take a screenshot.") == control.Intent("screenshot")


@pytest.mark.skipif(sys.platform != "darwin", reason="screencapture is macOS-only")
class TestExecuteScreenshot:
    def test_screenshot_creates_file(self, monkeypatch, tmp_path):
        # Point the screenshot at a temp file by faking the home dir path join.
        target = tmp_path / "shot.png"
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(target))
        result = control.execute(control.Intent("screenshot"))
        assert result.kind == "screenshot"
        # On CI without a display screencapture may fail; only assert the file
        # when the command reported success.
        if result.ok:
            assert target.exists()


class TestRoute:
    def test_route_returns_none_for_noncommand(self):
        assert control.route("tell me a joke") is None
