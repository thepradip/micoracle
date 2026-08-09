"""Start/stop voice engine for embedding in a GUI (e.g. the menu-bar app).

The CLI in ``hands_free_voice.py`` runs the listener as a blocking loop on the
main thread. A desktop app needs to start and stop that loop on demand and get
status updates without blocking its own UI run-loop. ``VoiceEngine`` wraps the
exact same pipeline — VAD segmenter, wake-word gate, dual-backend dispatch — but
drives it from background threads with a stop event and a status callback.

It deliberately reuses the helpers and constants from ``hands_free_voice`` so
the GUI and CLI share one implementation of the tricky parts (wake detection,
dispatch, Pro wiring).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from dataclasses import dataclass

import numpy as np

import analytics as _analytics
import hands_free_voice as hfv
import jarvis as _jarvis
import macros as _macros
import paths
import platform_adapter as _pa
import pro
import stt as _stt
import tts as _tts
from segmenter import VADSegmenter

_LOCAL_BACKENDS = {"mlx", "faster", "auto"}


def _make_logger() -> logging.Logger:
    """Log engine activity to <config>/engine.log so the GUI app is debuggable.

    The menu-bar app has no console; without this, failures (no audio, bad
    device, dispatch errors) are invisible. The CLI prints to stdout instead.
    """
    logger = logging.getLogger("micoracle.engine")
    if not logger.handlers:
        try:
            handler = logging.FileHandler(paths.config_path("engine.log"))
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        except Exception:
            pass
    return logger


log = _make_logger()


@dataclass
class EngineConfig:
    stt_backend: str = "auto"
    command_stt_backend: str | None = None
    tts_backend: str = "auto"
    target_app: str = ""
    device: str = ""


def _stt_config_kwargs() -> dict:
    """Read backend params from the environment — identical to the CLI."""
    return dict(
        mlx_repo=os.environ.get(
            "VOICE_AGENT_MLX_REPO", "mlx-community/whisper-medium.en-mlx-4bit"),
        faster_model=os.environ.get("VOICE_AGENT_FASTER_MODEL", "small.en"),
        faster_device=os.environ.get("VOICE_AGENT_FASTER_DEVICE", "auto"),
        faster_compute_type=os.environ.get("VOICE_AGENT_FASTER_COMPUTE", "int8"),
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        openai_model=os.environ.get("VOICE_AGENT_OPENAI_STT_MODEL", "whisper-1"),
        azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT"),
        azure_api_key=os.environ.get("AZURE_OPENAI_KEY"),
        azure_deployment=os.environ.get("AZURE_WHISPER_DEPLOYMENT", "whisper"),
        realtime_api_key=os.environ.get("OPENAI_API_KEY"),
        realtime_model=os.environ.get("VOICE_AGENT_REALTIME_MODEL", "gpt-4o-transcribe"),
        sixtydb_api_key=os.environ.get("SIXTYDB_API_KEY"),
        sixtydb_language=os.environ.get("VOICE_AGENT_SIXTYDB_LANGUAGE", "en"),
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY"),
        elevenlabs_model=os.environ.get("VOICE_AGENT_ELEVENLABS_MODEL", "scribe_v2"),
        elevenlabs_language=os.environ.get("VOICE_AGENT_ELEVENLABS_LANGUAGE", "en"),
        deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY"),
        deepgram_model=os.environ.get("VOICE_AGENT_DEEPGRAM_MODEL", "nova-2"),
        deepgram_language=os.environ.get("VOICE_AGENT_DEEPGRAM_LANGUAGE", "en"),
        assemblyai_api_key=os.environ.get("ASSEMBLYAI_API_KEY"),
        assemblyai_language=os.environ.get("VOICE_AGENT_ASSEMBLYAI_LANGUAGE", "en"),
        groq_api_key=os.environ.get("GROQ_API_KEY"),
        groq_model=os.environ.get("VOICE_AGENT_GROQ_MODEL", "whisper-large-v3-turbo"),
        groq_language=os.environ.get("VOICE_AGENT_GROQ_LANGUAGE", "en"),
        gladia_api_key=os.environ.get("GLADIA_API_KEY"),
    )


class VoiceEngine:
    """Runs the micoracle listener on background threads with start/stop."""

    def __init__(self, config: EngineConfig, status_cb=None) -> None:
        self.config = config
        self._status_cb = status_cb or (lambda msg: None)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stream = None
        self.running = False
        self.entitlement = pro.FREE
        self.target_app = config.target_app
        self.listen_backend = ""
        self.command_backend = ""
        self.device_name = ""

    # ── status ──────────────────────────────────────────────────────
    def _emit(self, msg: str) -> None:
        try:
            self._status_cb(msg)
        except Exception:
            pass

    # ── lifecycle ───────────────────────────────────────────────────
    def start(self) -> None:
        """Build backends and begin listening. Raises on fatal setup errors."""
        if self.running:
            return
        self._stop.clear()
        log.info("VoiceEngine.start() called (device=%r)", self.config.device)

        adapter = _pa.get_platform_adapter()
        # Empty target_app = dynamic: type into whatever app is frontmost when a
        # command fires. Only pin if the user explicitly set one.
        target_app = (self.config.target_app or "").strip()
        self.target_app = target_app

        effective_stt = self.config.stt_backend
        effective_cmd = self.config.command_stt_backend
        if effective_stt not in _LOCAL_BACKENDS:
            # Cost-guard: a paid backend can't drive continuous listening.
            local = _stt.auto_select_stt_backend()
            if effective_cmd is None:
                effective_cmd = effective_stt
            effective_stt = local

        kwargs = _stt_config_kwargs()
        try:
            stt_backend = _stt.make_stt_backend(_stt.STTConfig(backend=effective_stt, **kwargs))
        except Exception as exc:
            # The frozen .app bundles faster-whisper, not MLX. If a saved/auto
            # setting asks for mlx and it isn't importable, fall back gracefully.
            if effective_stt in ("mlx", "auto"):
                log.warning("STT %r unavailable (%s); falling back to faster-whisper", effective_stt, exc)
                effective_stt = "faster"
                stt_backend = _stt.make_stt_backend(_stt.STTConfig(backend=effective_stt, **kwargs))
            else:
                raise
        command_stt = stt_backend
        if effective_cmd:
            command_stt = _stt.make_stt_backend(_stt.STTConfig(backend=effective_cmd, **kwargs))

        tts_choice = self.config.tts_backend
        try:
            tts_backend = _tts.make_tts_backend(_tts.TTSConfig(
                backend=tts_choice,
                voice=os.environ.get("VOICE_AGENT_TTS_VOICE") or None,
                openai_api_key=os.environ.get("OPENAI_API_KEY"),
                openai_voice=os.environ.get("VOICE_AGENT_OPENAI_TTS_VOICE", "alloy"),
                azure_key=os.environ.get("AZURE_SPEECH_KEY"),
                azure_region=os.environ.get("AZURE_SPEECH_REGION"),
                azure_voice=os.environ.get("VOICE_AGENT_AZURE_TTS_VOICE", "en-US-AriaNeural"),
            ))
        except Exception:
            tts_backend = _tts.SilentTTS()

        device = hfv.resolve_input_device(self.config.device)
        import sounddevice as _sd
        try:
            self.device_name = _sd.query_devices(device)["name"]
        except Exception:
            self.device_name = str(device)

        # Pro features (entitlement-gated; core stays free).
        self.entitlement = pro.load_entitlement()
        macro_store = _macros.load_macros() if self.entitlement.has(pro.MACROS) else None
        if self.entitlement.has(pro.CUSTOM_WAKE_WORDS):
            hfv.register_custom_wake_words(hfv.load_custom_wake_words())
        tracker = _analytics.UsageTracker()
        jarvis_agent = _jarvis.make_agent()
        agent_runner = None
        try:
            import agent as _agent

            agent_runner = _agent.make_runner(tts_backend.speak)
        except Exception as exc:
            log.warning("agent init failed (%s); assistant runs chat-only", exc)
        self._agent = agent_runner

        ctx = hfv.DispatchContext(
            adapter=adapter,
            target_app=target_app,
            tts=tts_backend,
            command_backend=command_stt.name,
            macros=macro_store,
            tracker=tracker,
            jarvis=jarvis_agent,
            agent=agent_runner,
        )

        self.listen_backend = stt_backend.name
        self.command_backend = command_stt.name
        log.info("engine starting: device=%r resolved_name=%r listen=%s command=%s target=%r",
                 self.config.device, self.device_name, stt_backend.name,
                 command_stt.name, target_app or "(dynamic)")

        import sounddevice as sd
        import webrtcvad

        vad = webrtcvad.Vad(hfv.VAD_AGGRESSIVENESS)
        audio_q: queue.Queue = queue.Queue()
        utterance_q: queue.Queue = queue.Queue()
        wake_state = hfv.WakeState()

        def audio_cb(indata, frames, time_info, status):  # noqa: ARG001
            audio_q.put(indata.copy().flatten())

        self._stream = sd.InputStream(
            device=device,
            samplerate=hfv.SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=hfv.FRAME_SAMPLES,
            callback=audio_cb,
        )

        segmenter = VADSegmenter(
            vad=vad,
            sample_rate=hfv.SAMPLE_RATE,
            preroll_frames=hfv.PREROLL_FRAMES,
            min_speech_frames=hfv.MIN_SPEECH_FRAMES,
            max_silence_frames=hfv.MAX_SILENCE_FRAMES,
            max_utterance_frames=hfv.MAX_UTTERANCE_FRAMES,
        )

        def segment_loop() -> None:
            while not self._stop.is_set():
                try:
                    frame = audio_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                pcm = segmenter.process_frame(frame)
                if pcm is not None:
                    utterance_q.put(pcm)

        def worker() -> None:
            while not self._stop.is_set():
                try:
                    pcm = utterance_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                armed = wake_state.active_backend()
                backend = command_stt if armed else stt_backend
                try:
                    text = backend.transcribe(pcm, hfv.SAMPLE_RATE)
                except Exception as exc:
                    log.exception("transcribe failed")
                    self._emit(f"error: {exc}")
                    tts_backend.speak("error")
                    continue
                log.info("heard: %r", text)
                if not text or hfv.looks_hallucinated(text) or hfv.is_silence_hallucination(text):
                    continue

                # A running agent task consumes answers/confirmations/"stop".
                if ctx.agent is not None and ctx.agent.feed_user_speech(text):
                    continue

                armed = wake_state.active_backend()
                if armed:
                    wake, idx = hfv.detect_wake_word(text)
                    if wake:
                        cmd = hfv.extract_command(text, idx)
                        if cmd:
                            hfv._dispatch(ctx, wake, cmd)
                            self._emit(f"sent: {cmd[:40]}")
                            wake_state.clear()
                        else:
                            wake_state.arm(wake)
                            self._emit("listening…")
                            tts_backend.speak("listening")
                        continue
                    cmd = text.strip(" ,.!?;:")
                    if cmd:
                        hfv._dispatch(ctx, armed, cmd)
                        self._emit(f"sent: {cmd[:40]}")
                        wake_state.clear()
                    continue

                wake, idx = hfv.detect_wake_word(text)
                if not wake:
                    log.info("ignored (no wake word): %r", text)
                    continue
                cmd = hfv.extract_command(text, idx)
                if cmd:
                    log.info("dispatch wake=%s cmd=%r", wake, cmd)
                    hfv._dispatch(ctx, wake, cmd)
                    self._emit(f"sent: {cmd[:40]}")
                    wake_state.clear()
                else:
                    wake_state.arm(wake)
                    self._emit("listening…")
                    tts_backend.speak("listening")

        self._stream.start()
        self._threads = [
            threading.Thread(target=segment_loop, daemon=True),
            threading.Thread(target=worker, daemon=True),
        ]
        for t in self._threads:
            t.start()
        self.running = True
        self._emit("ready")

    def stop(self) -> None:
        """Stop listening and tear down the audio stream and threads."""
        if not self.running:
            return
        self._stop.set()
        if getattr(self, "_agent", None) is not None:
            try:
                self._agent.abort()
                self._agent.registry.close()
            except Exception:
                pass
            self._agent = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads = []
        self.running = False
        self._emit("stopped")
