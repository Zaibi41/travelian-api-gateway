# Note for SooperFare: booking requests sent by Travelian (18–20 Nov 2026)

Ready to send (gateway#51). No tokens, keys or passenger personal data; SooperFare can find each booking by its own order ref / booking id.

**From:** Travelian (partner account), API base URL `https://api.sooperfare.com`, calls from our server IP 52.76.26.224.
**Endpoint:** `POST /v1/booking/reservation` (hold), header `X-SF-Authorization` (session token). Our side answered each request below with HTTP 201 after your reservation response.

You told us some of our booking requests never reached you. We checked against your own `POST /v1/booking/list` for our account on 20 Nov 2026, and these reservations are there:

| Our request (UTC) | Channel | Travelian reference | SooperFare order ref | SooperFare booking id (uuid) | PNR | Airline | Created at SooperFare (UTC) |
|---|---|---|---|---|---|---|---|
| 19 Nov 2026 16:46:31 | Admin | TRV-WTVPEL | spa-kj2 | 02bf4cca-89ae-4e65-b7b3-7c773dc326ee | FLJTIW | PA | 16:46:29 |
| 19 Nov 2026 21:02:09 | Website | TRV-H8ZFHS | spa-ae7 | 9ec40482-6bf9-4b90-8320-ceb330368634 | GKY7WM | PF | 21:02:04 |
| 20 Nov 2026 11:30:30 | Website | — | spa-spo-l5h | 61df680e-2a10-4b8c-bbe2-cdf702c4c160 | FUGWBM | PA | 11:30:25 |

Your list shows 10 reservations under our account between 18 and 20 Nov 2026. All are unpaid; none was confirmed or issued by us.

**What we checked on our side (gateway#46):** the base URL has no double slash; every SooperFare error is now logged (without secrets) and shown to staff; holds are recorded only after your reservation response.

**What we'd like from you:** please confirm the bookings you said were missing (reference, time, route) so we can match them to this list. If any are not in the table above, send us the time and route and we'll trace that request.

Note for Travelian staff: our server logs keep times and status codes but not SooperFare references or PNRs, so matching is by time plus SooperFare's list (see gateway#46).
