#!/usr/bin/env bash
#
# Build the MicOracle.app menu-bar agent and package it as a .dmg.
#
#   ./build_dmg.sh
#
# Produces dist/MicOracle-<version>.dmg. The app is ad-hoc signed so it runs on
# THIS machine. To distribute to other Macs without the "unidentified developer"
# warning, sign + notarize with a Developer ID (see app/README_APP.md).
#
set -euo pipefail
cd "$(dirname "$0")"

VERSION="1.6.0"
APP="dist/MicOracle.app"
DMG="dist/MicOracle-${VERSION}.dmg"
VENV=".build-venv"

# Build with a NON-conda Python. Conda links libffi/libssl via @rpath that
# py2app fails to bundle, so the frozen app crashes on launch with
# "Library not loaded: @rpath/libffi.8.dylib". Homebrew/python.org builds work.
PYTHON="${PYTHON:-/opt/homebrew/bin/python3.13}"
if [ ! -x "$PYTHON" ]; then
  echo "ERROR: $PYTHON not found. Install Homebrew Python (brew install python@3.13)"
  echo "       or set PYTHON=/path/to/non-conda/python3 and re-run."
  exit 1
fi
case "$("$PYTHON" -c 'import sys; print(sys.prefix)')" in
  *conda*|*miniconda*|*anaconda*)
    echo "ERROR: $PYTHON is a conda Python — py2app bundles from conda crash on"
    echo "       launch (libffi @rpath). Use Homebrew/python.org instead."
    exit 1 ;;
esac
echo "==> Building with $("$PYTHON" --version) at $PYTHON"

echo "==> Setting up a fresh isolated build venv ($VENV)"
rm -rf "$VENV"
"$PYTHON" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip wheel >/dev/null

echo "==> Installing runtime + build dependencies"
pip install -r requirements.txt
# faster-whisper is the bundled local STT (MLX 0.31+ can't be frozen by py2app).
pip install "faster-whisper>=1.0.0" rumps py2app
# Pro features (optional but bundled so licensed users get them):
pip install "cryptography>=39.0" "pyyaml>=6.0" || true
# Jarvis conversational brain (BYO-key) — bundle both providers.
pip install "openai>=1.0.0" "anthropic>=0.40.0" || true
# py2app breaks on setuptools >=71 (drops the legacy install path it uses).
pip install "setuptools<71"

echo "==> Cleaning previous build"
rm -rf build dist

echo "==> Freezing the app with py2app"
# Temporarily hide pyproject.toml so setuptools doesn't merge the library's
# [project] metadata (dependencies) into the app's setup() — that merge raises
# "install_requires is no longer supported". Restore it no matter what.
restore_pyproject() { [ -f pyproject.toml.bak ] && mv pyproject.toml.bak pyproject.toml; }
trap restore_pyproject EXIT
mv pyproject.toml pyproject.toml.bak
python setup_app.py py2app
restore_pyproject
trap - EXIT

echo "==> Ad-hoc code-signing (runs locally; not notarized)"
codesign --force --deep --sign - "$APP" || \
  echo "    (ad-hoc signing failed — app may need right-click → Open)"

echo "==> Building the DMG"
rm -f "$DMG"
STAGING="$(mktemp -d)"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
hdiutil create \
  -volname "MicOracle" \
  -srcfolder "$STAGING" \
  -ov -format UDZO \
  "$DMG"
rm -rf "$STAGING"

deactivate
echo ""
echo "==> Done: $DMG"
echo "    Open the DMG and drag MicOracle to Applications."
echo "    First launch: right-click MicOracle → Open (unsigned app)."
echo "    Grant Microphone + Accessibility when prompted."
