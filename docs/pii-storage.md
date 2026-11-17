# Passenger identity data: storage, saved people and reads

**Sandbox phase:** identity data is stored as plain text. There are no encryption keys or
settings; holds, the Passengers tab and saved people work with no extra setup.

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

**Before any real customer data:** decide and apply database protection for these values. This
is a go-live blocker in the frontend repo's `docs/roadmap.md`.
