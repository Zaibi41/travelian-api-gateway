# Traveler identity encryption: keys, backup and rotation

Date of birth and passport / ID number of every traveler on a booking are encrypted in the
gateway database (`booking_travelers`, migration 0011) with AES-256-GCM (`app/core/pii.py`).
Each value is bound to its booking, traveler and field, and carries the id of the key that
encrypted it. Reads return a masked copy (birth year only; last 3 characters of the document).
The full values come only from `POST /v1/admin/bookings/{id}/travelers/{position}/reveal`,
which needs the `reveal_identity` permission and a reason, and is logged in `booking_events`.

Names, title, passenger type, ID type, document country, nationality, document expiry and the
customer's contact details are not encrypted (they are needed for search, contact and the
airline report); they are limited by role like the rest of the booking.

## Settings (environment only; never in the repo, never printed)

| Variable | Value |
| --- | --- |
| `TRAVELIAN_PII_KEYS` | `<key id>:<base64 32-byte key>`, comma-separated when there are several (rotation) |
| `TRAVELIAN_PII_ACTIVE_KEY` | the key id new values are encrypted with |
| `TRAVELIAN_PII_HASH_KEY` | base64, at least 32 bytes; keys the document hash used for duplicate checks |

Locally they go in the gateway `.env`; on Lightsail in the service environment; later in AWS
Parameter Store (SecureString). Without them the gateway refuses holds before calling the
supplier ("Traveler details can't be stored securely right now. Nothing was booked."), and
reveals return 503.

Generate a key (prints it once; copy it straight into the secret store, not into chat or a ticket):

```bash
.venv/bin/python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
```

Key ids: short and dated, e.g. `k2026a`. Use a separate value for the hash key.

## Backup

- **Losing a key makes every value encrypted with it unreadable.** There is no recovery.
- Keep each key (with its id) in two places: Parameter Store (SecureString, KMS-encrypted) and
  the company password manager, under an entry readable only by the owners. Record the key id
  and the date it became active; never the key in email, chat, tickets or the repo.
- Database backups contain only ciphertext; restoring one also needs the keys that were active
  when the rows were written. Keep retired keys as long as any backup that uses them is kept.
- The hash key is needed to match duplicates, not to read data. Losing it means rehashing
  document numbers (decrypt and hash again), not data loss.

## Rotation

1. Generate a new key with a new id.
2. Add it to `TRAVELIAN_PII_KEYS` (keep the old ones) and set `TRAVELIAN_PII_ACTIVE_KEY` to the new id.
   Restart. New values use the new key; old values still decrypt with their own key id.
3. Re-encrypt old rows when needed (rows per key: `SELECT key_id, count(*) FROM booking_travelers GROUP BY key_id`).
   A re-encryption command is not built yet; until it is, keep old keys in `TRAVELIAN_PII_KEYS`.
4. Remove an old key only when no row and no kept backup uses it. Removing it too early makes
   those values unreadable (reveal returns 503); it never returns wrong data.

On suspected key exposure: rotate at once, re-encrypt, then remove the exposed key, and treat
the data encrypted with it as exposed.

## What is never logged

Keys, plaintext, ciphertext, document numbers, dates of birth and contact details. Reveal
events record who, which traveler, which fields and the reason.
