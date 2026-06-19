"""Tests for Pro entitlement and offline license verification."""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import pro


@pytest.fixture()
def keypair():
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub_pem


def _mint(priv, **payload):
    return pro.encode_license(payload, priv.sign)


class TestVerifyLicense:
    def test_valid_pro_license(self, keypair):
        priv, pub = keypair
        key = _mint(priv, email="a@b.com", tier="pro", seats=1,
                    issued=1, expires=None, id="x1")
        ent = pro.verify_license(key, public_key_pem=pub)
        assert ent.tier == "pro"
        assert ent.is_pro
        assert ent.has(pro.MACROS)
        assert ent.has(pro.CUSTOM_WAKE_WORDS)
        assert not ent.has(pro.CONFIG_SYNC)

    def test_team_includes_config_sync(self, keypair):
        priv, pub = keypair
        key = _mint(priv, email="t@b.com", tier="team", seats=10,
                    issued=1, expires=None, id="t1")
        ent = pro.verify_license(key, public_key_pem=pub)
        assert ent.seats == 10
        assert ent.has(pro.CONFIG_SYNC)
        assert ent.has(pro.MACROS)

    def test_expired_license_rejected(self, keypair):
        priv, pub = keypair
        key = _mint(priv, email="a@b.com", tier="pro", expires=1000)
        with pytest.raises(pro.LicenseError, match="expired"):
            pro.verify_license(key, public_key_pem=pub, now=2000)

    def test_not_yet_expired(self, keypair):
        priv, pub = keypair
        key = _mint(priv, email="a@b.com", tier="pro", expires=5000)
        ent = pro.verify_license(key, public_key_pem=pub, now=1000)
        assert ent.is_pro

    def test_tampered_signature_rejected(self, keypair):
        priv, pub = keypair
        key = _mint(priv, email="a@b.com", tier="pro")
        body, sig = key.rsplit(".", 1)
        tampered = body + "." + ("A" * len(sig))
        with pytest.raises(pro.LicenseError):
            pro.verify_license(tampered, public_key_pem=pub)

    def test_tampered_payload_rejected(self, keypair):
        priv, pub = keypair
        key = _mint(priv, email="a@b.com", tier="pro")
        prefix, body, sig = key.split(".")
        # Flip a char in the payload; signature no longer matches.
        body = ("Z" if body[0] != "Z" else "Y") + body[1:]
        with pytest.raises(pro.LicenseError):
            pro.verify_license(f"{prefix}.{body}.{sig}", public_key_pem=pub)

    def test_wrong_prefix_rejected(self, keypair):
        _, pub = keypair
        with pytest.raises(pro.LicenseError, match="Not a MicOracle"):
            pro.verify_license("NOPE.aaa.bbb", public_key_pem=pub)

    def test_unknown_tier_rejected(self, keypair):
        priv, pub = keypair
        key = _mint(priv, email="a@b.com", tier="enterprise")
        with pytest.raises(pro.LicenseError, match="tier"):
            pro.verify_license(key, public_key_pem=pub)


class TestEntitlement:
    def test_free_default(self):
        assert pro.FREE.tier == "free"
        assert not pro.FREE.is_pro
        assert not pro.FREE.has(pro.MACROS)

    def test_describe_free(self):
        assert "Free" in pro.FREE.describe()


class TestLoadAndSave:
    def test_load_returns_free_without_license(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MICORACLE_HOME", str(tmp_path))
        monkeypatch.delenv("MICORACLE_LICENSE", raising=False)
        assert pro.load_entitlement().tier == "free"

    def test_load_returns_free_on_garbage(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MICORACLE_HOME", str(tmp_path))
        monkeypatch.setenv("MICORACLE_LICENSE", "garbage-not-a-license")
        assert pro.load_entitlement().tier == "free"

    def test_save_then_load_roundtrip(self, monkeypatch, tmp_path, keypair):
        priv, pub = keypair
        monkeypatch.setenv("MICORACLE_HOME", str(tmp_path))
        monkeypatch.delenv("MICORACLE_LICENSE", raising=False)
        monkeypatch.setattr(pro, "PUBLIC_KEY_PEM", pub)
        key = _mint(priv, email="dev@acme.com", tier="pro", expires=None)
        ent = pro.save_license(key)
        assert ent.email == "dev@acme.com"
        # A fresh load reads the persisted file.
        assert pro.load_entitlement().tier == "pro"

    def test_save_rejects_bad_license(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MICORACLE_HOME", str(tmp_path))
        with pytest.raises(pro.LicenseError):
            pro.save_license("MICO1.bad.sig")


class TestEmbeddedKey:
    def test_default_key_path_uses_embedded(self, keypair):
        # When public_key_pem is omitted, the embedded constant is used; an
        # ephemeral-key token must therefore fail against it.
        priv, _ = keypair
        key = _mint(priv, email="a@b.com", tier="pro")
        with pytest.raises(pro.LicenseError):
            pro.verify_license(key)
