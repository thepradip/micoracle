"""Integration tests for the CLI subcommands and the dispatch path.

These exercise the Pro wiring end to end without audio hardware: mint a license
with an ephemeral key, activate it through the real ``license`` subcommand, then
drive ``macros`` / ``stats`` and a simulated dispatch.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import analytics
import hands_free_voice as hfv
import macros as macros_mod
import pro


@pytest.fixture()
def pro_env(monkeypatch, tmp_path):
    """A temp config home with an activated Pro license."""
    monkeypatch.setenv("MICORACLE_HOME", str(tmp_path))
    monkeypatch.delenv("MICORACLE_LICENSE", raising=False)
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setattr(pro, "PUBLIC_KEY_PEM", pub)
    key = pro.encode_license(
        {"email": "dev@acme.com", "tier": "pro", "seats": 1,
         "issued": 1, "expires": None, "id": "cli1"},
        priv.sign,
    )
    return key, tmp_path


class TestLicenseCommand:
    def test_activate_and_status(self, pro_env, capsys):
        key, _ = pro_env
        assert hfv._cmd_license([key]) == 0
        assert "activated" in capsys.readouterr().out.lower()
        # Status reflects the persisted license.
        assert hfv._cmd_license([]) == 0
        assert "Pro" in capsys.readouterr().out

    def test_bad_license_returns_error(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("MICORACLE_HOME", str(tmp_path))
        assert hfv._cmd_license(["MICO1.nope.sig"]) == 1
        assert "rejected" in capsys.readouterr().err.lower()


class TestMacrosCommand:
    def test_init_then_list(self, pro_env, capsys):
        key, _ = pro_env
        hfv._cmd_license([key])
        capsys.readouterr()
        assert hfv._cmd_macros(["--init"]) == 0
        assert "starter" in capsys.readouterr().out.lower()
        assert hfv._cmd_macros([]) == 0
        listing = capsys.readouterr().out
        assert "refactor this" in listing

    def test_list_without_macros(self, pro_env, capsys):
        assert hfv._cmd_macros([]) == 0
        assert "No macros" in capsys.readouterr().out


class TestStatsCommand:
    def test_stats_empty(self, pro_env, capsys):
        assert hfv._cmd_stats([]) == 0
        assert "No usage" in capsys.readouterr().out

    def test_export_requires_pro(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("MICORACLE_HOME", str(tmp_path))
        monkeypatch.delenv("MICORACLE_LICENSE", raising=False)
        # No license -> free -> export gated.
        assert hfv._cmd_stats(["--export", "json"]) == 2
        assert "Pro" in capsys.readouterr().err

    def test_export_json_with_pro(self, pro_env, capsys):
        key, tmp_path = pro_env
        hfv._cmd_license([key])
        capsys.readouterr()
        # Seed one usage event.
        analytics.UsageTracker(tmp_path / "usage.jsonl").record(
            wake="claude", text="hello world", backend="mlx", now=1.0)
        assert hfv._cmd_stats(["--export", "json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data[0]["wake"] == "claude"


class FakeAdapter:
    """Minimal platform adapter that records what was dispatched."""

    supported_apps = {"Terminal"}

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def paste_and_return(self, text, target):
        self.sent.append((text, target))


class SilentTTS:
    def speak(self, phrase):  # noqa: ARG002
        pass


class TestDispatchPath:
    def _ctx(self, tmp_path, macro_store):
        return hfv.DispatchContext(
            adapter=FakeAdapter(),
            target_app="Terminal",
            tts=SilentTTS(),
            command_backend="groq",
            macros=macro_store,
            tracker=analytics.UsageTracker(tmp_path / "usage.jsonl"),
        )

    def test_dispatch_expands_macro_and_tracks(self, tmp_path):
        store = macros_mod.MacroStore([
            macros_mod.Macro("write tests for", "Write tests for {args}."),
        ])
        ctx = self._ctx(tmp_path, store)
        hfv._dispatch(ctx, "claude", "write tests for the parser")
        # The expanded text reached the adapter.
        assert ctx.adapter.sent[0][0] == "Write tests for the parser."
        # And the event log records the macro by trigger.
        ev = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
        assert ev["macro"] == "write tests for"
        assert ev["backend"] == "groq"

    def test_dispatch_without_macros_passes_through(self, tmp_path):
        ctx = self._ctx(tmp_path, None)
        hfv._dispatch(ctx, "codex", "just say this verbatim")
        assert ctx.adapter.sent[0][0] == "just say this verbatim"
        ev = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
        assert ev["macro"] is None
