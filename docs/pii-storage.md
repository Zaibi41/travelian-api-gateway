# Passenger identity data: storage, masking and reveal

**Sandbox phase:** each traveler's date of birth and passport / national ID number are stored as
plain text in `booking_travelers` (migration 0015). There are no encryption keys or settings;
holds and the Passengers tab work with no extra setup.

- **Masked on read.** Every API response that includes travelers carries masked values only,
  computed when the data is read (`app/core/pii.py`): document number last 3 characters, date of
  birth and document expiry year only, plus a document validity status (valid, expiring within
  6 months, expired). This applies to every role.
- **Reveal is logged.** The full values come only from
  `POST /v1/admin/bookings/{id}/travelers/{position}/reveal`, which needs the `reveal_identity`
  permission and a reason. Each reveal writes an `identity_revealed` audit event (who, which
  traveler, which fields, the reason) with no values in it.
- **Never logged.** Identity values don't appear in logs or audit details.

**Before any real customer data:** decide and apply database protection for these values. This
is a go-live blocker in the frontend repo's `docs/roadmap.md`.
