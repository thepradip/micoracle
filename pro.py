"""Pro-tier entitlements and fully offline license verification.

MicOracle's core — wake words, every STT/TTS backend, and dispatch — is
MIT-licensed and free forever. A small set of power-user features is gated
behind a paid license:

  * voice macros (spoken shortcuts that expand into full prompt templates)
  * custom wake words (your own phrases beyond claude / codex / micoracle)
  * analytics export (CSV / JSON dumps of the local usage log)
  * team config sync (the Team tier)

Licenses are Ed25519-signed tokens verified entirely on-device against an
embedded public key. Nothing is contacted at runtime — no license server,
no phone-home, consistent with the project's privacy stance. A license looks
like::

    MICO1.<base64url(payload)>.<base64url(signature)>

where ``payload`` is compact JSON::

    {"email": "...", "tier": "pro", "seats": 1,
     "issued": 1750000000, "expires": 1781536000, "id": "..."}

Vendors mint licenses with ``tools/sign_license.py`` (which holds the private
key). Verification here needs the ``cryptography`` package; without it Pro
features stay locked and the user is told to ``pip install micoracle[pro]``.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import paths

# ─────────────────────────── feature flags ────────────────────────

MACROS = "macros"
CUSTOM_WAKE_WORDS = "custom_wake_words"
ANALYTICS_EXPORT = "analytics_export"
CONFIG_SYNC = "config_sync"

# Features unlocked per tier. "free" is the baseline (empty) — the core
# product needs no entitlement. Higher tiers are supersets.
_PRO_FEATURES = frozenset({MACROS, CUSTOM_WAKE_WORDS, ANALYTICS_EXPORT})
_TEAM_FEATURES = _PRO_FEATURES | {CONFIG_SYNC}

TIER_FEATURES: dict[str, frozenset[str]] = {
    "free": frozenset(),
    "pro": _PRO_FEATURES,
    "team": frozenset(_TEAM_FEATURES),
}

LICENSE_PREFIX = "MICO1"

# Embedded verification key. Replace this with the public key printed by
# tools/sign_license.py if you re-generate the signing keypair.
PUBLIC_KEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAF68y8+c+Sx4xR55QPP1lc0qLTmd1mh4K/p2I9vjw91M=
-----END PUBLIC KEY-----
"""


class LicenseError(Exception):
    """Raised for malformed, expired, or unverifiable licenses."""


@dataclass(frozen=True)
class Entitlement:
    """What the current user is allowed to do."""

    tier: str = "free"
    email: str | None = None
    seats: int = 1
    expires: int | None = None
    license_id: str | None = None
    features: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_pro(self) -> bool:
        return self.tier != "free"

    def has(self, feature: str) -> bool:
        return feature in self.features

    def describe(self) -> str:
        if not self.is_pro:
            return "Free (MIT core)"
        who = f" · {self.email}" if self.email else ""
        when = ""
        if self.expires:
            when = f" · renews {time.strftime('%Y-%m-%d', time.gmtime(self.expires))}"
        seats = f" · {self.seats} seats" if self.seats > 1 else ""
        return f"{self.tier.capitalize()}{who}{seats}{when}"


FREE = Entitlement(tier="free", features=TIER_FEATURES["free"])


# ─────────────────────────── token codec ──────────────────────────


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def encode_license(payload: dict, sign) -> str:
    """Build a license string from ``payload`` using a ``sign(bytes)->bytes``.

    Used by the vendor-side signing tool; kept here so the encode/decode pair
    lives in one place and can be unit-tested together.
    """
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64url_encode(sign(body.encode("ascii")))
    return f"{LICENSE_PREFIX}.{body}.{signature}"


# ─────────────────────────── verification ─────────────────────────


def _load_public_key(public_key_pem: str):
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise LicenseError(
            "License verification needs the 'cryptography' package. "
            "Install it with:  pip install micoracle[pro]"
        ) from exc
    return load_pem_public_key(public_key_pem.encode())


def verify_license(
    key: str,
    *,
    public_key_pem: str | None = None,
    now: float | None = None,
) -> Entitlement:
    """Verify a license string and return its :class:`Entitlement`.

    Raises :class:`LicenseError` for any malformed, tampered, expired, or
    unverifiable token. Callers that want a graceful fallback should catch it
    (see :func:`load_entitlement`). ``public_key_pem`` defaults to the embedded
    key; it is resolved at call time so the constant stays patchable in tests.
    """
    from cryptography.exceptions import InvalidSignature

    public_key_pem = public_key_pem or PUBLIC_KEY_PEM
    key = (key or "").strip()
    parts = key.split(".")
    if len(parts) != 3 or parts[0] != LICENSE_PREFIX:
        raise LicenseError("Not a MicOracle license token.")

    _, body, signature = parts
    pub = _load_public_key(public_key_pem)
    try:
        pub.verify(_b64url_decode(signature), body.encode("ascii"))
    except InvalidSignature as exc:
        raise LicenseError("License signature is invalid.") from exc
    except Exception as exc:  # malformed base64, wrong key type, etc.
        raise LicenseError(f"License could not be verified: {exc}") from exc

    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise LicenseError("License payload is corrupt.") from exc

    tier = str(payload.get("tier", "free")).lower()
    if tier not in TIER_FEATURES:
        raise LicenseError(f"Unknown license tier: {tier!r}")

    expires = payload.get("expires")
    if expires is not None:
        clock = time.time() if now is None else now
        if clock > float(expires):
            raise LicenseError("License has expired.")

    return Entitlement(
        tier=tier,
        email=payload.get("email"),
        seats=int(payload.get("seats", 1) or 1),
        expires=int(expires) if expires is not None else None,
        license_id=payload.get("id"),
        features=TIER_FEATURES[tier],
    )


# ─────────────────────────── discovery ────────────────────────────


def _read_license_file() -> str | None:
    path = paths.config_path("license")
    if path.exists():
        text = path.read_text().strip()
        if text:
            return text
    return None


def load_entitlement(*, now: float | None = None) -> Entitlement:
    """Find and verify a license, returning :data:`FREE` if none is valid.

    Lookup order: ``MICORACLE_LICENSE`` env var, then ``<config>/license``.
    Never raises — an invalid license degrades to the free tier so the core
    product always runs.
    """
    import os

    key = os.environ.get("MICORACLE_LICENSE", "").strip() or _read_license_file()
    if not key:
        return FREE
    try:
        return verify_license(key, now=now)
    except LicenseError:
        return FREE


def save_license(key: str, *, now: float | None = None) -> Entitlement:
    """Verify ``key`` and persist it to ``<config>/license`` on success.

    Returns the resulting entitlement. Raises :class:`LicenseError` if the
    key does not verify, so the caller can report a precise reason.
    """
    ent = verify_license(key, now=now)
    path = paths.config_path("license")
    path.write_text(key.strip() + "\n")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return ent


def require(entitlement: Entitlement, feature: str) -> bool:
    """Return True if ``feature`` is unlocked. Convenience for call sites."""
    return entitlement.has(feature)
