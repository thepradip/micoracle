"""Computer-control actions for Jarvis-style voice control (macOS).

Turns spoken commands into real actions on the Mac — open apps, switch focus,
take screenshots, open URLs / web searches, and type into the active app. This
is the deterministic (no-LLM, offline) action layer: ``parse()`` maps a command
string to an :class:`Intent` (pure, unit-testable), and ``execute()`` performs
the side effect. ``route()`` is the convenience pair.

Routing is meant to be gated to the "jarvis" wake word by the caller, so normal
dictation ("claude, open the config file") is never mistaken for a command.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import quote_plus

import platform_adapter as _pa

# Spoken app names → real macOS application names.
APP_ALIASES = {
    "browser": "Safari", "the browser": "Safari", "safari": "Safari",
    "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "terminal": "Terminal", "iterm": "iTerm", "warp": "Warp",
    "text editor": "TextEdit", "textedit": "TextEdit", "notes": "Notes",
    "vs code": "Visual Studio Code", "vscode": "Visual Studio Code",
    "code": "Visual Studio Code", "finder": "Finder", "mail": "Mail",
    "calendar": "Calendar", "music": "Music", "spotify": "Spotify",
}

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


def _resolve_app(name: str) -> str:
    return APP_ALIASES.get(name.strip().lower(), name.strip().title())


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


# ─────────────────────────── execute ──────────────────────────────


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, text=True).returncode == 0
    except Exception:
        return False


def execute(intent: Intent) -> ActionResult:
    k = intent.kind
    if k == "screenshot":
        path = os.path.expanduser("~/Desktop/micoracle-screenshot.png")
        ok = _run(["screencapture", "-x", path]) and os.path.exists(path)
        return ActionResult(ok, k, path, "Screenshot saved to your desktop" if ok else "Screenshot failed")

    if k == "open_app":
        ok = _run(["open", "-a", intent.arg])
        return ActionResult(ok, k, intent.arg, f"Opening {intent.arg}" if ok else f"I couldn't open {intent.arg}")

    if k == "focus_app":
        ok = _run(["osascript", "-e", f'tell application "{intent.arg}" to activate'])
        return ActionResult(ok, k, intent.arg, f"Switching to {intent.arg}" if ok else f"I couldn't switch to {intent.arg}")

    if k == "open_url":
        url = intent.arg if re.match(r"^https?://", intent.arg) else "https://" + intent.arg
        ok = _run(["open", url])
        return ActionResult(ok, k, url, "Opening the page" if ok else "I couldn't open that link")

    if k == "web_search":
        url = "https://www.google.com/search?q=" + quote_plus(intent.arg)
        ok = _run(["open", url])
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
