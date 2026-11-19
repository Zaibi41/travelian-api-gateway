# Website flight payments (gateway): card with automatic issuance, bank transfer, JazzCash; red alerts; S3 upload fix

Companion website PR: travelian `feat/flight-payments` (linked on GitHub). Go-live steps: `docs/FLIGHT_PAYMENTS_GOLIVE.md`. Draft: not ready for review yet.

## Changes (d927857, 5b0cbbb, b02a499, 9cf8a69)
- `PUBLIC_FLIGHT_BOOKING_ENABLED` (default off): website flight fares are held at the supplier and paid by card (PayFast),
  bank transfer, JazzCash or Book Only; restricted airlines stay requests. `POST /v1/public/flights/payment-options`.
- Card: issuance starts only from a PayFast result verified with the secured key, for the asked amount, recorded once,
  that fully pays the booking (DB row locks: one payment, one issuance claim). Southair issues automatically when
  `SOUTHAIR_TICKETING_ENABLED`; never marked issued without the supplier's ticket numbers.
- Not issued automatically (ticketing off, SooperFare, refusal, unclear, paid after the hold): hold read at the supplier,
  one re-hold of the same fare if lost, red alert to admins (admin panel + SES email, skips logged), second alert at 30
  minutes left; hold lost -> `paid_not_ticketed` (migration 0025).
- Recovery job: card-paid flights not issued or stuck mid-issue for 10 minutes get one retry (stuck ones checked with the
  supplier first).
- Admin: derived status "Paid, issuance pending", filterable; "Paid, not ticketed" card; urgent notifications; supplier
  names only for allowed viewers.
- Bank transfer / JazzCash: held, staff alerted, staff record and issue. Never automatic.
- Fixes: PayFast notifications posted as form data were ignored (python-multipart not installed); S3 uploads were signed
  SigV2 on the global endpoint (refused for new buckets), now SigV4 regional; QR image can be removed on edit.
- Not built: SooperFare ticket issuing (`/v1/booking/confirm` has no documented response).

## Tests
- Gateway: 1277 passed; 15 known fixture failures unchanged (docs/TESTING.md). New: forged/unverified PayFast never pays or
  issues, concurrent notify + return (one payment, one ticketing call), recovery, re-hold, red alerts, uploads.

## Before merge / deploy
Migration 0025 after the gateway deploy; flags off until PayFast production, a funded Southair account and SES are ready.


🤖 Generated with [Claude Code](https://claude.com/claude-code)
