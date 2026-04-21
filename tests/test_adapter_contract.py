"""Contract tests: capture the exact shell argv each platform adapter emits.

These are *not* end-to-end tests (no real keystrokes happen). They mock
``subprocess.run`` (and Windows's Python APIs) and assert that the adapter
calls the right tools with the right arguments in the right order.

If a real-world tool changes its CLI (e.g. xdotool adds a required flag),
these tests stay green but reality breaks — that's why we still need D (real
hardware testing). But if I accidentally reorder ``wtype -M ctrl v -m ctrl``
or drop the ``--selection clipboard`` flag, these tests fail immediately.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, call, patch

import pytest

import platform_adapter as pa


# ─────────────────────────── helpers ─────────────────────────────


def _argv_list(calls) -> list[list[str]]:
    """Extract the first positional arg (argv) from a list of mock call tuples."""
    return [c[0] for c in calls]


def _record_subprocess(mock_responses: dict[str, MagicMock] | None = None):
    """Returns (calls_list, fake_run_func) recording every subprocess.run argv.

    ``mock_responses`` maps "first-command-token" to a MagicMock returned when
    that command is called (lets us control pbpaste / xclip / xdotool search
    output per test). Unlisted commands get a returncode=0 success mock.
    """
    calls: list[tuple[list[str], dict]] = []

    def fake_run(*args, **kwargs):
        argv = list(args[0])
        calls.append((argv, dict(kwargs)))
        head = argv[0] if argv else ""
        if mock_responses and head in mock_responses:
            resp = mock_responses[head]
            # MagicMock instances are themselves callable, so `callable(resp)`
            # returns True for them. Only *invoke* resp if it's a plain function
            # (lambda or def) — otherwise treat it as the response to return.
            if callable(resp) and not isinstance(resp, MagicMock):
                return resp(argv)
            return resp
        return MagicMock(returncode=0, stdout="", stderr="")

    return calls, fake_run


# ─────────────────────────── macOS ───────────────────────────────


class TestMacAdapterContract:
    def test_dispatch_argv_sequence(self):
        adapter = pa.MacAdapter()
        calls, fake_run = _record_subprocess({
            "pbpaste": MagicMock(returncode=0, stdout="ORIGINAL", stderr=""),
        })
        with patch("subprocess.run", fake_run):
            adapter.paste_and_return("hello world", "Terminal")

        argvs = _argv_list(calls)

        # 1. Read existing clipboard.
        assert argvs[0] == ["pbpaste"]
        # 2. Overwrite clipboard with our payload (stdin = "hello world").
        assert argvs[1] == ["pbcopy"]
        assert calls[1][1]["input"] == "hello world"
        # 3. osascript call with our dispatch script.
        assert argvs[2][:2] == ["osascript", "-e"]
        script = argvs[2][2]
        assert 'tell application "Terminal" to activate' in script
        assert 'keystroke "v" using command down' in script
        assert "key code 36" in script  # Return
        # 4. Restore original clipboard (last pbcopy gets the original content).
        last = calls[-1]
        assert last[0] == ["pbcopy"]
        assert last[1]["input"] == "ORIGINAL"

    def test_clipboard_restored_even_when_dispatch_fails(self):
        adapter = pa.MacAdapter()
        # osascript always fails → dispatch raises after retries.
        calls, fake_run = _record_subprocess({
            "pbpaste": MagicMock(returncode=0, stdout="KEEPME", stderr=""),
            "osascript": MagicMock(returncode=1, stdout="", stderr="boom"),
        })
        with patch("subprocess.run", fake_run):
            with pytest.raises(RuntimeError, match="AppleScript dispatch failed"):
                adapter.paste_and_return("payload", "Terminal")

        # Clipboard restore must have happened despite the failure (finally block).
        last = calls[-1]
        assert last[0] == ["pbcopy"]
        assert last[1]["input"] == "KEEPME"

    def test_dispatch_retries_on_transient_failure(self):
        adapter = pa.MacAdapter()
        attempts = {"n": 0}

        def osascript_response(argv):
            attempts["n"] += 1
            # Fail on first attempt, succeed on second.
            if attempts["n"] == 1:
                return MagicMock(returncode=1, stdout="", stderr="transient")
            return MagicMock(returncode=0, stdout="", stderr="")

        calls, fake_run = _record_subprocess({
            "pbpaste": MagicMock(returncode=0, stdout="", stderr=""),
            "osascript": osascript_response,
        })
        with patch("subprocess.run", fake_run):
            adapter.paste_and_return("x", "Terminal")

        osascript_calls = [c for c in calls if c[0][0] == "osascript"]
        assert len(osascript_calls) == 2, "expected one retry after transient failure"


# ───────────────────────── Linux X11 ─────────────────────────────


class TestLinuxX11AdapterContract:
    def _make_adapter(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        return pa.LinuxAdapter()

    def test_dispatch_argv_sequence(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch)
        calls, fake_run = _record_subprocess()
        with patch("subprocess.run", fake_run):
            adapter.paste_and_return("hi there", "gnome-terminal")

        argvs = _argv_list(calls)

        # 1. Type the payload directly; no xclip, no clipboard stash.
        type_calls = [
            a for a in argvs if a[:3] == ["xdotool", "type", "--clearmodifiers"]
        ]
        assert len(type_calls) == 1, f"expected exactly one xdotool type call. argvs={argvs}"
        assert type_calls[0][-1] == "hi there", "last arg must be the payload"
        assert "--" in type_calls[0], "payload must be separated by -- to avoid flag parsing"
        assert "--delay" in type_calls[0]
        # 2. Enter via xdotool key Return.
        assert ["xdotool", "key", "--clearmodifiers", "Return"] in argvs
        # 3. xclip must NOT be called — X11 path never touches the clipboard.
        assert not any(a and a[0] == "xclip" for a in argvs)

    def test_no_window_activation_attempted(self, monkeypatch):
        """Paste must target the currently-focused window; no search/activate."""
        adapter = self._make_adapter(monkeypatch)
        calls, fake_run = _record_subprocess()
        with patch("subprocess.run", fake_run):
            adapter.paste_and_return("x", "some-label")

        argvs = _argv_list(calls)
        assert not any(a[:2] == ["xdotool", "search"] for a in argvs)
        assert not any(a[:2] == ["xdotool", "windowactivate"] for a in argvs)

    def test_xdotool_type_failure_raises(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch)
        calls, fake_run = _record_subprocess({
            "xdotool": lambda argv: (
                MagicMock(returncode=1, stdout="", stderr="boom")
                if len(argv) > 1 and argv[1] == "type"
                else MagicMock(returncode=0, stdout="", stderr="")
            ),
        })
        with patch("subprocess.run", fake_run):
            with pytest.raises(RuntimeError, match="xdotool type failed"):
                adapter.paste_and_return("hello", "some-label")

    def test_empty_text_skips_typing(self, monkeypatch):
        """Whitespace-only / empty text must not invoke xdotool at all."""
        adapter = self._make_adapter(monkeypatch)
        calls, fake_run = _record_subprocess()
        with patch("subprocess.run", fake_run):
            adapter.paste_and_return("   \n  ", "some-label")

        assert calls == [], f"expected no subprocess calls for empty input; got {calls}"


# ─────────────────────── Linux Wayland ───────────────────────────


class TestLinuxWaylandAdapterContract:
    def _make_adapter(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        return pa.LinuxAdapter()

    def test_dispatch_argv_sequence(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch)
        calls, fake_run = _record_subprocess({
            "wl-paste": MagicMock(returncode=0, stdout="OLD", stderr=""),
        })
        with patch("subprocess.run", fake_run):
            adapter.paste_and_return("hi", "any-app")

        argvs = _argv_list(calls)

        # Wayland uses wl-paste/wl-copy instead of xclip.
        assert ["wl-paste"] in argvs
        assert ["wl-copy"] in argvs
        # wtype is how we send the Ctrl+V chord on Wayland.
        # Pattern: press ctrl, type 'v', release ctrl.
        assert ["wtype", "-M", "ctrl", "v", "-m", "ctrl"] in argvs
        # Return key.
        assert ["wtype", "-k", "Return"] in argvs

    def test_wayland_skips_window_activation(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch)
        calls, fake_run = _record_subprocess()
        with patch("subprocess.run", fake_run):
            adapter.paste_and_return("hi", "any-app")

        argvs = _argv_list(calls)
        # No xdotool / wmctrl / etc. — Wayland blocks external focus control.
        assert not any("xdotool" in a[0] for a in argvs if a)
        assert not any("wmctrl" in a[0] for a in argvs if a)


# ─────────────────────── Windows ─────────────────────────────────


class TestWindowsAdapterContract:
    def _fake_modules(self):
        fake_pyperclip = MagicMock()
        fake_pyperclip.paste = MagicMock(return_value="OLD_CLIP")
        fake_pyperclip.copy = MagicMock()
        fake_pyautogui = MagicMock()
        fake_pyautogui.FAILSAFE = True  # will be set False by adapter
        fake_pyautogui.hotkey = MagicMock()
        fake_pyautogui.press = MagicMock()
        fake_win32gui = MagicMock()
        fake_win32gui.EnumWindows = MagicMock()
        fake_win32gui.IsWindowVisible = MagicMock(return_value=True)
        fake_win32gui.SetForegroundWindow = MagicMock()
        fake_win32gui.GetForegroundWindow = MagicMock(return_value=42)
        fake_win32process = MagicMock()
        fake_win32process.GetWindowThreadProcessId = MagicMock(return_value=(1, 1234))
        fake_psutil = MagicMock()
        mock_proc = MagicMock()
        mock_proc.name = MagicMock(return_value="WindowsTerminal.exe")
        fake_psutil.Process = MagicMock(return_value=mock_proc)
        return {
            "pyperclip": fake_pyperclip,
            "pyautogui": fake_pyautogui,
            "win32gui": fake_win32gui,
            "win32process": fake_win32process,
            "psutil": fake_psutil,
        }

    def test_dispatch_call_sequence(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        fakes = self._fake_modules()
        with patch.dict(sys.modules, fakes):
            adapter = pa.WindowsAdapter()
            adapter.paste_and_return("hi there", "WindowsTerminal.exe")

        pyperclip = fakes["pyperclip"]
        pyautogui = fakes["pyautogui"]
        win32gui = fakes["win32gui"]

        # 1. Read clipboard.
        assert pyperclip.paste.called
        # 2. Write payload to clipboard (first copy call).
        first_copy = pyperclip.copy.call_args_list[0]
        assert first_copy == call("hi there")
        # 3. EnumWindows invoked to find the target window.
        assert win32gui.EnumWindows.called
        # 4. Paste chord.
        pyautogui.hotkey.assert_called_with("ctrl", "v")
        # 5. Enter.
        pyautogui.press.assert_called_with("enter")
        # 6. Clipboard restored with original contents (last copy).
        last_copy = pyperclip.copy.call_args_list[-1]
        assert last_copy == call("OLD_CLIP")

    def test_pyautogui_failsafe_disabled(self, monkeypatch):
        """The adapter must disable pyautogui's 'move to corner to abort' feature
        because mic-driven automation can't recover from a stray cursor."""
        monkeypatch.setattr("sys.platform", "win32")
        fakes = self._fake_modules()
        with patch.dict(sys.modules, fakes):
            pa.WindowsAdapter()
        assert fakes["pyautogui"].FAILSAFE is False

    def test_get_frontmost_returns_process_name(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        fakes = self._fake_modules()
        with patch.dict(sys.modules, fakes):
            adapter = pa.WindowsAdapter()
            assert adapter.get_frontmost_app() == "WindowsTerminal.exe"

    def test_clipboard_restored_even_on_dispatch_failure(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        fakes = self._fake_modules()
        fakes["pyautogui"].hotkey.side_effect = Exception("focus stolen")
        with patch.dict(sys.modules, fakes):
            adapter = pa.WindowsAdapter()
            with pytest.raises(Exception, match="focus stolen"):
                adapter.paste_and_return("x", "WindowsTerminal.exe")

        # Restore must have happened.
        last_copy = fakes["pyperclip"].copy.call_args_list[-1]
        assert last_copy == call("OLD_CLIP")
