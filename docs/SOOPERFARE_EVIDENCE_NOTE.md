# Note for SooperFare: reservations made by Travelian, 18-19 November 2026

**From:** Travelian (partner account `<our SooperFare partner login>`), API calls from our server's static IP **52.76.26.224** to
`https://api.sooperfare.com` (endpoint `POST /v1/airline/reservation`, then `POST /v1/booking/list`).

You told us you received no booking requests from us. Our server logs and **your own `POST /v1/booking/list` answer for our
account** (read on 19 Nov 2026, 15:40 UTC) show these reservations on your side:

| Your order ref | Your booking id (`uuid`) | PNR | Airline | Created (PKT) | Status in your booking list | Travelian ref |
|---|---|---|---|---|---|---|
| spa-nbh-vps | 83948930-3b71-4df4-99df-9a144a9dee3f | ZWHLZG | PA | 18 Nov 22:15 | active (unpaid) | test reservation |
| spa-zrc | 3801e825-ccc9-421e-a441-222d8740612a | VFTATB | PA | 19 Nov 01:07 | active (unpaid) | TRV-G6CMP6 |
| spa-bro-7p3 | a1350a73-57a6-4528-b492-daf1c57d3e82 | MAUFPE | PA | 19 Nov 01:08 | active (unpaid) | not saved by Travelian (see below) |
| spa-vvf | 192c9779-e61a-4e11-b2f4-32f93c907606 | IKVIMS | PA | 19 Nov 01:10 | active (unpaid) | TRV-KCO5VJ |
| spa-o7i-i7l | 09d08847-e9e1-4de9-b21d-aaac08b05709 | RNICJL | PA | 19 Nov 01:31 | active (unpaid) | TRV-AKD438 |
| spa-gq1-x2g | d80baa03-6e7c-40c4-83b0-df1440e26206 | GKY5TH | PF | 19 Nov 16:48 | expired (`is_cancelled` true) | TRV-INQJJ8 |
| spa-0rj | 49c85ade-bdfe-48a9-b8f1-2424635b702c | OFZOJG | PA | 19 Nov 21:23 | active after a cancel request (see below) | integration test |

Each reservation call returned `status: true` with your `uuid`, `order_ref_id`, the airline PNR and `reservation_status: active`,
and each one is listed by `booking/list` under our account. None was confirmed (`booking/confirm` has never been called by us),
so they are all **unpaid reservations**; if your team looks only at confirmed or paid bookings, they won't appear there.

**Questions:**
1. Please check these order refs under our partner account. If your portal shows a different account for our API login, tell us.
2. Cancelling an unpaid reservation: on 19 Nov 2026 16:23:50 UTC we called `POST /v1/booking/cancel` for spa-0rj
   (`booking_ref_id` 49c85ade-…, `txn_ref_id` TRAVELIAN-TEST-CANCEL, amount 24070, PKR). You answered "Operation Successfull" with
   transaction `txn-016` ("Cancel For Airline Booking.", status `unpaid`), but `booking/list` still shows the booking `active` with
   `is_cancelled` false 3.5 minutes later. How is an unpaid reservation cancelled immediately? Is `booking/cancel` processed by
   your team, or is `booking/void` the call for unpaid reservations? Does the `unpaid` cancel transaction create any charge?
3. Please cancel spa-bro-7p3 (MAUFPE): our system couldn't verify its total, so no customer booking exists for it.
4. `booking/confirm`: what does the answer look like (ticket numbers?), and what must `txn_ref_id` be (your collection shows "TEST")?
5. Group fares: is there an API to search SooperFare group inventory? Your shared Postman collection has no group endpoint.

Full requests and responses of the 19 Nov test (secrets and personal data removed): `docs/SOOPERFARE_API_RESPONSES.md`.
