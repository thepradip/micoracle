"""MicOracle — macOS menu-bar voice agent.

A thin rumps UI around :class:`engine.VoiceEngine`. Lives in the menu bar
(no dock icon), lets you start/stop listening, pick the transcription model,
see the current target app and license tier, and quit.

Status updates arrive from the engine's background threads, so they are stashed
and applied on the main thread by a rumps timer — AppKit UI must only be touched
from the main run loop.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

# When frozen by py2app the modules sit beside this file; in dev, the repo root
# is already on sys.path. This keeps both happy.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rumps

import paths
import platform_adapter as _pa
import pro
from engine import EngineConfig, VoiceEngine

APP_NAME = "MicOracle"
ICON_IDLE = "🎙️"
ICON_LIVE = "🔴"

LISTEN_MODELS = ["auto", "mlx", "faster"]
COMMAND_MODELS = ["none", "groq", "deepgram", "openai", "realtime"]


def _settings_path() -> Path:
    return paths.config_path("app.json")


def _load_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text())
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    try:
        _settings_path().write_text(json.dumps(data, indent=2))
    except OSError:
        pass


class MicOracleApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(APP_NAME, title=ICON_IDLE, quit_button=None)
        s = _load_settings()
        self.cfg = EngineConfig(
            stt_backend=s.get("stt_backend", "auto"),
            command_stt_backend=(s.get("command_stt_backend") or None),
            tts_backend=s.get("tts_backend", "auto"),
            target_app=s.get("target_app", ""),
            device=s.get("device", ""),
        )
        self.engine: VoiceEngine | None = None
        self._status = "stopped"
        self._lock = threading.Lock()

        # Menu structure.
        self.status_item = rumps.MenuItem("Idle")
        self.status_item.set_callback(None)  # display-only
        self.toggle_item = rumps.MenuItem("Start listening", callback=self.on_toggle)
        self.target_item = rumps.MenuItem("Target: active app")
        self.target_item.set_callback(None)
        self.pin_item = rumps.MenuItem("Pin to current app", callback=self.on_pin)
        self.license_item = rumps.MenuItem("License: Free", callback=self.on_license)

        self.menu = [
            self.status_item,
            self.toggle_item,
            None,
            self._mic_menu(),
            self._listen_menu(),
            self._command_menu(),
            None,
            self.target_item,
            self.pin_item,
            None,
            self.license_item,
            rumps.MenuItem("Open config folder", callback=self.on_open_config),
            None,
            rumps.MenuItem(f"Quit {APP_NAME}", callback=self.on_quit),
        ]

        self._refresh_license_label()
        # Apply engine status to the UI on the main thread, ~3x/sec.
        rumps.Timer(self._tick, 0.3).start()

    # ── menu builders ────────────────────────────────────────────────
    def _mic_menu(self) -> rumps.MenuItem:
        root = rumps.MenuItem("Microphone")
        self._mic_items: dict[str, rumps.MenuItem] = {}
        # "System default" plus every input device by name.
        entries = [("", "System default")]
        try:
            import hands_free_voice as hfv
            for _idx, d in hfv.list_input_devices():
                entries.append((d["name"], d["name"]))
        except Exception:
            pass
        for value, label in entries:
            it = rumps.MenuItem(label, callback=self.on_pick_mic)
            it.state = (value == (self.cfg.device or ""))
            root.add(it)
            self._mic_items[label] = (value, it)
        return root

    def _listen_menu(self) -> rumps.MenuItem:
        root = rumps.MenuItem("Listening model")
        self._listen_items: dict[str, rumps.MenuItem] = {}
        for name in LISTEN_MODELS:
            it = rumps.MenuItem(name, callback=self.on_pick_listen)
            it.state = (name == self.cfg.stt_backend)
            root.add(it)
            self._listen_items[name] = it
        return root

    def _command_menu(self) -> rumps.MenuItem:
        root = rumps.MenuItem("Command model (cloud)")
        self._command_items: dict[str, rumps.MenuItem] = {}
        current = self.cfg.command_stt_backend or "none"
        for name in COMMAND_MODELS:
            it = rumps.MenuItem(name, callback=self.on_pick_command)
            it.state = (name == current)
            root.add(it)
            self._command_items[name] = it
        return root

    # ── status plumbing ──────────────────────────────────────────────
    def _on_engine_status(self, msg: str) -> None:
        with self._lock:
            self._status = msg

    def _tick(self, _timer) -> None:
        with self._lock:
            status = self._status
        running = bool(self.engine and self.engine.running)
        self.title = ICON_LIVE if running else ICON_IDLE
        self.toggle_item.title = "Stop listening" if running else "Start listening"
        if running and self.engine and self.engine.device_name:
            self.status_item.title = f"{status}  ·  🎤 {self.engine.device_name}"
        else:
            self.status_item.title = status if running else "Idle"
        pinned = (self.cfg.target_app or "").strip()
        self.target_item.title = f"Target: {pinned}" if pinned else "Target: active app"
        self.pin_item.title = "Use active app (unpin)" if pinned else "Pin to current app"

    def _refresh_license_label(self) -> None:
        ent = pro.load_entitlement()
        self.license_item.title = f"License: {ent.describe()}"

    def _persist(self) -> None:
        _save_settings({
            "stt_backend": self.cfg.stt_backend,
            "command_stt_backend": self.cfg.command_stt_backend,
            "tts_backend": self.cfg.tts_backend,
            "target_app": self.cfg.target_app,
            "device": self.cfg.device,
        })

    # ── actions ──────────────────────────────────────────────────────
    def on_toggle(self, _sender) -> None:
        if self.engine and self.engine.running:
            self.engine.stop()
            self._on_engine_status("stopped")
            return
        self.engine = VoiceEngine(self.cfg, status_cb=self._on_engine_status)
        try:
            self.engine.start()
        except Exception as exc:
            self.engine = None
            rumps.alert(
                title=f"{APP_NAME} couldn't start",
                message=f"{exc}\n\nCheck microphone permission in System Settings → "
                        "Privacy & Security → Microphone, then try again.",
            )
            return
        self._refresh_license_label()

    def _restart_if_running(self) -> None:
        if self.engine and self.engine.running:
            self.engine.stop()
            self.on_toggle(None)

    def on_pick_mic(self, sender) -> None:
        for label, (value, it) in self._mic_items.items():
            it.state = (label == sender.title)
        chosen = self._mic_items.get(sender.title, ("", None))[0]
        self.cfg.device = chosen
        self._persist()
        self._restart_if_running()

    def on_pick_listen(self, sender) -> None:
        for name, it in self._listen_items.items():
            it.state = (name == sender.title)
        self.cfg.stt_backend = sender.title
        self._persist()
        self._restart_if_running()

    def on_pick_command(self, sender) -> None:
        for name, it in self._command_items.items():
            it.state = (name == sender.title)
        self.cfg.command_stt_backend = None if sender.title == "none" else sender.title
        self._persist()
        self._restart_if_running()

    def on_pin(self, _sender) -> None:
        # Toggle between dynamic (type into whatever app is active) and pinned
        # (always type into one named app, captured now).
        if (self.cfg.target_app or "").strip():
            self.cfg.target_app = ""          # unpin → dynamic
        else:
            try:
                self.cfg.target_app = _pa.get_platform_adapter().get_frontmost_app()
            except Exception as exc:
                rumps.alert(title="Couldn't pin app", message=str(exc))
                return
        self._persist()
        self._restart_if_running()

    def on_license(self, _sender) -> None:
        ent = pro.load_entitlement()
        if ent.is_pro:
            rumps.alert(title="MicOracle Pro", message=ent.describe())
        else:
            rumps.alert(
                title="MicOracle Free",
                message="Voice macros, custom wake words, and analytics export are "
                        "Pro features.\n\nActivate a license with:\n"
                        "  micoracle license <KEY>\n\nor set MICORACLE_LICENSE.",
            )

    def on_open_config(self, _sender) -> None:
        import subprocess
        subprocess.Popen(["open", str(paths.config_dir())])

    def on_quit(self, _sender) -> None:
        if self.engine and self.engine.running:
            self.engine.stop()
        rumps.quit_application()


def main() -> None:
    MicOracleApp().run()


if __name__ == "__main__":
    main()
