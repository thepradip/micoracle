# License ledger

`ledger.csv` is the record of every Pro license minted with
`tools/sign_license.py`. Append one row each time you issue a key.

| Column | Meaning |
|---|---|
| `id` | The `id` field baked into the token (`--id`, or a random 12-char hex). The only way to tie a leaked key back to a customer. |
| `email` | Licensee email (matches the token payload). |
| `tier` | `pro` or `team`. |
| `seats` | Seat count. |
| `issued` | ISO date the key was minted. |
| `expires` | ISO date, or `perpetual`. |
| `notes` | Order ref, refund status, revocation, etc. |

## Privacy note

This file contains customer emails. It is acceptable in a **private** repo for
a solo operator, but:

- never copy it into the public repo,
- if the team grows, move issuance records to a proper system (Stripe metadata,
  a licensing DB, keygen.sh) and stop committing PII to git.

## Revocation

Offline licenses cannot be remotely revoked without a key rotation (which
invalidates everyone). For a single bad actor, the practical options are: let
the term expire, or rotate the signing key and re-issue to everyone else. Track
the offending `id` here in `notes` either way.
