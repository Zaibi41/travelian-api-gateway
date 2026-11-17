# Agent guidance

## Identifier conventions

- Generate new Travelian-owned public resource IDs in canonical, lowercase, hyphenated UUID format. Use `str(uuid4())` for newly created resources and `str(uuid5(...))` only when stable deterministic identity is required.
- Do not add resource-name prefixes such as `agency-` or return UUIDs using `.hex`.
- Preserve external identifiers exactly as supplied, including supplier booking IDs, PNRs, Cognito subjects and app-client IDs. Never convert them to Travelian UUIDs.
- Internal audit/event sequence primary keys may remain integers when they are not public resource identifiers.
- Preserve existing identifiers and relationships. When changing an exposed identifier format, document the compatibility impact, retain legacy lookup support where applicable, and use reviewed migrations if stored values or foreign keys must change. Do not silently rewrite existing records.
- Add regression coverage for new public ID generation and any legacy compatibility affected by the change.

## Project structure

- This repository is the Travelian API gateway.
- Keep Live Ticketing, Group Ticketing, and Hotels as separate API domains.
- Keep supplier integrations isolated under `app/vendors/<product>/<supplier_name>/` and normalize supplier responses at that boundary.
- Record each vendor's success, failure, or timeout separately. Do not let one vendor failure hide another vendor's successful result.
- Group Ticketing inventory endpoints return the combined Travelian group JSON only; do not add a public per-vendor success/failure summary. Keep supplier failures in logs and return successful vendors' group records when partial data is available.
- Keep Group Ticketing's unfiltered inventory route separate from its filtered search route. Search filters combined inventory (one-way and return tickets) by any of outbound origin, outbound destination and departure date (at least one), plus adult count, sorted by departure date.

## Supplier accuracy and credentials

- Use only supplier endpoints, request fields, and response formats documented by the user or verified from an authoritative supplier source.
- Do not invent endpoints, payload fields, response data, credentials, or supplier behavior. If a contract is incomplete or a documented endpoint fails, report the exact uncertainty and ask for authoritative clarification instead of guessing.
- Keep credentials in environment variables or a secret manager. The local `.env` is ignored by git. Never print, log, commit, or return credentials or bearer tokens.
- Do not create simulated supplier offers or bookings in the live integration path. Mock data is allowed only in clearly identified tests or the explicitly named demo adapter.
- Booking creation changes supplier state. Do not automatically retry it; a timeout may be indeterminate and must be reconciled with the supplier before another attempt.

## Dhillu & Sahi integration

- Dhillu & Sahi is a **Group Ticketing vendor**. Keep its group inventory, availability, booking, and booking-details operations under `app/vendors/group_ticketing/dhillu_sahi/` and the Group Ticketing API domain; do not register it as a Live Ticketing or Hotels supplier unless the user provides a separate contract for those products.
- Base URL: `https://api.dhilluandsahi.com`.
- Authentication contract supplied by the user: `POST /api/login`, JSON body with `email` and `password`, token at `success.token`.
- Group inventory contract supplied by the user: `POST /api/pnr-respect-to-mysector-user`, Bearer token, JSON body optionally containing `my_sector_id`.
- Live behavior: omitting `my_sector_id` returned HTTP 404; using the documented example `9` previously returned inventory. `my_sector_id` is a sector group id; the gateway reads the ids only from `DHILLU_SAHI_SECTOR_GROUP_IDS` (no default in code). Do not add or assume ids in code, tests or docs.
- Availability contract supplied by the user: `POST /api/check-availbility`, Bearer token, JSON body with `pnr`, `adult`, `child`, and `price`.
- Booking contract supplied by the user: `POST /api/add-booking`, Bearer token, passenger arrays and booking fields as documented in the API implementation and README. Do not retry automatically. Confirmed by Dhillu & Sahi on 2026-11-14: add-booking only holds seats (unpaid), the supplier expires an unpaid booking after 1 hour, and there is no cancel call. It is wired as the Group Ticketing hold (docs/api/group-holds.md), behind GROUP_DHILLU_SAHI_HOLD_ENABLED.
- Booking-details contract supplied by the user: `POST /api/agent-booking-detail`, Bearer token, JSON body with `booking_id`.
- Verified live integration result on 2026-10-16: login succeeded using the locally configured credentials. The actual gateway inventory route succeeded after the mapper was adjusted to accept numeric meal values (5 groups, 21 tickets). Availability for the documented sample PNR returned `false`, and booking details for the documented sample booking ID were returned successfully. Do not guess or silently substitute supplier paths or request fields.
- Test integrations through the actual versioned Group Ticketing API routes. Booking creation changes supplier state and must not be invoked without a concrete user request and booking payload.
