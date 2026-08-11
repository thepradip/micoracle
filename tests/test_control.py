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


class TestInstalledAppResolution:
    @pytest.fixture(autouse=True)
    def fake_installed(self, monkeypatch):
        monkeypatch.setattr(control, "_installed_cache", [
            ("WhatsApp", "WhatsApp"),
            ("Visual Studio Code", "Visual Studio Code"),
            ("Google Chrome", "Google Chrome"),
        ])

    def test_mishear_aliases_resolve(self):
        # observed Whisper mishears must map to the real app
        assert control.parse("open bots app") == control.Intent("open_app", "WhatsApp")
        assert control.parse("open what's app") == control.Intent("open_app", "WhatsApp")
        assert control.parse("open versus code") == control.Intent(
            "open_app", "Visual Studio Code")

    def test_fuzzy_match_against_installed(self):
        # not in the alias table — close enough to an installed app
        assert control.parse("open whazapp") == control.Intent("open_app", "WhatsApp")

    def test_exact_installed_match_case_insensitive(self):
        assert control._match_installed("google chrome") == "Google Chrome"

    def test_below_threshold_falls_back_to_titlecase(self):
        assert control.parse("open flurbextron") == control.Intent(
            "open_app", "Flurbextron")

    def test_empty_spoken_name_no_match(self):
        assert control._match_installed("") is None


class TestIsCompound:
    def test_two_control_clauses(self):
        assert control.is_compound("open safari and type hello world")
        assert control.is_compound("open the browser and go to github.com")

    def test_then_after_control_action(self):
        assert control.is_compound("take a screenshot then send it to bob")
        assert control.is_compound("open safari and then search for python")

    def test_single_intent_with_and_in_argument(self):
        assert not control.is_compound("search for black and white cats")
        assert not control.is_compound("type peanut butter and jelly")

    def test_plain_chat_not_compound(self):
        assert not control.is_compound("what is the capital of France")
        assert not control.is_compound("tell me a joke and make it short")
        assert not control.is_compound("")

    def test_single_action_not_compound(self):
        assert not control.is_compound("open safari")
        assert not control.is_compound("take a screenshot")


@pytest.mark.skipif(sys.platform != "darwin", reason="screencapture is macOS-only")
class TestExecuteScreenshot:
    def test_screenshot_creates_file(self, monkeypatch, tmp_path):
        # Redirect the screenshot folder to a temp dir via the env override.
        monkeypatch.setenv("MICORACLE_SCREENSHOT_DIR", str(tmp_path))
        result = control.execute(control.Intent("screenshot"))
        assert result.kind == "screenshot"
        assert result.detail.startswith(str(tmp_path))
        # On CI without a display screencapture may fail; only assert the file
        # when the command reported success.
        if result.ok:
            assert list(tmp_path.glob("micoracle-*.png"))


class TestCrossPlatform:
    def test_mac_screenshot_argv(self, monkeypatch):
        monkeypatch.setattr(control.sys, "platform", "darwin")
        assert control._screenshot_argv("/tmp/x.png") == ["screencapture", "-x", "/tmp/x.png"]

    def test_linux_screenshot_first_available_tool(self, monkeypatch):
        monkeypatch.setattr(control.sys, "platform", "linux")
        monkeypatch.setattr(control.shutil, "which",
                            lambda t: "/usr/bin/scrot" if t == "scrot" else None)
        assert control._screenshot_argv("/tmp/x.png") == ["scrot", "/tmp/x.png"]

    def test_linux_screenshot_no_tool(self, monkeypatch):
        monkeypatch.setattr(control.sys, "platform", "linux")
        monkeypatch.setattr(control.shutil, "which", lambda t: None)
        assert control._screenshot_argv("/tmp/x.png") is None
        assert control.take_screenshot("/tmp/x.png") is False

    def test_windows_screenshot_uses_powershell(self, monkeypatch):
        monkeypatch.setattr(control.sys, "platform", "win32")
        argv = control._screenshot_argv("C:/tmp/x.png")
        assert argv[0] == "powershell"
        assert "CopyFromScreen" in argv[-1]

    def test_windows_open_app_uses_start_process(self, monkeypatch):
        monkeypatch.setattr(control.sys, "platform", "win32")
        calls = []
        monkeypatch.setattr(control, "_run", lambda cmd: calls.append(cmd) or True)
        assert control._open_app("notepad") is True
        assert "Start-Process 'notepad'" in calls[0][-1]

    def test_linux_focus_needs_xdotool(self, monkeypatch):
        monkeypatch.setattr(control.sys, "platform", "linux")
        monkeypatch.setattr(control.shutil, "which", lambda t: None)
        assert control._focus_app("firefox") is False

    def test_linux_focus_with_xdotool(self, monkeypatch):
        monkeypatch.setattr(control.sys, "platform", "linux")
        monkeypatch.setattr(control.shutil, "which",
                            lambda t: "/usr/bin/xdotool" if t == "xdotool" else None)
        calls = []
        monkeypatch.setattr(control, "_run", lambda cmd: calls.append(cmd) or True)
        assert control._focus_app("firefox") is True
        assert calls[0][:3] == ["xdotool", "search", "--name"]

    def test_open_url_uses_webbrowser(self, monkeypatch):
        opened = []
        monkeypatch.setattr(control.webbrowser, "open", lambda u: opened.append(u) or True)
        result = control.execute(control.Intent("open_url", "example.com"))
        assert result.ok is True
        assert opened == ["https://example.com"]

    def test_web_search_uses_webbrowser(self, monkeypatch):
        opened = []
        monkeypatch.setattr(control.webbrowser, "open", lambda u: opened.append(u) or True)
        result = control.execute(control.Intent("web_search", "rust lifetimes"))
        assert result.ok is True
        assert "google.com/search?q=rust+lifetimes" in opened[0]


class TestRoute:
    def test_route_returns_none_for_noncommand(self):
        assert control.route("tell me a joke") is None
