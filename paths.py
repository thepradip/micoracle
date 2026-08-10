"""Shared filesystem locations for user config and runtime state.

Everything MicOracle writes for a user — license, macros, usage log — lives
under a single directory so it is easy to find, back up, or wipe. The default
is ``~/.micoracle``; set ``MICORACLE_HOME`` to relocate it (used by tests to
point at a temp directory).
"""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    """Return the MicOracle config directory, creating it if needed."""
    override = os.environ.get("MICORACLE_HOME", "").strip()
    base = Path(override).expanduser() if override else Path.home() / ".micoracle"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path(name: str) -> Path:
    """Return a path to ``name`` inside the config directory."""
    return config_dir() / name


def screenshot_dir() -> Path:
    """Folder where screenshots land: ~/Desktop/Screenshots by default.

    Set ``MICORACLE_SCREENSHOT_DIR`` to relocate it. Created on first use.
    """
    override = os.environ.get("MICORACLE_SCREENSHOT_DIR", "").strip()
    base = (
        Path(override).expanduser() if override
        else Path.home() / "Desktop" / "Screenshots"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def screenshot_path(prefix: str = "micoracle") -> Path:
    """A fresh timestamped .png path inside the screenshot folder."""
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return screenshot_dir() / f"{prefix}-{stamp}.png"
