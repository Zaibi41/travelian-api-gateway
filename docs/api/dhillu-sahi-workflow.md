# Dhillu & Sahi: documented workflow and remaining gates

Source: user-supplied Dhillu & Sahi APIs Integration Manual, reviewed 2026-10-23. No credentials, real PNRs or passenger examples are reproduced here. Examples below are synthetic. Implementation uses the five supplied endpoints; no single-ticket, cancellation, refund, ticket-issuance or reconciliation supplier endpoint has been invented.

## Inventory and selection

Use `GET /v1/group-ticketing/inventory` or the separate filtered search route. Each returned ticket now includes additive fields:

- `id`: opaque selection identifier derived from supplier, original PNR, departure date, sector, ticket type and leg details. Supplier identity is not exposed in the public wrapper.
- `updated_at`: original supplier group-record timestamp, preserved as a string; null if unavailable.
- `sold`: original mapped string/integer sold count; null if unavailable.

Existing PNR, price, remaining seats, ticket type and leg fields remain unchanged. Skypass records do not acquire fabricated Dhillu & Sahi metadata; fields unsupported by their adapter stay null.

Select one record using the gateway-only route:

```http
GET /v1/group-ticketing/tickets/{ticket_id}
```

The response is a single public ticket object with the same fields as the inventory ticket. This refreshes combined inventory; it does not call an undocumented supplier detail endpoint or reserve a seat. Invalid identifier returns 422, no current match returns 404, ambiguous duplicate matches return 409, and inventory outage returns 503. When only part of supplier inventory is available, a 404 means no match in the currently returned inventory, not proof of permanent removal from the supplier.

Price, remaining seats, sold count and timestamp are excluded from ID calculation so those changes can refresh under the same ID. Changes to identity/leg fields change the ID. Identical identity records are deliberately ambiguous rather than arbitrarily selected. IDs are not booking IDs, permissions, guaranteed supplier-native identifiers, or a durable reservation. There is no new database/cache or production route configuration.

## Availability

Existing explicit counts remain supported:

```json
{"pnr":"MOCK-001","adult":1,"child":2,"price":100}
```

Here `child` is the combined children-plus-infants count, as documented by the supplier. Alternatively, the gateway can derive counts:

```json
{"pnr":"MOCK-001","passenger_types":["adult","child","infant"],"price":100}
```

This sends only documented supplier fields: `pnr`, `adult=1`, `child=2`, `price`. It never forwards `passenger_types`. Use exactly one input mode: mixed modes, missing explicit counts, or zero total passengers return 422 without calling the supplier. The zero-passenger rejection is an intentional validation tightening. Existing valid positive-count requests retain their mapping. Availability does not lock a fare or reserve seats.

## Booking mapping — protected, not enabled

The security dependency still returns 503 before booking creation or booking-details supplier access. The following mapping is verified only in mocked tests with explicit test-only guard overrides.

- Login captures `success.agent_id` when it is a positive integer, excluding booleans. Missing/invalid agent identity does not block inventory or availability, but blocks booking dispatch.
- Booking derives `agent` from that supplier-authenticated login. The old caller `agent` field is optional/deprecated for compatibility; a supplied conflicting value is rejected before creation. This binds the supplier account, not a Travelian caller/agency. Trusted portal identity and ownership remain unresolved.
- `updated_at` remains required and must parse as a timezone-aware ISO timestamp. The exact input string is forwarded, retaining fractional seconds and timezone spelling. Use the selected supplier record's timestamp, never a newly generated current time.
- Passenger arrays and documented booking field names remain intact. No unsupported price, idempotency, payment or ticketing fields are added to the supplier body.
- A successful creation response must include a positive integer `data.id`; booleans, zero, negative, missing or string identifiers are rejected. Rejection of malformed responses does not prove that no supplier booking was created: reconciliation remains required before another attempt.
- No automatic booking retries are introduced.

The direct booking request is not yet bound to a persisted selection/accepted quote, and timestamp provenance/freshness is not enforced by comparing the submitted value against inventory. The new ID/details route supports selection but does not replace that missing secure booking-attempt layer. Booking stays disabled until authorization, ownership, durable duplicate prevention and reconciliation are implemented.

## Manual clarifications still required

- `my_sector_id` is a sector group id. No call lists the ids. The gateway calls the listing once per id in `DHILLU_SAHI_SECTOR_GROUP_IDS` (no default; unset or empty means not configured) and merges the results (repeats dropped by supplier ticket id); one failing id doesn't hide the others.
- The manual's group inventory example is incomplete JSON. Tests use clearly synthetic complete structures reflecting the documented fields, not a claim of a captured live response.
- Supplier login agent identity does not document how multiple Travelian agencies share or own supplier accounts.
- Creation financial obligations, fare-change guarantees, idempotency, lost-response lookup and cancellation/refund/issuance contracts remain unconfirmed.
- Booking-details monetary fields are documented as PKR. Inventory currency is not assigned from that statement; unsupported currency remains null.
- Booking PDF URLs are returned as supplier data; the gateway does not automatically fetch them or treat a URL as ticket issuance proof.

No supplier booking, production mutation, SQL integration, UI change or public routing change is authorized by this implementation. Preserve static outbound IP `52.76.26.224`.
