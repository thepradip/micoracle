"""Computer-control actions for MicOracle voice control (macOS, Linux, Windows).

Turns spoken commands into real actions on the machine — open apps, switch
focus, take screenshots, open URLs / web searches, and type into the active
app. This is the deterministic (no-LLM, offline) action layer: ``parse()``
maps a command string to an :class:`Intent` (pure, unit-testable), and
``execute()`` performs the side effect. ``route()`` is the convenience pair.

Per-OS mechanics:
  - macOS   — screencapture / open -a / osascript
  - Linux   — gnome-screenshot|scrot|grim|import|spectacle, $PATH launch or
              gtk-launch, xdotool focus
  - Windows — PowerShell (CopyFromScreen screenshot, Start-Process launch,
              WScript AppActivate focus)
URLs and web searches use the stdlib ``webbrowser`` module everywhere.

Routing is gated to the "micoracle" wake word by the caller, so normal
dictation ("claude, open the config file") is never mistaken for a command.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from urllib.parse import quote_plus

import paths as _paths
import platform_adapter as _pa

# Spoken app names → real application names, per OS. Includes common Whisper
# mishears ("versus code", "what's app") — the same trick as WAKE_VARIANTS.
_VSCODE_MISHEARS = ("vs code", "vscode", "vs. code", "versus code", "the s code")
_WHATSAPP_MISHEARS = ("whatsapp", "whats app", "what's app", "watts app", "bots app")

_MAC_ALIASES = {
    "browser": "Safari", "the browser": "Safari", "safari": "Safari",
    "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "terminal": "Terminal", "iterm": "iTerm", "warp": "Warp",
    "text editor": "TextEdit", "textedit": "TextEdit", "notes": "Notes",
    "code": "Visual Studio Code", "finder": "Finder", "mail": "Mail",
    "calendar": "Calendar", "music": "Music", "spotify": "Spotify",
    "slack": "Slack", "discord": "Discord", "telegram": "Telegram",
    "zoom": "zoom.us", "teams": "Microsoft Teams", "edge": "Microsoft Edge",
    "firefox": "Firefox", "brave": "Brave Browser", "notion": "Notion",
    "word": "Microsoft Word", "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "outlook": "Microsoft Outlook",
    "settings": "System Settings", "system settings": "System Settings",
    **{m: "Visual Studio Code" for m in _VSCODE_MISHEARS},
    **{m: "WhatsApp" for m in _WHATSAPP_MISHEARS},
}
_LINUX_ALIASES = {
    "browser": "firefox", "the browser": "firefox", "chrome": "google-chrome",
    "google chrome": "google-chrome", "terminal": "gnome-terminal",
    "files": "nautilus", "file manager": "nautilus", "text editor": "gedit",
    "spotify": "spotify", "slack": "slack", "discord": "discord",
    "telegram": "telegram-desktop", "zoom": "zoom", "firefox": "firefox",
    "edge": "microsoft-edge", "brave": "brave-browser",
    **{m: "code" for m in _VSCODE_MISHEARS},
    **{m: "whatsapp-for-linux" for m in _WHATSAPP_MISHEARS},
}
_WINDOWS_ALIASES = {
    "browser": "msedge", "the browser": "msedge", "chrome": "chrome",
    "google chrome": "chrome", "terminal": "wt", "notepad": "notepad",
    "text editor": "notepad", "explorer": "explorer", "files": "explorer",
    "mail": "outlook", "slack": "slack", "discord": "discord",
    "telegram": "telegram", "zoom": "zoom", "teams": "ms-teams",
    "edge": "msedge", "brave": "brave", "firefox": "firefox",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "outlook": "outlook",
    **{m: "code" for m in _VSCODE_MISHEARS},
    **{m: "whatsapp" for m in _WHATSAPP_MISHEARS},
}

if sys.platform == "darwin":
    APP_ALIASES = _MAC_ALIASES
elif sys.platform == "win32":
    APP_ALIASES = _WINDOWS_ALIASES
else:
    APP_ALIASES = _LINUX_ALIASES

_SCREENSHOT = re.compile(r"^\s*(?:take|grab|capture|get)?\s*(?:a|the)?\s*screen\s?shot\b", re.I)
_SEARCH = re.compile(r"^\s*(?:search for|search|google|look up)\s+(.+)", re.I)
_FOCUS = re.compile(r"^\s*(?:switch to|focus|go back to|activate)\s+(.+)", re.I)
_TYPE = re.compile(r"^\s*(?:type|write|enter|input)\s+(.+)", re.I)
_GOTO = re.compile(r"^\s*(?:go to|navigate to|visit|open up|open)\s+(.+)", re.I)
_DOMAINISH = re.compile(r"^(https?://|www\.)|\.\w{2,}(/|$)", re.I)


@dataclass(frozen=True)
class Intent:
    kind: str   # screenshot | open_app | open_url | web_search | focus_app | type_text
    arg: str = ""


@dataclass
class ActionResult:
    ok: bool
    kind: str
    detail: str = ""
    speak: str = ""   # short spoken confirmation


# ─────────────────── installed-app discovery ──────────────────────
#
# Whisper mangles app names ("open whatsapp" → "open bots app"), so unknown
# names are fuzzy-matched against what is actually installed on this machine.
# Each entry is (display_name, launch_target): the launch target is what the
# OS launcher needs — app name (macOS), .desktop id (Linux), .lnk path
# (Windows).

_APP_FUZZY_THRESHOLD = 0.78
_installed_cache: "list[tuple[str, str]] | None" = None


def _mac_installed() -> list[tuple[str, str]]:
    apps = []
    roots = ("/Applications", "/Applications/Utilities",
             "/System/Applications", "/System/Applications/Utilities",
             os.path.expanduser("~/Applications"))
    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".app"):
                name = entry[:-4]
                apps.append((name, name))
    return apps


def _linux_installed() -> list[tuple[str, str]]:
    apps = []
    roots = ("/usr/share/applications", "/usr/local/share/applications",
             os.path.expanduser("~/.local/share/applications"))
    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".desktop"):
                continue
            name = None
            try:
                with open(os.path.join(root, entry), encoding="utf-8",
                          errors="ignore") as fh:
                    for line in fh:
                        if line.startswith("Name="):
                            name = line[5:].strip()
                            break
            except OSError:
                continue
            if name:
                apps.append((name, entry[:-8]))  # gtk-launch <desktop-id>
    return apps


def _windows_installed() -> list[tuple[str, str]]:
    apps = []
    roots = (
        os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                     r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
    )
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".lnk"):
                    apps.append((f[:-4], os.path.join(dirpath, f)))
    return apps


def _installed_apps() -> list[tuple[str, str]]:
    global _installed_cache
    if _installed_cache is None:
        try:
            if sys.platform == "darwin":
                _installed_cache = _mac_installed()
            elif sys.platform == "win32":
                _installed_cache = _windows_installed()
            else:
                _installed_cache = _linux_installed()
        except Exception:
            _installed_cache = []
    return _installed_cache


def _normalize_app(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _match_installed(spoken: str) -> str | None:
    """Best installed app for a (possibly misheard) spoken name, or None."""
    norm = _normalize_app(spoken)
    if not norm:
        return None
    best, best_ratio = None, 0.0
    for display, launch in _installed_apps():
        dn = _normalize_app(display)
        if not dn:
            continue
        if dn == norm:
            return launch
        ratio = difflib.SequenceMatcher(None, norm, dn).ratio()
        if ratio > best_ratio:
            best, best_ratio = launch, ratio
    return best if best_ratio >= _APP_FUZZY_THRESHOLD else None


def _resolve_app(name: str) -> str:
    spoken = name.strip().lower()
    if spoken in APP_ALIASES:
        return APP_ALIASES[spoken]
    installed = _match_installed(spoken)
    if installed is not None:
        return installed
    return name.strip().title()


def parse(command: str) -> Intent | None:
    """Map a command string to an Intent, or None if it's not a control action."""
    c = (command or "").strip().rstrip(".!?")
    if not c:
        return None

    if _SCREENSHOT.match(c):
        return Intent("screenshot")

    m = _SEARCH.match(c)
    if m:
        return Intent("web_search", m.group(1).strip())

    m = _FOCUS.match(c)
    if m:
        return Intent("focus_app", _resolve_app(m.group(1)))

    m = _TYPE.match(c)
    if m:
        return Intent("type_text", m.group(1).strip())

    m = _GOTO.match(c)
    if m:
        target = m.group(1).strip()
        first = target.split()[0]
        if _DOMAINISH.search(first):
            return Intent("open_url", first)
        return Intent("open_app", _resolve_app(target))

    return None


