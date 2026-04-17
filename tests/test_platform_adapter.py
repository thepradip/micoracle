"""Mocked tests for the platform adapter factory and adapter internals."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

import platform_adapter as pa


class TestFactory:
    def test_darwin_returns_mac(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        adapter = pa.get_platform_adapter()
        assert isinstance(adapter, pa.MacAdapter)

    def test_linux_returns_linux_when_x11_deps_present(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        adapter = pa.get_platform_adapter()
        assert isinstance(adapter, pa.LinuxAdapter)
        assert adapter.display_server == "x11"

    def test_linux_wayland_detected(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        adapter = pa.get_platform_adapter()
        assert isinstance(adapter, pa.LinuxAdapter)
        assert adapter.display_server == "wayland"

    def test_linux_missing_deps_raises(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(RuntimeError, match="X11 dispatch requires"):
            pa.get_platform_adapter()

    def test_win32_returns_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        fake_modules = {
            "pyperclip": MagicMock(),
            "pyautogui": MagicMock(FAILSAFE=False),
            "win32gui": MagicMock(),
            "win32process": MagicMock(),
            "psutil": MagicMock(),
        }
        with patch.dict(sys.modules, fake_modules):
            adapter = pa.get_platform_adapter()
        assert isinstance(adapter, pa.WindowsAdapter)

    def test_unknown_platform_raises(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "freebsd12")
        with pytest.raises(RuntimeError, match="Unsupported platform"):
            pa.get_platform_adapter()


class TestMacAdapter:
    def test_dispatch_script_contains_paste_and_return(self):
        adapter = pa.MacAdapter()
        script = adapter._build_dispatch_script("Terminal")
        assert 'keystroke "v" using command down' in script
        assert "key code 36" in script           # Return key
        assert "Terminal" in script
        assert "activate" in script

    def test_dispatch_script_escapes_double_quotes(self):
        adapter = pa.MacAdapter()
        script = adapter._build_dispatch_script('My "App"')
        # Escaped form for AppleScript: backslash-quote
        assert '\\"App\\"' in script

    def test_dispatch_script_escapes_backslashes(self):
        adapter = pa.MacAdapter()
        script = adapter._build_dispatch_script('path\\to')
        assert 'path\\\\to' in script

    def test_get_frontmost_raises_on_osascript_failure(self):
        adapter = pa.MacAdapter()
        with patch("subprocess.run") as fake_run:
            fake_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="osascript boom",
            )
            with pytest.raises(RuntimeError, match="Unable to detect"):
                adapter.get_frontmost_app()

    def test_get_frontmost_raises_on_empty_output(self):
        adapter = pa.MacAdapter()
        with patch("subprocess.run") as fake_run:
            fake_run.return_value = MagicMock(returncode=0, stdout="\n", stderr="")
            with pytest.raises(RuntimeError, match="Unable to detect"):
                adapter.get_frontmost_app()

    def test_get_frontmost_returns_stripped_name(self):
        adapter = pa.MacAdapter()
        with patch("subprocess.run") as fake_run:
            fake_run.return_value = MagicMock(returncode=0, stdout="Terminal\n", stderr="")
            assert adapter.get_frontmost_app() == "Terminal"


class TestLinuxAdapter:
    def test_wayland_get_frontmost_raises(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        adapter = pa.LinuxAdapter()
        with pytest.raises(RuntimeError, match="not supported on Wayland"):
            adapter.get_frontmost_app()

    def test_x11_get_frontmost_returns_window_title(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        adapter = pa.LinuxAdapter()
        with patch("subprocess.run") as fake_run:
            fake_run.return_value = MagicMock(
                returncode=0, stdout="gnome-terminal-server\n", stderr="",
            )
            assert adapter.get_frontmost_app() == "gnome-terminal-server"
