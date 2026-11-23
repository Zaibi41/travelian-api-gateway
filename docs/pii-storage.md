# Passenger identity data: storage, saved people and reads

**Encryption at rest (gateway#84):** passport / national ID numbers and dates of birth are encrypted once the
identity keys are set (see "Encryption at rest" below). Without keys (sandbox) they are stored as plain text and
holds, the Passengers tab and saved people work with no extra setup.

## Where it is stored
- **Booking copies** (`booking_customers`, `booking_travelers`, migrations 0011, 0015): each
  booking keeps its own copy of the customer and travelers as they were when booked: date of
  birth, passport / national ID number, country, nationality, expiry. Editing a saved person
  never changes a past booking. `person_id` / `document_id` link a copy to the saved person
  (nullable; nothing else on a past booking changes).
- **Saved people** (`saved_people`, `saved_person_documents`, `saved_person_events`, migration
  0017): one record per real person, a customer, a traveler or both, with any number of
  documents (CNIC, passport, a renewed passport). Every record belongs to Travelian or to one
  agency; a caller only ever sees its own owner's people (back office works in Travelian's scope).
  Details come from this database, never from Cognito.

## Matching
- Documents are compared normalised: CNIC (Pakistani national ID) digits only, dashes and spaces
  dropped; passports and other IDs upper-case letters and digits. Match key: owner + ID type +
  document country + normalised number; one live document per key and owner.
- Search: an exact full document number first, then phone or email (normalised), then name prefix.
- A document already saved for a person with a different name or date of birth is refused
  (409 `document_belongs_to_another_person`).
- Same name and date of birth (or, for customers, the same email or phone) without a matching
  document is a **possible duplicate**: nothing is merged automatically. Staff choose (add to that
  person, or keep separate) or merge two people deliberately (permission, reason, logged).

## Full values on read (no masking)
- Back-office API responses carry the stored values in full: document number, date of birth, document
  expiry (with a validity status: valid, expiring within 6 months, expired), phone and email. This
  applies in search, lists, details and the itinerary PDF/PNG (decided by Zohaib, 16 November 2026;
  masking, the reveal endpoint, its audit event and the `reveal_identity` permission were removed).
- Identity values are still never written to logs or booking events.
- A hold's possible duplicate (`needs_choice`) is resolved with
  `POST /v1/admin/bookings/{id}/saved-people/{role}/{position}/resolve`: the gateway reads the values
  from that booking's own customer or traveler row, so the browser never resends identity values.
- In a hold, a saved traveler (`traveler_id`) or saved customer (`customer_id`) is loaded on the
  server; the browser never receives the stored identity.
- Identity values don't appear in logs or audit details.

## Encryption at rest (gateway#84)
- **What:** `booking_travelers.date_of_birth` / `document_number`, `saved_people.date_of_birth` and
  `saved_person_documents.document_number` (migration 0035 adds `*_enc` columns). Names, phone, email, nationality,
  ID type, document country and expiry stay plain (expiry drives the "documents expiring" checks).
- **How:** AES-256-GCM in the gateway (`app/core/pii.py`), a fresh nonce per value, the column name as associated data
  (a value copied into another column doesn't decrypt). Stored as `<key id>:<base64>`. Matching uses keyed hashes
  (HMAC-SHA256 with a separate key): `saved_person_documents.document_number_hmac` (the normalised number; the
  per-owner unique key) and `saved_people.date_of_birth_hmac` (same name + date of birth duplicates). Encrypted rows
  have their plain columns empty.
- **Keys:** never in the repo. `PII_KEYS` (`<id>:<base64 32-byte key>`, several ids during a rotation),
  `PII_ACTIVE_KEY` (the id new values use), `PII_HASH_KEY` (base64, 32+ bytes). From the environment or Parameter
  Store (`<prefix>/identity/keys`, `identity/active-key`, `identity/hash-key`, SecureString). All empty: encryption
  off. Some set but not all (or invalid): saving identity is refused rather than stored plain. Losing a key loses
  the values encrypted with it: keep a copy in the password manager.
- **Turning it on:** run migration 0035, set the three keys, restart; new values are encrypted. Then
  `python -m app.cli.encrypt_identity` (dry run, counts) and `--apply` to encrypt existing rows. Safe to repeat;
  the gateway reads plain and encrypted rows alike meanwhile.
- **Rotation:** add the new key to `PII_KEYS`, make it `PII_ACTIVE_KEY`, restart, run `--apply` (re-encrypts rows on
  older keys), then remove the old key. The hash key is not rotated by the CLI (changing it needs `--decrypt --apply`
  first, then `--apply` with the new hash key).
- **Back to plain:** `--decrypt --apply` (needed before downgrading migration 0035).
- **Trade-off:** once encrypted, a document number is found by its full number only (exact search and the hold
  form's typeahead); a partial number no longer matches. Names, phone and email search are unchanged.
