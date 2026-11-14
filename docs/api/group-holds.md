# Group Ticketing holds (admin)

Back office only (super_admin and staff). Adults only, 1 to 9. The price comes from the provider's listing (per adult) plus Travelian's markup, and is checked again before issuing.

| Endpoint | What it does |
| --- | --- |
| `POST /v1/group-ticketing/holds` | Hold seats on a group fare (`ticket_id` from search, `adults`, customer card, one adult per seat). Never retried. Answers with the booking reference (`TRV-GRP-…`), Travelian's hold expiry, price per adult and total (and the supplier PNR, with supplier access). |
| `GET /v1/group-ticketing/holds/{booking_id}` | The hold: status (`held`, `expired`, `released`, `processing`, `issued`, `unknown`), Travelian's own time limit, price, payments, masked travelers. A hold past its time limit is marked `expired` (logged) and is never held again. `pnr`, `supplier` and `supplier_release_failed` only with the supplier names permission. |
| `POST /v1/group-ticketing/holds/{booking_id}/price-check` | Current supplier price (read-only): held total, new total, difference. |
| `POST /v1/group-ticketing/holds/{booking_id}/release` | Release at the supplier, then mark released (logged). If the supplier fails, the booking reads as expired and the expiry job retries. |
| `POST /v1/group-ticketing/holds/{booking_id}/issue` | Needs full payment and the issue tickets permission. Checks the price again: if it moved, `409 price_changed` with `held_total`, `new_total`, `difference`, until the call repeats with `accepted_total` equal to the new total (the booking total changes, logged; any difference must be paid before issuing). One issue call, never retried; an unclear outcome is `unknown`. |

Payments use the existing `POST /v1/admin/bookings/{booking_id}/payments`; it refuses a group hold past its time limit.
Audit (`booking_events`): `hold_created`, `hold_expired`, `hold_released`, `payment_recorded`, `price_change_accepted`, `issuance_started`, `issuance_issued` / `issuance_failed` / `issuance_unknown`.
Staff see `stock` (`supplier` or `owned`), never `supplier`, unless granted "see supplier names". Supplier cost is never returned here.

Group identity: each search result and each hold carry `group_source` (`provider`, `provider_name`, `supplier_pnr`, `supplier_group_id`, `supplier_ticket_id`) only for viewers with the supplier names permission (Super Admin; staff when granted). When a hold is created the listing row's identity is copied onto the booking (`supplier_response -> 'source'`; the hold's PNR when the supplier gives one, else the listing's). Dhillu & Sahi: the ticket's `pnr`, the first subpart's `group_id` and `id`. Skypass: `airline_pnr` and the ticket `id`; Skypass sends no group ID, and `open_seats_slot` (hold-only) is never returned. In Bookings, a group booking's `pnr` (the supplier's) is also left out, and not searchable, without that permission.
Passport numbers and dates of birth are stored as in the Flights holds (plain text for the sandbox phase, masked on read; docs/pii-storage.md).

## Hold time and the expiry job

Travelian's own hold time: `hold_expires_at` = hold creation + `GROUP_HOLD_MINUTES` (default 30), stored on the booking. No supplier expiry is used (suppliers hold for 60 minutes as standard; Skypass sends none). After it, payments and issuing are refused even if the supplier still holds the seats.

`app/services/group_hold_expiry.py` runs from the app lifespan every `GROUP_HOLD_JOB_INTERVAL_SECONDS` (30): held group bookings past their time limit become `expired` (`hold_expired`), then each booking whose seats still need releasing gets one supplier release call (`supplier_released`, or `supplier_release_failed` and another try next run, up to `GROUP_HOLD_RELEASE_ATTEMPTS`, 5). "Release hold" releases straight away; if the supplier fails, the booking reads as expired and the job keeps retrying. While a release has failed, viewers with supplier access get `supplier_release_failed: true` ("Supplier release failed, release manually").

## Selling price

`GROUP_TICKETING_MARKUP_PERCENT` and `GROUP_TICKETING_MARKUP_FIXED_MINOR`: unset (or empty) counts as 0, so every fare with a listing price shows that price per adult and can be held where the provider holds seats.

## Sources (`app/services/group_holds.py`)

Each group source plugs in as a `GroupHoldProvider` (`hold`, `price`, `release`, `issue`) in `PROVIDERS`. **Skypass** is registered for hold, release and price (`app/vendors/group_ticketing/skypass/holds.py`); it does not issue (`issue_not_supported`, "Issuing is not available for this provider yet"). Other sources answer `409 hold_not_supported` before contacting any supplier, and their search results carry `can_hold: false` with `hold_unavailable_reason` ("Dhillu & Sahi has no hold API" for viewers with the supplier names permission, "Holding isn't available for this provider" for everyone else, so the supplier is never named to them).

| Source | What it supports (14 Nov 2026) |
| --- | --- |
| Dhillu & Sahi | Search only. Listing: `price` is per adult (booking 2749: one adult, group price 45,000 = booking total 45,000); no currency field, PKR. `baggage` and `departure_handcarry` in kg; `meal` 0/1; one-way only (`ticket_type` 1). Also a read-only availability check, add-booking (behind booking authorization) and booking details. No hold, release or issue call: Hold seats is off. The Postman collection (re-checked 14 Nov 2026) has exactly five calls: `login`, `pnr-respect-to-mysector-user` (groups), `check-availbility` (read-only availability), `add-booking` (creates a real supplier booking) and `agent-booking-detail`. `add-booking` is not used as a hold: nothing documented cancels or releases it, so the 30-minute hold and auto-release can't be honoured. |
| Skypass (`api.skypass.pk`, production) | **Hold, release and price re-check are wired.** Listing: `adult_price` is the price per adult (child and infant prices are separate fields, often "Price on call"); no currency field, PKR from `GROUP_TICKETING_DEFAULT_CURRENCY`. Baggage comes as one string, `20+07` = 20 kg checked + 7 kg hand carry; `sector_type` Out/In gives outbound and return legs. Live-checked 14 Nov 2026 with 1 seat, released straight after. `hold-available-seats` (form: `ticket_id`, `quantity`, `open_seats_slot`) answered `{"status":"success","bal_seats":"1","message":"1 Ticket(s) on hold"}`: no hold id, PNR, expiry or price. `release-seats` (`ticket_id`, `open_seats_slot`) answered `{"status":"success","message":"Ticket on hold free now."}`. The price is the ticket detail's `open_adult_price` (unchanged by the hold); the PNR is the ticket's `airline_pnr`. Every ticket has `on_hold_rule: 2` (meaning not documented). Not built: `submit-booking` (whether it issues tickets or creates a supplier booking on hold is unconfirmed; `cancel-booking` cancels "a booking if it is on hold"); it is never called. |
| Al Haider Travel | Skipped. Postman collection only (login with email and password; groups, group detail, create, show, cancel and confirm booking; `fares`, `auto_cancel_after`, numeric `status` codes not explained). Missing: an adapter, and `AL_HAIDER_BASE_URL`, `AL_HAIDER_EMAIL`, `AL_HAIDER_PASSWORD`. |
| Travelian-owned stock | `stock: "owned"` is reserved for source `travelian`; no owned inventory exists yet. |
