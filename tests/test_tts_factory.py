"""Mocked tests for the TTS backend factory and auto-selection."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

import tts


class TestAutoSelect:
    def test_mac_selects_say(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        assert tts.auto_select_tts_backend() == "say"

    def test_linux_selects_pyttsx3(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        assert tts.auto_select_tts_backend() == "pyttsx3"

    def test_windows_selects_pyttsx3(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        assert tts.auto_select_tts_backend() == "pyttsx3"


class TestSilent:
    def test_none_returns_silent_backend(self):
        backend = tts.make_tts_backend(tts.TTSConfig(backend="none"))
        assert isinstance(backend, tts.SilentTTS)
        assert backend.name == "none"

    def test_silent_speak_is_noop(self):
        backend = tts.SilentTTS()
        # Should not raise or block on any input.
        backend.speak("anything")
        backend.speak("")


class TestMakeBackendErrors:
    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown TTS backend"):
            tts.make_tts_backend(tts.TTSConfig(backend="gibberish"))

    def test_say_on_non_mac_raises(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(RuntimeError, match="macOS only"):
            tts.make_tts_backend(tts.TTSConfig(backend="say"))

    def test_openai_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        fake_module = MagicMock()
        fake_module.OpenAI = MagicMock()
        with patch.dict(sys.modules, {"openai": fake_module}):
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                tts.make_tts_backend(tts.TTSConfig(backend="openai"))

    def test_azure_missing_creds_raises(self, monkeypatch):
        monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
        monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
        with pytest.raises(RuntimeError, match="Azure TTS requires"):
            tts.make_tts_backend(tts.TTSConfig(backend="azure"))


class TestMakeBackendSuccess:
    def test_say_on_mac_ok(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        backend = tts.make_tts_backend(tts.TTSConfig(backend="say"))
        assert backend.name == "say"

    def test_say_speak_invokes_subprocess(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        backend = tts.MacSayTTS()
        fake_popen = MagicMock()
        with patch("subprocess.Popen", fake_popen):
            backend.speak("listening")
        fake_popen.assert_called_once()
        called_args = fake_popen.call_args[0][0]
        assert called_args[0] == "say"
        assert "listening" in called_args

    def test_say_speak_with_custom_voice(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        backend = tts.MacSayTTS(voice="Ava (Premium)")
        fake_popen = MagicMock()
        with patch("subprocess.Popen", fake_popen):
            backend.speak("hi")
        called_args = fake_popen.call_args[0][0]
        assert "-v" in called_args
        assert "Ava (Premium)" in called_args

    def test_say_speak_empty_string_is_noop(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        backend = tts.MacSayTTS()
        fake_popen = MagicMock()
        with patch("subprocess.Popen", fake_popen):
            backend.speak("")
        fake_popen.assert_not_called()

    def test_openai_tts_with_key_initializes(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
        fake_module = MagicMock()
        fake_module.OpenAI = MagicMock(return_value=MagicMock())
        with patch.dict(sys.modules, {"openai": fake_module}):
            backend = tts.make_tts_backend(tts.TTSConfig(backend="openai"))
        assert backend.name == "openai"

    def test_auto_on_mac_returns_say(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        backend = tts.make_tts_backend(tts.TTSConfig(backend="auto"))
        assert backend.name == "say"