_CLAUSE_SPLIT = re.compile(r"\s+(?:and\s+then|then|and)\s+", re.I)


def is_compound(command: str) -> bool:
    """True when the command chains a control action with further steps.

    The control layer executes exactly one intent, so a chained instruction
    like "open safari and type hello" must be routed to the multi-step agent
    instead — otherwise the tail clause is silently swallowed into the first
    intent's argument. A command is compound when it splits on and/then into
    clauses of which at least two parse as control actions, or when it uses
    "then" (an explicit sequencing word) after a leading control action.
    Single-intent phrases whose argument merely contains "and" ("search for
    black and white cats") stay non-compound.
    """
    parts = [p.strip() for p in _CLAUSE_SPLIT.split(command or "") if p.strip()]
    if len(parts) < 2 or parse(parts[0]) is None:
        return False
    if re.search(r"\bthen\b", command, re.I):
        return True
    return any(parse(p) is not None for p in parts[1:])


# ─────────────────────────── execute ──────────────────────────────


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, text=True).returncode == 0
    except Exception:
        return False


# ── per-OS action primitives ──────────────────────────────────────


def _screenshot_argv(path: str) -> list[str] | None:
    """The screenshot command for this OS, or None if no tool is available."""
    if sys.platform == "darwin":
        return ["screencapture", "-x", path]
    if sys.platform == "win32":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
            "$g=[System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); "
            f"$bmp.Save('{path}'); $g.Dispose(); $bmp.Dispose()"
        )
        return ["powershell", "-NoProfile", "-Command", ps]
    # Linux/BSD: first available tool wins (X11 and Wayland options).
    for tool, argv in (
        ("gnome-screenshot", ["gnome-screenshot", "-f", path]),
        ("scrot", ["scrot", path]),
        ("grim", ["grim", path]),
        ("spectacle", ["spectacle", "-b", "-n", "-o", path]),
        ("import", ["import", "-window", "root", path]),
    ):
        if shutil.which(tool):
            return argv
    return None


