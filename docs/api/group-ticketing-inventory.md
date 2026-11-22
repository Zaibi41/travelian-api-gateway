# Combined Group Ticketing inventory

## Call

```http
GET /v1/group-ticketing/inventory
Accept: application/json
```

No body or search filters are required. This is the application's existing versioned route. The public hostname/path depends on separately approved production routing; this document does not expose `/group-ticketing` or change `/v2`.

## Response contract

HTTP 200 returns one object with a `data` array. Every element is a normalized group from a successful configured supplier. The example below is synthetic test data, not a supplier offer or live PNR:

```json
{
  "data": [
    {
      "id": "0123456789abcdef0123456789abcdef",
      "sector": "LHE - RUH",
      "airline": "Synthetic Airline",
      "airline_logo": null,
      "tickets": [
        {
          "id": "fedcba9876543210fedcba9876543210",
          "updated_at": "2026-05-08T05:39:41.000000Z",
          "sold": "2",
          "pnr": "MOCK-001",
          "departure_date": "2026-12-01",
          "sector": "LHE - RUH",
          "airline": "Synthetic Airline",
          "airline_logo": null,
          "price": "100",
          "currency": null,
          "remaining": 3,
          "baggage": "20 KG",
          "meal": 5,
          "ticket_type": "One Way",
          "subparts": []
        }
      ]
    }
  ]
}
```

Use `GET /v1/group-ticketing/tickets/{ticket_id}` to retrieve one current inventory ticket. See the [Dhillu & Sahi workflow](dhillu-sahi-workflow.md) for selection, count derivation and protected booking mapping.

Group fields: `id`, `sector`, `airline`, `airline_logo`, `tickets`. IDs are opaque gateway IDs scoped by source vendor, so equal source IDs from different vendors do not collide. They are not supplier booking IDs or proof of ownership. For Dhillu & Sahi rows without a source ID, the current mapper uses the row position internally, so ID stability across supplier reordering is not guaranteed.

Ticket fields:

| Field | Meaning / current behavior |
| --- | --- |
| `id` | Opaque gateway ticket-selection ID; see [workflow](dhillu-sahi-workflow.md) for refresh/ambiguity behavior |
| `updated_at`, `sold` | Original supplier timestamp and sold count when mapped; otherwise null |
| `pnr` | Original supplier identifier, including punctuation; no truncation or normalization |
| `departure_date`, `sector` | Supplier-mapped departure date and route; can be null |
| `airline`, `airline_logo` | Supplier-mapped airline presentation fields; can be null |
| `price` | Supplier-mapped value; may be string, number or null; not a guaranteed locked quote |
| `currency` | Null when not supplied by the current adapter; never infer a currency |
| `remaining` | Integer when supplied; null means unknown, not zero |
| `baggage`, `meal`, `ticket_type` | Mapped supplier values; strings/numbers/null are supported |
| `subparts` | Supplier leg-detail dictionaries; content can differ by supplier and is not a fixed cross-vendor leg schema |

Root/group/ticket wrappers contain no added vendor names or per-vendor success/failure summary. Leg dictionaries retain adapter-mapped detail. This is a normalized subset of supported fields, not a promise to forward every raw supplier field. Similar records from different suppliers are retained, not silently merged or deduplicated.

## Aggregation and failures

- Every configured adapter is called concurrently, within the gateway's fan-out concurrency and timeout settings.
- Unfiltered inventory retains all returned group/ticket types; it does not apply origin/date/passenger filters. Filtered search remains the separate `POST /v1/group-ticketing/search` operation. Search takes any one of `origin`, `destination` and `departure_date` (the others may be left out, null or empty; none at all is a 422), plus `adults`; it matches a return ticket by its outbound sector and date, and answers in departure date order.

Return tickets: every ticket's `legs` lists its flights, outbound then return (`direction`). Skypass says which is which (`sector_type` Out / In; read live 15 Nov 2026, "Two Way" tickets). Dhillu & Sahi and Al Haider listed no return ticket live (Dhillu & Sahi sector ids 1 to 60 checked: all outbound from Pakistan, ticket_type 1, one subpart); when one of their tickets has more flights and one leaves from the ticket's destination, that flight and the ones after it are the return (`app/vendors/group_ticketing/legs.py`). An Al Haider round-trip sector written out ("LHE-DMM-LHE") lists as its outbound sector. A hold keeps every flight on the booking (Itinerary tab; `flight.legs` on the hold).
- One failed, timed-out or invalid supplier response does not hide successful suppliers' inventory.
- A successful empty supplier response is usable: if the successful suppliers have no rows, return HTTP 200 with `{"data":[]}`, even when another supplier fails.
- No configured supplier, or failure of every configured supplier, produces HTTP 503 with `{"detail":"Group inventory is temporarily unavailable"}`.
- Failures are recorded in logs. Consumers cannot infer complete supplier coverage from a 200 response because the public response intentionally contains no vendor outcome summary.
- Reading inventory neither reserves seats nor creates a booking. Validate current availability separately before any subsequently authorized booking flow.

