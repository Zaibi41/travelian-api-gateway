# Hotels: RateHawk

Source contract: `travelian/docs/postman/Travelian — RateHawk Sandbox — Complete API.postman_collection.json`
(48 operations, RateHawk B2B API v3 + Content API v1). Code: `app/vendors/hotels/ratehawk/`.

## Settings

| Variable | Meaning |
| --- | --- |
| `RATEHAWK_ENDPOINT`, `RATEHAWK_KEY_ID`, `RATEHAWK_API_KEY` | HTTP Basic credentials. All three or hotel search answers "not configured". |
| `RATEHAWK_CURRENCY` / `_RESIDENCY` / `_LANGUAGE` | Search defaults: `USD`, `pk`, `en`. The sandbox rejects `PKR` (`invalid_params`, 17 Nov 2026). |
| `RATEHAWK_WRITE_ENABLED` | Off by default. Needed for the console's write operations. |
| `HOTEL_MARKUP_PERCENT` | Selling price = supplier price + percent, rounded up to a cent. Empty = 0. |

## Search API (Search > Hotels)

Roles: internal and agency roles; customers 403 (same as flight search).

- `POST /v1/hotels/destinations` `{query}` → cities/regions, then hotels (RateHawk multicomplete).
- `POST /v1/hotels/search` `{destination, destination_kind?, destination_id?, check_in, check_out, rooms:[{adults, children_ages}]}`
  - region → `search/serp/region/`; one hotel → `search/serp/hotels/`. Free text takes the first matching region.
  - Names, stars, city and address come from `content/v1/hotel_content_by_ids/` (100 per call), cached in memory for 24 hours.
  - Up to 300 hotels, cheapest first; each with all its rates, cheapest first.
  - Each rate: room type, board, nights, selling total and per night (minor units + currency), cancellation
    (`free_until` with the supplier time, `non_refundable`, `charges_apply`).
  - D23: `source` and `providers` only with "see supplier names"; `supplier_cost` only with "see cost and margin". Agency roles never.
  - D24: a failed content batch drops those hotels and adds `notice: "Some results may be missing."`.
  - Hotel and rate IDs are opaque, bound to the user, valid 15 minutes (`expires_at`). Supplier hashes stay in the gateway.

## Supplier console (Super Admin only)

- `GET /v1/admin/suppliers/ratehawk/operations` lists the 48 operations (key, group, supplier method and path, writes, download).
- `POST /v1/admin/suppliers/ratehawk/operations/{key}` `{payload}` sends `payload` as that operation's documented body
  (GET operations: as JSON in `?data=`) and returns `{operation, data}` with RateHawk's `data` unchanged. Download
  operations (voucher, invoices, closing documents, single act) return the file.
- Write operations (booking form/finish, cancel, order group create/disband/add/remove/pay overpay, profile
  create/edit/disable/restore/delete) answer 409 `writes_disabled` unless `RATEHAWK_WRITE_ENABLED`. They are never
  retried; a timeout answers 504 `outcome_unknown` (check booking status before trying again).

## Sandbox notes (17 Nov 2026)

- The key's overview lists every operation as active except `search/hotelsort/`, `serp/prebook/`,
  `general/contract/data/info/`, `general/financial/info/` and the order group create/disband/info/add/remove/pay
  routes; those may answer 403 for this key.
- Rate limits per minute: region and geo search 10, hotelpage 10, hotels search 150, content by IDs 30, multicomplete 30.
- Destination suggestions in the sandbox find Dubai; Makkah, Madinah, Istanbul, Jeddah and London return nothing.

## Not built yet

- Hotelpage, prebook and booking from Search (the Book, Hold and Quote actions): no approved booking flow screen.
  The operations exist in the console; the session keeps each rate's hotel and hashes for that step.
- Hotel Inventory (manual hotels) in the same list (D25): no gateway table yet.
- Selling in PKR: needs an exchange-rate source, or a production key that prices in PKR.
