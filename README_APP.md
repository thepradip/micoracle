# MicOracle.app — macOS menu-bar voice agent

A native menu-bar build of MicOracle: a 🎙️ in your status bar that listens for
your wake word and types transcribed commands into the active app. No terminal,
no `pip`, no Python knowledge needed once it's built.

## What it is

- **`voice_agent.py`** — the menu-bar UI (rumps): Start/Stop, model picker,
  target app, license, quit.
- **`engine.py`** — start/stop wrapper around the same listener the CLI uses.
- **`setup_app.py`** — py2app config that freezes it into `MicOracle.app`.
- **`build_dmg.sh`** — one command: build the `.app` and wrap it in a `.dmg`.

The transcription model is **not** bundled (too large); it downloads on first
use into `~/.cache`. On Apple Silicon the default is MLX Whisper — fully
on-device, no network, no API key.

## Build it

```bash
./build_dmg.sh
```

That creates `dist/MicOracle-1.5.0.dmg`. Open it and drag **MicOracle** to
Applications.

> **First py2app build tip:** if the app launches and immediately quits with a
> `ModuleNotFoundError`, add the named module to `INCLUDES` in `setup_app.py`
> and rebuild. This is normal for py2app freezing dynamically-imported deps.

## First launch (unsigned app)

This machine has **no Apple Developer ID**, so the app is *ad-hoc signed* — it
runs locally but macOS Gatekeeper will warn on first open:

1. **Right-click** MicOracle in Applications → **Open** → **Open** again.
   (Double-clicking shows "unidentified developer" with no Open button; the
   right-click path is the bypass.)
2. Grant **Microphone** when prompted (System Settings → Privacy & Security →
   Microphone).
3. Grant **Accessibility** so it can paste into other apps (System Settings →
   Privacy & Security → Accessibility → enable MicOracle).

Then click the 🎙️ → **Start listening**, focus your terminal/editor, and say
*"Claude, …"*, *"Codex, …"*, or *"Micoracle, …"*.

## Distributing to other Macs (notarization)

To ship the DMG so others can double-click without warnings, you need a paid
**Apple Developer ID** ($99/yr). Once you have it:

```bash
# 1. Sign with your Developer ID (replace the identity)
codesign --force --deep --options runtime \
  --sign "Developer ID Application: Your Name (TEAMID)" dist/MicOracle.app

# 2. Notarize (requires an app-specific password stored as a keychain profile)
xcrun notarytool submit dist/MicOracle-1.5.0.dmg \
  --keychain-profile "notary" --wait

# 3. Staple the ticket so it works offline
xcrun stapler staple dist/MicOracle-1.5.0.dmg
```

`build_dmg.sh` already structures the bundle correctly; add these three steps
after it when you're ready to distribute.

## Pro features in the app

The menu-bar app respects the same license as the CLI. Activate once:

```bash
micoracle license <KEY>
```

Then voice macros, custom wake words, and analytics export work inside the app
too. The license, macros, and settings all live under `~/.micoracle/`
(reachable from the menu via **Open config folder**).

## App icon

The Dock / Finder / DMG icon is `assets/micoracle.icns` (built from the
MicOracle logo) and is already wired into `setup_app.py`. The **menu-bar**
glyph stays as an emoji (🎙️ idle, 🔴 listening) — a small colored logo with
text doesn't render well at menu-bar size.

To regenerate the `.icns` from a new 1024×1024+ PNG:

```bash
ICONSET=assets/micoracle.iconset; mkdir -p "$ICONSET"
for sz in 16 32 128 256 512; do
  sips -z $sz $sz logo.png --out "$ICONSET/icon_${sz}x${sz}.png"
  sips -z $((sz*2)) $((sz*2)) logo.png --out "$ICONSET/icon_${sz}x${sz}@2x.png"
done
iconutil -c icns "$ICONSET" -o assets/micoracle.icns && rm -rf "$ICONSET"
```
