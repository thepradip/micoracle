"""Mocked tests for the STT backend factory and auto-selection."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

import stt


class TestAutoSelect:
    def test_apple_silicon_selects_mlx(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        assert stt.auto_select_stt_backend() == "mlx"

    def test_apple_silicon_aarch64_selects_mlx(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("platform.machine", lambda: "aarch64")
        assert stt.auto_select_stt_backend() == "mlx"

    def test_intel_mac_selects_faster(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        assert stt.auto_select_stt_backend() == "faster"

    def test_linux_selects_faster(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        assert stt.auto_select_stt_backend() == "faster"

    def test_windows_selects_faster(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("platform.machine", lambda: "AMD64")
        assert stt.auto_select_stt_backend() == "faster"


class TestMakeBackendErrors:
    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown STT backend"):
            stt.make_stt_backend(stt.STTConfig(backend="bogus"))

    def test_mlx_on_non_mac_raises(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(RuntimeError, match="MLX Whisper runs only on macOS"):
            stt.make_stt_backend(stt.STTConfig(backend="mlx"))

    def test_mlx_on_intel_mac_raises(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        with pytest.raises(RuntimeError, match="MLX Whisper runs only on macOS"):
            stt.make_stt_backend(stt.STTConfig(backend="mlx"))

    def test_openai_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        fake_openai = MagicMock()
        fake_openai.OpenAI = MagicMock()
        with patch.dict(sys.modules, {"openai": fake_openai}):
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                stt.make_stt_backend(stt.STTConfig(backend="openai"))

    def test_azure_missing_creds_raises(self, monkeypatch):
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_KEY", raising=False)
        fake_openai = MagicMock()
        fake_openai.AzureOpenAI = MagicMock()
        with patch.dict(sys.modules, {"openai": fake_openai}):
            with pytest.raises(RuntimeError, match="Azure STT requires"):
                stt.make_stt_backend(stt.STTConfig(backend="azure"))


class TestMakeBackendSuccess:
    def test_openai_with_key_succeeds(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        fake_module = MagicMock()
        fake_module.OpenAI = MagicMock(return_value=MagicMock())
        with patch.dict(sys.modules, {"openai": fake_module}):
            backend = stt.make_stt_backend(stt.STTConfig(backend="openai"))
        assert backend.name == "openai"
        fake_module.OpenAI.assert_called_once_with(api_key="sk-test-key")

    def test_azure_with_creds_succeeds(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_KEY", "azure-key")
        fake_module = MagicMock()
        fake_module.AzureOpenAI = MagicMock(return_value=MagicMock())
        with patch.dict(sys.modules, {"openai": fake_module}):
            backend = stt.make_stt_backend(stt.STTConfig(
                backend="azure", azure_deployment="whisper",
            ))
        assert backend.name == "azure"
        fake_module.AzureOpenAI.assert_called_once()

    def test_auto_routes_to_faster_on_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        fake_module = MagicMock()
        fake_module.WhisperModel = MagicMock(return_value=MagicMock())
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            backend = stt.make_stt_backend(stt.STTConfig(backend="auto"))
        assert backend.name == "faster"

    def test_faster_missing_package_raises(self, monkeypatch):
        # Temporarily hide faster_whisper from sys.modules AND block its import.
        monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def mock_import(name, *args, **kwargs):
            if name == "faster_whisper":
                raise ImportError("no faster_whisper")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(RuntimeError, match="faster-whisper not installed"):
                stt.make_stt_backend(stt.STTConfig(backend="faster"))
