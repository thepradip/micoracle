"""Platform-specific dispatch layer.

Abstracts the three OS-level operations VoiceCode needs:

- Detect the currently focused / frontmost application
- Paste text into it, then press Return
- Read and write the system clipboard (to stash/restore user content)

One concrete adapter per OS. Import-time is cheap; heavy deps are loaded lazily
inside each adapter's __init__ so a user who never touches a given platform
never has to install that platform's deps.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod


class PlatformAdapter(ABC):
    """Abstract interface shared by all OS adapters."""

    #: Names the adapter recognizes as "sensible" dispatch targets. Used only
    #: for a startup warning when the user picks something exotic.
    supported_apps: set[str] = set()

    focus_delay_secs: float = 0.10
    paste_delay_secs: float = 0.15
    submit_delay_secs: float = 0.15
    dispatch_retries: int = 2

    @abstractmethod
    def get_frontmost_app(self) -> str:
        """Return the name of the frontmost/focused application."""

    @abstractmethod
    def paste_and_return(self, text: str, target_app: str) -> None:
        """Paste ``text`` into ``target_app`` and press Return.

        Must preserve the user's clipboard contents.
        """


# ───────────────────────────── macOS ──────────────────────────────


class MacAdapter(PlatformAdapter):
    supported_apps = {
        "Terminal", "iTerm2", "Warp", "Cursor",
        "Visual Studio Code", "Claude", "Codex",
    }

    def get_frontmost_app(self) -> str:
        script = (
            'tell application "System Events" to get name of first application process '
            "whose frontmost is true"
        )
        proc = subprocess.run(
            ["osascript", "-e", script],
            text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "Unable to detect the focused app. Grant Accessibility access to "
                f"your terminal. Details: {proc.stderr.strip()}"
            )
        name = proc.stdout.strip()
        if not name:
            raise RuntimeError("Unable to detect the focused app.")
        return name

    def paste_and_return(self, text: str, target_app: str) -> None:
        original = self._read_clipboard()
        try:
            self._write_clipboard(text)
            script = self._build_dispatch_script(target_app)
            last_error = ""
            for attempt in range(1, self.dispatch_retries + 1):
                proc = subprocess.run(
                    ["osascript", "-e", script],
                    text=True, capture_output=True, check=False,
                )
                if proc.returncode == 0:
                    return
                last_error = proc.stderr.strip() or proc.stdout.strip()
                time.sleep(0.2)
            raise RuntimeError(
                f"AppleScript dispatch failed after {self.dispatch_retries} attempts. "
                f"Check Accessibility + Automation permissions. Details: {last_error}"
            )
        finally:
            self._write_clipboard(original)

    def _build_dispatch_script(self, app_name: str) -> str:
        escaped = app_name.replace("\\", "\\\\").replace('"', '\\"')
        return (
            f'tell application "{escaped}" to activate\n'
            f'delay {self.focus_delay_secs}\n'
            'tell application "System Events"\n'
            '    keystroke "v" using command down\n'
            f'    delay {self.paste_delay_secs}\n'
            '    key code 36\n'
            f'    delay {self.submit_delay_secs}\n'
            'end tell\n'
        )

    @staticmethod
    def _read_clipboard() -> str:
        proc = subprocess.run(["pbpaste"], text=True, capture_output=True, check=False)
        return proc.stdout if proc.returncode == 0 else ""

    @staticmethod
    def _write_clipboard(text: str) -> None:
        proc = subprocess.run(
            ["pbcopy"], input=text, text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pbcopy failed: {proc.stderr.strip()}")


# ───────────────────────────── Linux ──────────────────────────────


class LinuxAdapter(PlatformAdapter):
    """Linux adapter. Supports X11 (xdotool + xclip) fully, Wayland partially.

    On Wayland, frontmost-app detection is compositor-specific and not
    universally supported; users should pass ``--target-app`` explicitly and
    focus the target window manually.
    """

    supported_apps = {
        "gnome-terminal", "konsole", "xterm", "xfce4-terminal",
        "alacritty", "kitty", "wezterm", "warp-terminal",
        "code", "cursor",
    }

    def __init__(self) -> None:
        self.display_server = (
            "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"
        )
        if self.display_server == "wayland":
            missing = [t for t in ("wtype", "wl-copy", "wl-paste") if not shutil.which(t)]
            if missing:
                raise RuntimeError(
                    f"Wayland dispatch requires: {', '.join(missing)}. "
                    "Install via your package manager (e.g. "
                    "`apt install wtype wl-clipboard`, "
                    "`pacman -S wtype wl-clipboard`)."
                )
        else:
            missing = [t for t in ("xdotool", "xclip") if not shutil.which(t)]
            if missing:
                raise RuntimeError(
                    f"X11 dispatch requires: {', '.join(missing)}. "
                    "Install via your package manager (e.g. "
                    "`apt install xdotool xclip`)."
                )

    def get_frontmost_app(self) -> str:
        if self.display_server == "wayland":
            raise RuntimeError(
                "Frontmost-app detection is not supported on Wayland. "
                "Pass --target-app <name> explicitly and keep that window focused."
            )
        # xdotool: window title of the active window. Typically "PID<pid>-<window_title>"
        # or just the window title depending on WM. We return the title as-is;
        # the user matches it (or a substring) with --target-app.
        proc = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"xdotool failed: {proc.stderr.strip()}")
        title = proc.stdout.strip()
        if not title:
            raise RuntimeError("Active window has no title; cannot infer app name.")
        return title

    def paste_and_return(self, text: str, target_app: str) -> None:
        original = self._read_clipboard()
        try:
            self._write_clipboard(text)
            self._activate_window(target_app)
            time.sleep(self.focus_delay_secs)
            if self.display_server == "wayland":
                subprocess.run(
                    ["wtype", "-M", "ctrl", "v", "-m", "ctrl"],
                    check=False,
                )
                time.sleep(self.paste_delay_secs)
                subprocess.run(["wtype", "-k", "Return"], check=False)
            else:
                subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], check=False)
                time.sleep(self.paste_delay_secs)
                subprocess.run(["xdotool", "key", "--clearmodifiers", "Return"], check=False)
            time.sleep(self.submit_delay_secs)
        finally:
            self._write_clipboard(original)

    def _activate_window(self, target_app: str) -> None:
        if self.display_server == "wayland":
            # Generic programmatic focus is blocked by Wayland's security model.
            # The user must keep the right window focused themselves.
            return
        # X11: find a window whose name matches (substring, case-insensitive)
        # and activate it.
        proc = subprocess.run(
            ["xdotool", "search", "--name", target_app],
            text=True, capture_output=True, check=False,
        )
        win_ids = [w for w in proc.stdout.splitlines() if w.strip()]
        if not win_ids:
            # Fall back: just use whatever is currently focused.
            return
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", win_ids[0]],
            check=False,
        )

    def _read_clipboard(self) -> str:
        cmd = ["wl-paste"] if self.display_server == "wayland" else ["xclip", "-selection", "clipboard", "-o"]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        return proc.stdout if proc.returncode == 0 else ""

    def _write_clipboard(self, text: str) -> None:
        cmd = ["wl-copy"] if self.display_server == "wayland" else ["xclip", "-selection", "clipboard"]
        proc = subprocess.run(cmd, input=text, text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"Clipboard write failed: {proc.stderr.strip()}")


# ──────────────────────────── Windows ─────────────────────────────


class WindowsAdapter(PlatformAdapter):
    supported_apps = {
        "cmd.exe", "powershell.exe", "pwsh.exe", "WindowsTerminal.exe",
        "wezterm-gui.exe", "Alacritty.exe",
        "Code.exe", "Cursor.exe",
    }

    def __init__(self) -> None:
        try:
            import pyperclip  # type: ignore
            import pyautogui  # type: ignore
            import win32gui    # type: ignore  # noqa: F401
            import win32process  # type: ignore  # noqa: F401
            import psutil      # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Windows dispatch requires additional packages. Install with:\n"
                "    pip install pyperclip pyautogui pywin32 psutil\n"
                f"Import error: {exc}"
            ) from exc
        self._pyperclip = pyperclip
        self._pyautogui = pyautogui
        self._psutil = psutil
        # Bind win32 modules to avoid reimporting per call.
        import win32gui, win32process  # type: ignore
        self._win32gui = win32gui
        self._win32process = win32process
        # pyautogui safety: moving mouse to a corner aborts. Disable that.
        self._pyautogui.FAILSAFE = False

    def get_frontmost_app(self) -> str:
        hwnd = self._win32gui.GetForegroundWindow()
        if not hwnd:
            raise RuntimeError("No foreground window detected.")
        _, pid = self._win32process.GetWindowThreadProcessId(hwnd)
        try:
            return self._psutil.Process(pid).name()
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"Unable to resolve PID {pid} to a process: {exc}") from exc

    def paste_and_return(self, text: str, target_app: str) -> None:
        original = self._pyperclip.paste()
        try:
            self._pyperclip.copy(text)
            self._activate_window(target_app)
            time.sleep(self.focus_delay_secs)
            self._pyautogui.hotkey("ctrl", "v")
            time.sleep(self.paste_delay_secs)
            self._pyautogui.press("enter")
            time.sleep(self.submit_delay_secs)
        finally:
            self._pyperclip.copy(original)

    def _activate_window(self, target_app: str) -> None:
        target_lower = target_app.lower()
        matching_hwnd = None

        def enum_cb(hwnd, _result):
            nonlocal matching_hwnd
            if matching_hwnd is not None:
                return
            try:
                _, pid = self._win32process.GetWindowThreadProcessId(hwnd)
                name = self._psutil.Process(pid).name().lower()
                if name == target_lower and self._win32gui.IsWindowVisible(hwnd):
                    matching_hwnd = hwnd
            except Exception:
                pass

        self._win32gui.EnumWindows(enum_cb, None)
        if matching_hwnd:
            try:
                self._win32gui.SetForegroundWindow(matching_hwnd)
            except Exception:
                # Best-effort; some focus-stealing rules block this.
                pass


# ───────────────────────────── Factory ────────────────────────────


def get_platform_adapter() -> PlatformAdapter:
    if sys.platform == "darwin":
        return MacAdapter()
    if sys.platform.startswith("linux"):
        return LinuxAdapter()
    if sys.platform in ("win32", "cygwin"):
        return WindowsAdapter()
    raise RuntimeError(
        f"Unsupported platform: {sys.platform}. "
        "VoiceCode supports macOS, Linux, and Windows."
    )
