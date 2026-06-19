#!/usr/bin/env python3
"""Vendor-side license minter for MicOracle Pro.

Holds the Ed25519 *private* key and issues signed license tokens that the
client verifies offline against the public key embedded in ``pro.py``. Keep
the private key secret — anyone with it can mint licenses.

Usage::

    # First run with no key present: generate a keypair.
    python tools/sign_license.py --keygen

    # Then mint licenses:
    python tools/sign_license.py --email user@acme.com --tier pro --days 365
    python tools/sign_license.py --email team@acme.com --tier team --seats 10 --days 365

The private key lives at ``~/.micoracle/signing_key.pem`` (override with
``--key``). After ``--keygen`` it prints the public key block to paste into
``pro.PUBLIC_KEY_PEM``.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

# Make the repo root importable so we can reuse pro.encode_license.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pro  # noqa: E402

DEFAULT_KEY_PATH = Path.home() / ".micoracle" / "signing_key.pem"


def _keygen(path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    print(f"Private key written to {path} (mode 600).\n")
    print("Paste this into pro.PUBLIC_KEY_PEM:\n")
    print(pub_pem)


def _load_signer(path: Path):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv = load_pem_private_key(path.read_bytes(), password=None)
    return priv.sign


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint MicOracle Pro licenses.")
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY_PATH,
                        help="Private signing key path.")
    parser.add_argument("--keygen", action="store_true",
                        help="Generate a new signing keypair and exit.")
    parser.add_argument("--email", help="Licensee email.")
    parser.add_argument("--tier", default="pro", choices=["pro", "team"],
                        help="License tier.")
    parser.add_argument("--seats", type=int, default=1, help="Seat count.")
    parser.add_argument("--days", type=int, default=365,
                        help="Validity in days (0 = perpetual).")
    parser.add_argument("--id", default=None, help="License id (default: random).")
    args = parser.parse_args()

    if args.keygen:
        _keygen(args.key)
        return 0

    if not args.key.exists():
        print(f"No signing key at {args.key}. Run with --keygen first.",
              file=sys.stderr)
        return 1
    if not args.email:
        print("--email is required to mint a license.", file=sys.stderr)
        return 1

    issued = int(time.time())
    expires = None if args.days <= 0 else issued + args.days * 86400
    payload = {
        "email": args.email,
        "tier": args.tier,
        "seats": max(1, args.seats),
        "issued": issued,
        "expires": expires,
        "id": args.id or uuid.uuid4().hex[:12],
    }
    license_str = pro.encode_license(payload, _load_signer(args.key))

    # Confirm it verifies against the embedded public key before handing it out.
    try:
        ent = pro.verify_license(license_str)
    except pro.LicenseError as exc:
        print(f"WARNING: minted license does not verify against the embedded "
              f"public key ({exc}). Did you update pro.PUBLIC_KEY_PEM?",
              file=sys.stderr)
        return 1

    print(license_str)
    print(f"\n# {ent.describe()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
