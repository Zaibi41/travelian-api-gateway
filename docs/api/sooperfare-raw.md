# SooperFare raw console (Super Admin only)

Every SooperFare partner call, sent through the gateway exactly as SooperFare documents it, with SooperFare's answer
returned unchanged. Use it to check what SooperFare really sends (for example extras and seat prices for one airline)
without the gateway's normalising.

Why through the gateway: SooperFare only accepts calls from the gateway's whitelisted IP (52.76.26.224), and the
gateway holds the SooperFare login. The SooperFare token is never returned.

Postman: `docs/postman/SooperFare Raw (via Travelian gateway).postman_collection.json`, built from SooperFare's own
"SooperFare Shareable" collection with SooperFare's auth headers and the login request removed.
Set `TRAVELIAN_TOKEN` to a Super Admin Cognito **access** token. `END_POINT` is
`https://api.travelian.pk/v1/admin/suppliers/sooperfare/raw`.

## Routes

- `GET /v1/admin/suppliers/sooperfare/raw/operations`: the 14 calls (key, method, SooperFare path, gateway path, writes).
- `{method} /v1/admin/suppliers/sooperfare/raw{SooperFare path}`: SooperFare's path, method, JSON body and query string,
  sent unchanged. Headers passed on: `X-SF-Client-Currency`, `X-SF-Client-Locale`.

| SooperFare call | Method and path | Changes a booking |
| --- | --- | --- |
| Airports | `GET /v1/airline/airports?query=` | no |
| Availability | `GET /v1/airline/availability` (JSON body) | no |
| Search | `POST /v1/airline/search` | no |
| Quote | `POST /v1/airline/quote` | no |
| Ancillary (get) | `POST /v1/airline/ancillary` | no |
| Ancillary (select, before reservation) | `PATCH /v1/airline/ancillary` | no |
| Fares On Request | `POST /v1/airline/fares` | no |
| Reservation | `POST /v1/airline/reservation` | yes: holds seats |
| Get Extras | `POST /v1/booking/extras` | no |
| Submit Extras | `PATCH /v1/booking/extras` | yes |
| List | `POST /v1/booking/list` | no |
| Confirm | `POST /v1/booking/confirm` | yes: issues tickets and charges Travelian's SooperFare balance |
| Cancel | `POST /v1/booking/cancel` | yes |
| Void | `POST /v1/booking/void` | yes |

## Answers

- SooperFare's body unchanged, with SooperFare's HTTP status. A SooperFare 401/403 comes back as 502 so it isn't
  mistaken for a Travelian sign-in problem. `X-Supplier-Status` always carries SooperFare's own status.
- 409 `writes_disabled`: a call that changes a booking while `SOOPERFARE_RAW_WRITE_ENABLED` is off (the default).
- 503 `not_configured`, 502 `unreachable`, 504 `timeout`. For a call that changes a booking, a timeout or lost
  connection is 504 `outcome_unknown`: check Booking List before trying again. Those calls are never retried.
- Reads renew the SooperFare login once on a 401.
- Logs record who called which operation and SooperFare's status, never the body (it can hold passenger details).
- Bookings made here are not saved in Travelian and don't appear in admin Bookings.
