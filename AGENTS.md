# Agent guidance

## Project structure

- This repository is the Travelian API gateway.
- Keep Live Ticketing, Group Ticketing, and Hotels as separate API domains.
- Keep supplier integrations isolated under `app/vendors/<product>/<supplier_name>/` and normalize supplier responses at that boundary.
- Record each vendor's success, failure, or timeout separately. Do not let one vendor failure hide another vendor's successful result.
- Group Ticketing inventory endpoints return the combined Travelian group JSON only; do not add a public per-vendor success/failure summary. Keep supplier failures in logs and return successful vendors' group records when partial data is available.
- Keep Group Ticketing's unfiltered inventory route separate from its filtered search route. Search filters combined inventory by one-way origin, destination, departure date, and adult count.

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
- Live behavior: omitting `my_sector_id` returned HTTP 404; using the documented example `9` previously returned inventory. Do not assume `9` means account-wide inventory; set the confirmed account value with `DHILLU_SAHI_GROUP_SECTOR_ID` before fetching/searching.
- Availability contract supplied by the user: `POST /api/check-availbility`, Bearer token, JSON body with `pnr`, `adult`, `child`, and `price`.
- Booking contract supplied by the user: `POST /api/add-booking`, Bearer token, passenger arrays and booking fields as documented in the API implementation and README. Do not retry automatically.
- Booking-details contract supplied by the user: `POST /api/agent-booking-detail`, Bearer token, JSON body with `booking_id`.
- Verified live integration result on 2026-10-16: login succeeded using the locally configured credentials. The actual gateway inventory route succeeded after the mapper was adjusted to accept numeric meal values (5 groups, 21 tickets). Availability for the documented sample PNR returned `false`, and booking details for the documented sample booking ID were returned successfully. Do not guess or silently substitute supplier paths or request fields.
- Test integrations through the actual versioned Group Ticketing API routes. Booking creation changes supplier state and must not be invoked without a concrete user request and booking payload.
