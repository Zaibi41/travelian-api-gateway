# Public website booking (guests)

No login. The gateway decides prices and what a guest sees; the page only displays it.

## Switch

| `PUBLIC_GROUP_BOOKING_ENABLED` | What a guest's booking does |
|---|---|
| `false` (default) | Saved as a **request** (`booking_status = 'requested'`, `channel = 'website'`): same checks and record as a hold, **no supplier call, no payment**. Staff see it in the admin Bookings list and follow up. |
| `true` | Seats are **held at the supplier** (the same hold staff use, actor "website"), then the guest can pay with PayFast. |

## Routes (`/v1/public`)

| Route | Purpose |
|---|---|
| `POST /group-ticketing/search` | Group fares, selling price only. Cached `PUBLIC_SEARCH_CACHE_SECONDS`; 30 searches per IP per minute. Never returns supplier names, cost, PNR, the Owned/Supplier label or hold flags. |
| `POST /group-ticketing/bookings` | Book as a guest. 5 per IP per hour. Answers with the reference and a private `access_token` (shown once; only its SHA-256 is stored). The guest can't set a price. |
| `GET /bookings/{reference}` | Header `X-Booking-Token`. Status, total, paid, hold time, flights, traveler names; final PNR and ticket numbers once issued. |
| `POST /bookings/{reference}/payments/payfast` | Header `X-Booking-Token`. The PayFast checkout form (`action`, `fields`) for the amount due; the browser posts it. Only for a live hold that can take payment. |
| `PUT /bookings/{reference}/contact` | Header `X-Booking-Token`, or `contact` (the email or phone booked with, My Bookings). Change the mobile number (`phone_code`, `phone`) and email before paying (travelian#56). No re-hold: the airline hold and price stay. Refused (409 `closed`) once paid, issued, cancelled or expired. Logged on the booking, masked (`customer_contact_changed`). Staff: `PUT /v1/admin/bookings/{id}/customer-contact`, at any time. Names, dates of birth and passports are not changed here (they need supplier cancel + re-hold, gateway#44 / #48). |
| `GET/POST /payments/payfast/notify` | PayFast's server notification (`CHECKOUT_URL`). Not in the API docs. |
| `POST /payments/payfast/return` | The website forwards the parameters PayFast sent the customer back with. |

A guest's booking is never refused as a double booking, and the answer never depends on other bookings. When a traveler
already has an active booking on the same route and date, the booking goes ahead and staff are flagged ("Possible double
booking": docs/api/group-holds.md, "No double booking").

## PayFast (sandbox `https://ipguat.apps.net.pk`)

Contract: the PayFast Postman collection (frontend `docs/postman/PayFast.postman_collection.json`, not committed) and
the Merchant Integration Guide v2.3 as cited by the `paygate_pk` library. The official docs page refused automated
reads, so confirm against PayFast's guide before production.

1. Token: `POST /Ecommerce/api/Transaction/GetAccessToken` (form `MERCHANT_ID`, `SECURED_KEY`, `BASKET_ID`, `TXNAMT`, `CURRENCY_CODE`) → `ACCESS_TOKEN`. Verified live from the Lightsail server on 18 November 2026.
2. Checkout: the browser posts the form to `/Ecommerce/api/Transaction/PostTransaction`. `ORDER_DATE` is `YYYY-MM-DD`; `SIGNATURE` and `VERSION` are free strings; `CHECKOUT_URL` is our notification URL. The secured key is never in the form.
3. Result: `basket_id`, `err_code` (`000` = paid), `transaction_id`, `transaction_amount`, `validation_hash` = SHA-256 hex of `basket_id|secured_key|merchant_id|err_code`.

A payment is recorded (method `payfast`, received into "PayFast", no receiving account) only when the hash verifies
**and** the amount equals what the gateway asked for, once per basket (later copies are ignored). A wrong amount is
logged as `online_payment_needs_review` and not recorded. A payment that arrives after the hold ended is recorded and
flagged `paid_after_hold_ended` for staff. Payment never issues tickets: staff issue from the admin as today.

## Not yet

- Emails to the guest and to staff (needs Amazon SES and a verified sender).
- Flights and hotels on these routes (next).
- Bot protection beyond per-IP limits (for example Cloudflare Turnstile).
- Rate limits and the search cache live in the one gateway process; move them to shared storage before running more workers.