## Current vendor coverage

The gateway registry currently implements **two inventory adapters**:

| Adapter | Required configuration, values never returned by this endpoint |
| --- | --- |
| Dhillu & Sahi | Account email, password and confirmed group sector selector |
| Skypass | Supplier base endpoint, client ID and client secret |

Other supplier code in the portal repository is not automatically registered in this gateway. Local mocked tests verify both adapters; they do not establish current production configuration or live connectivity.

"All inventory" means all rows returned by the supported supplier operation for the configured account/selector. Dhillu & Sahi requires a confirmed sector selector; do not assume its example selector represents every account sector. Additional sectors, pagination or suppliers require authoritative contracts before extending coverage. No undocumented fetching or invented data is used to imply completeness.

To add another supplier: obtain its authoritative inventory/authentication contract, implement an isolated adapter under `app/vendors/group_ticketing/<supplier>/`, map to `GroupInventory`/`GroupTicket`, register it in `get_group_inventory_vendors()`, and test through the versioned route with mocked HTTP. The portal continues calling the same endpoint and receiving the same public schema. Adapter registration is currently explicit code, not automatic discovery.

## Verification

`tests/group_ticketing/test_inventory.py` covers real route → registry → adapter → mocked HTTP → public response: both-vendor aggregation, concurrent dispatch, mixed routes/types, original PNRs, equal source IDs, numeric meal values, unknown currency/capacity, leg details, each vendor's HTTP failure/timeout/malformed response, successful empty inventory, total outage and configuration gating. No live supplier endpoint is contacted.

This response-focused increment does not change booking guards, UI, SQL, supplier credentials, production infrastructure, static outbound IP `52.76.26.224`, or public routing.

## UUID representation

Generated public group/ticket IDs now use canonical hyphenated UUIDv5 strings instead of 32-character UUID hex. Their underlying UUID values and deterministic identity inputs are unchanged. Single-ticket lookup accepts both previous unhyphenated IDs and canonical UUIDs and returns canonical IDs. Clients must treat IDs as opaque strings and refresh cached inventory if they compare literal ID strings. Supplier identifiers and PNRs are unchanged. No data migration is required.

## Al Haider

`app/vendors/group_ticketing/al_haider/`, from `docs/postman/Al Haider Travel.postman_collection.json`. A permanent access token (`AL_HAIDER_ACCESS_TOKEN`, sent as `Authorization: Bearer`; no login, no email or password) and `AL_HAIDER_BASE_URL`. Only `GET /api/available/groups` (all query values empty) is called; booking calls are not built and Hold seats is off ("Holding is not wired for Al Haider yet" for viewers with supplier access, the general reason otherwise).

Fields used (live response read 14 Nov 2026): `pnr` (supplier PNR); `id` (supplier group id); first `details[].id` by `sr` (supplier ticket id); `airline.airline_name`, `airline.logo_url` (served through the gateway); `sector` "ISB-MCT-JED" (airport codes, shown "ISB - MCT - JED"; each flight's codes come from it, since `details[].origin`/`destination` are city names); `dept_date`; `details[]` in `sr` order: `flight_no` "9P-710" -> 9P710, `flight_date`, `dept_time`, `arv_time` (there is no arrival date: a flight lands on its `flight_date`, or the next day when `arv_time` is earlier than `dept_time`), `baggage` "20+7" = 20 kg checked + 7 kg hand carry (same format as Skypass; the group-level `baggage` is always null); `available_no_of_pax` (seats left); `meal` Yes/No; `type` ("KSA ONEWAY"); `price` (whole rupees, PKR). The price is read as per passenger: the API has no total or per-type price fields and its values (Rs 69,000 to 105,000 for one-way KSA and Muscat flights) match the other suppliers' per-adult prices on the same routes. Al Haider has not confirmed it in writing.

