"""py2app build config for the MicOracle menu-bar app.

Build a standalone .app:

    python setup_app.py py2app

The transcription models are NOT bundled (they are large and download on first
use from Hugging Face into ~/.cache). Everything else is frozen into the app.

py2app sometimes misses a dynamically-imported module; if the built app fails
to launch with a ModuleNotFoundError, add that module name to `includes` below
and rebuild.
"""

import sys

# py2app's modulegraph walks each module's AST recursively; deeply-nested
# literals in the dependency tree (numba/scipy/etc.) overflow the default
# limit of 1000 with a RecursionError. Raise it before py2app runs.
sys.setrecursionlimit(10000)

from setuptools import setup

APP = ["voice_agent.py"]

# Our flat (non-package) modules must be force-included — py2app follows imports
# but being explicit avoids surprises when freezing.
INCLUDES = [
    "engine",
    "hands_free_voice",
    "stt",
    "tts",
    "segmenter",
    "platform_adapter",
    "pro",
    "macros",
    "analytics",
    "paths",
    "numpy",
    "sounddevice",
    "webrtcvad",
    "soundfile",
    "cffi",
    "rumps",
]

# Copy these as WHOLE packages (not split/zipped). Critical for native packages:
# py2app otherwise separates the .so from its pure-Python submodules and adjacent
# dylibs, causing "No module named mlx._reprlib_fix" / "libmlx.dylib not loaded".
PACKAGES = [
    "rumps",
    "numpy",
    "sounddevice",
    "soundfile",
    # MLX (Apple Silicon local STT) and its dependency tree
    "mlx",
    "mlx_whisper",
    "numba",
    "llvmlite",
    "scipy",
    "tiktoken",
    "regex",
    "huggingface_hub",
    "tqdm",
    # faster-whisper (cross-platform local STT) and its engine
    "faster_whisper",
    "ctranslate2",
]

# These are optional at runtime; excluding keeps the bundle smaller. Remove an
# entry here (and ensure it's pip-installed) if you want it frozen in.
EXCLUDES = [
    "tkinter",
    "matplotlib",
    "PyQt5",
    "PySide2",
    "pytest",
    "test",
]

PLIST = {
    "CFBundleName": "MicOracle",
    "CFBundleDisplayName": "MicOracle",
    "CFBundleIdentifier": "com.thepradip.micoracle",
    "CFBundleShortVersionString": "1.5.0",
    "CFBundleVersion": "1.5.0",
    "LSMinimumSystemVersion": "12.0",
    # Menu-bar only — no dock icon, no main window.
    "LSUIElement": True,
    # macOS requires these usage strings or the app is killed on first access.
    "NSMicrophoneUsageDescription":
        "MicOracle listens for your wake word to transcribe voice commands on-device.",
    "NSAppleEventsUsageDescription":
        "MicOracle pastes transcribed text into your active app and presses Return.",
}

OPTIONS = {
    "argv_emulation": False,
    "plist": PLIST,
    "includes": INCLUDES,
    "packages": PACKAGES,
    "excludes": EXCLUDES,
    "optimize": 1,
    "iconfile": "assets/micoracle.icns",
}

setup(
    app=APP,
    name="MicOracle",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
