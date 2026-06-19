# MicOracle Pro — Licensing Operations

> **Private repo.** This is the full version of MicOracle (free MIT core + the
> paid Pro tier). The public `micoracle` repo ships the free core only. Keep
> this repo private — it documents how licenses are minted.

## How licensing works

Licenses are **Ed25519-signed tokens verified entirely offline**. There is no
license server and nothing is contacted at runtime.

```
MICO1.<base64url(payload)>.<base64url(signature)>
```

- The **private** signing key mints licenses. It lives at
  `~/.micoracle/signing_key.pem` (mode 600) and is **never committed** —
  `.gitignore` blocks `signing_key.pem`.
- The **public** key is embedded in `pro.PUBLIC_KEY_PEM` and ships in every
  install. It only verifies; it cannot mint.

Security rests on the secrecy of the private key, not the code. The
verification logic and signing tool are safe to keep in this repo.

## Minting a license

```bash
# Pro, 1 seat, 1 year
python tools/sign_license.py --email customer@acme.com --tier pro --days 365

# Team, 10 seats, 1 year
python tools/sign_license.py --email team@acme.com --tier team --seats 10 --days 365

# Perpetual (no expiry)
python tools/sign_license.py --email customer@acme.com --tier pro --days 0
```

The token is printed to stdout; the human-readable summary goes to stderr. The
tool refuses to emit a token that does not verify against the embedded public
key, so a key/code mismatch fails loudly.

After issuing, **append a row to `licenses/ledger.csv`** (see below) so you can
track and, if needed, identify a leaked key by its `id`.

## Rotating the signing key

If the private key is lost or compromised:

```bash
python tools/sign_license.py --keygen      # writes a new key, prints the public block
```

Then paste the printed public key into `pro.PUBLIC_KEY_PEM` and ship a release.
**All previously issued licenses stop verifying** once the public key changes —
plan a migration (re-issue to active customers) before rotating.

## Tiers and features

| Tier | Features unlocked (`pro.TIER_FEATURES`) |
|---|---|
| `free` | core only — wake words, all STT/TTS backends, dispatch, basic stats |
| `pro`  | `macros`, `custom_wake_words`, `analytics_export` |
| `team` | everything in `pro` + `config_sync` |

## Customer activation (support reference)

```bash
pip install micoracle[pro]      # installs cryptography + pyyaml
micoracle license MICO1...      # activate; persists to ~/.micoracle/license
micoracle license               # show current tier
```

`MICORACLE_LICENSE` env var also works and takes precedence over the file.

## Files that matter here

| Path | Role |
|---|---|
| `tools/sign_license.py` | Vendor minter. Loads the private key from disk. |
| `pro.py` | Entitlements + offline verification. Holds the embedded **public** key. |
| `macros.py` / `analytics.py` | Pro feature implementations. |
| `licenses/ledger.csv` | Issued-license log (see privacy note in `licenses/README.md`). |
