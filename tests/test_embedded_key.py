"""Maintainer smoke test: the embedded public key must match the signing key.

Skips when the private signing key is not present (i.e. on any machine other
than the vendor's), so CI and contributors are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pro

SIGNING_KEY = Path.home() / ".micoracle" / "signing_key.pem"


@pytest.mark.skipif(not SIGNING_KEY.exists(), reason="vendor signing key absent")
def test_embedded_public_key_matches_signing_key():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv = load_pem_private_key(SIGNING_KEY.read_bytes(), password=None)
    key = pro.encode_license(
        {"email": "smoke@test", "tier": "pro", "seats": 1,
         "issued": 1, "expires": None, "id": "smoke"},
        priv.sign,
    )
    # Uses the embedded PUBLIC_KEY_PEM (no override) — must verify.
    ent = pro.verify_license(key)
    assert ent.tier == "pro"
