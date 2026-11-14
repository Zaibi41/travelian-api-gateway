# Group Ticketing holds (admin)

Back office only (super_admin and staff). Adults only, 1 to 9. The price is not known before the hold.

| Endpoint | What it does |
| --- | --- |
| `POST /v1/group-ticketing/holds` | Hold seats on a group fare (`ticket_id` from search, `adults`, customer card, one adult per seat). Never retried. Answers with the booking reference (`TRV-GRP-…`), PNR, hold expiry, price per adult and total. |
| `GET /v1/group-ticketing/holds/{booking_id}` | The hold: status (`held`, `expired`, `released`, `processing`, `issued`, `unknown`), countdown time, price, payments, masked travelers. A hold past its time limit is marked `expired` (logged) and is never held again. |
| `POST /v1/group-ticketing/holds/{booking_id}/price-check` | Current supplier price (read-only): held total, new total, difference. |
| `POST /v1/group-ticketing/holds/{booking_id}/release` | Release at the supplier, then mark released (logged). |
| `POST /v1/group-ticketing/holds/{booking_id}/issue` | Needs full payment and the issue tickets permission. Checks the price again: if it moved, `409 price_changed` with `held_total`, `new_total`, `difference`, until the call repeats with `accepted_total` equal to the new total (the booking total changes, logged; any difference must be paid before issuing). One issue call, never retried; an unclear outcome is `unknown`. |

Payments use the existing `POST /v1/admin/bookings/{booking_id}/payments`; it refuses a group hold past its time limit.
Audit (`booking_events`): `hold_created`, `hold_expired`, `hold_released`, `payment_recorded`, `price_change_accepted`, `issuance_started`, `issuance_issued` / `issuance_failed` / `issuance_unknown`.
Staff see `stock` (`supplier` or `owned`), never `supplier`, unless granted "see supplier names". Supplier cost is never returned here.
Passport numbers and dates of birth are stored as in the Flights holds (plain text for the sandbox phase, masked on read; docs/pii-storage.md).

## Sources (`app/services/group_holds.py`)

Each group source plugs in as a `GroupHoldProvider` (`hold`, `price`, `release`, `issue`) in `PROVIDERS`. The registry is **empty**: `POST /holds` answers `409 hold_not_supported` before contacting any supplier, and search results carry `can_hold: false`.

| Source | What it supports (14 Nov 2026) |
| --- | --- |
| Dhillu & Sahi | Inventory, read-only availability check (`/api/check-availbility`, true/false), add-booking (behind booking authorization), booking details. No hold expiry, release, price re-check or issue call. |
| Skypass (`api.skypass.pk`, production) | Live-checked 14 Nov 2026 with 1 seat, released straight after. `hold-available-seats` (form: `ticket_id`, `quantity`, `open_seats_slot`) answered `{"status":"success","bal_seats":"1","message":"1 Ticket(s) on hold"}`: no hold id, PNR, expiry or price. `release-seats` (`ticket_id`, `open_seats_slot`) answered `{"status":"success","message":"Ticket on hold free now."}`. The price is the ticket detail's `open_adult_price` (unchanged by the hold); the PNR is the ticket's `airline_pnr`. Every ticket has `on_hold_rule: 2` (meaning not documented). **Missing before an adapter:** how long a hold lasts, and whether `submit-booking` issues tickets or creates a supplier booking on hold (`cancel-booking` cancels "a booking if it is on hold"). `submit-booking` was not called. |
| Al Haider Travel | Postman collection only (create, show, cancel and confirm booking; `fares`, `auto_cancel_after`, numeric `status` codes not explained). No adapter and no credentials in the gateway. |
| Travelian-owned stock | `stock: "owned"` is reserved for source `travelian`; no owned inventory exists yet. |
