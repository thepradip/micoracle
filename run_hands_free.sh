#!/usr/bin/env bash
# Launcher for macOS and Linux.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="$HOME/.local/bin:$PATH"
exec python3 hands_free_voice.py "$@"