def take_screenshot(path: str) -> bool:
    """Capture the whole screen to ``path``. Cross-platform, best tool wins."""
    argv = _screenshot_argv(path)
    return argv is not None and _run(argv) and os.path.exists(path)


def _open_app(app: str) -> bool:
    if sys.platform == "darwin":
        return _run(["open", "-a", app])
    if sys.platform == "win32":
        return _run(["powershell", "-NoProfile", "-Command", f"Start-Process '{app}'"])
    # Linux: try the binary on $PATH, then a .desktop launch (the resolver may
    # hand us a real desktop-id from _linux_installed — try it verbatim first).
    for candidate in (app, app.lower().replace(" ", "-"), app.lower().replace(" ", "")):
        exe = shutil.which(candidate)
        if exe:
            try:
                subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False
    if shutil.which("gtk-launch"):
        for candidate in (app, app.lower().replace(" ", "-")):
            if _run(["gtk-launch", candidate]):
                return True
    return False


def _focus_app(app: str) -> bool:
    if sys.platform == "darwin":
        return _run(["osascript", "-e", f'tell application "{app}" to activate'])
    if sys.platform == "win32":
        ps = f"(New-Object -ComObject WScript.Shell).AppActivate('{app}')"
        return _run(["powershell", "-NoProfile", "-Command", ps])
    if shutil.which("xdotool"):
        return _run(["xdotool", "search", "--name", app, "windowactivate"])
    return False


def _open_in_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def execute(intent: Intent) -> ActionResult:
    k = intent.kind
    if k == "screenshot":
        path = str(_paths.screenshot_path())
        ok = take_screenshot(path)
        return ActionResult(ok, k, path, "Screenshot saved to your Screenshots folder" if ok else "Screenshot failed")

    if k == "open_app":
        ok = _open_app(intent.arg)
        return ActionResult(ok, k, intent.arg, f"Opening {intent.arg}" if ok else f"I couldn't open {intent.arg}")

    if k == "focus_app":
        ok = _focus_app(intent.arg)
        return ActionResult(ok, k, intent.arg, f"Switching to {intent.arg}" if ok else f"I couldn't switch to {intent.arg}")

    if k == "open_url":
        url = intent.arg if re.match(r"^https?://", intent.arg) else "https://" + intent.arg
        ok = _open_in_browser(url)
        return ActionResult(ok, k, url, "Opening the page" if ok else "I couldn't open that link")

    if k == "web_search":
        url = "https://www.google.com/search?q=" + quote_plus(intent.arg)
        ok = _open_in_browser(url)
        return ActionResult(ok, k, url, f"Searching for {intent.arg}" if ok else "Search failed")

    if k == "type_text":
        try:
            _pa.get_platform_adapter().paste_and_return(intent.arg, "")  # active app, dynamic
            return ActionResult(True, k, intent.arg, "")
        except Exception as exc:
            return ActionResult(False, k, str(exc), "I couldn't type that")

    return ActionResult(False, "unknown", intent.kind, "I didn't understand that command")


def route(command: str) -> ActionResult | None:
    """Parse + execute. Returns None if the command isn't a control action."""
    intent = parse(command)
    if intent is None:
        return None
    return execute(intent)
