# Skypass integration coverage and contract blocker

Reviewed 2026-10-23 against the user-supplied **APIs Integration Manual — Skypass Customer Interface**, gateway source and sibling portal proxy source. No credentials, real PNRs or passenger data were inspected or included. No supplier request was made.

## Current gateway coverage

| Capability | Current FastAPI behavior |
| --- | --- |
| Supplier authentication | Existing adapter sends configured key/secret to `/auth` and reads `access_token` |
| Inventory | Existing adapter fetches `/tickets`, validates and normalizes ticket/sector fields |
| Combined inventory and search | Includes configured Skypass alongside successful other vendor results |
| Ticket selection/details | Gateway `/v1/group-ticketing/tickets/{ticket_id}` selects a normalized record from refreshed inventory; it is not a call to Skypass's supplier detail operation |
| Supplier seat hold/release or availability/status | Not implemented in this gateway; no Dhillu & Sahi request format may be reused as a Skypass contract |
| Booking/list/details/amend/cancel | Not implemented for Skypass in this gateway; existing generic booking handlers are Dhillu & Sahi-specific and locally fail-closed |
| Supplier ledger | Not implemented in this gateway |

Current inventory behavior was established before this manual. The new attachment is an integration guide, not a full endpoint contract; it does not independently verify its payloads or complete the missing operations.

## What the supplied manual establishes

- Skypass issues API credentials; secure credential/token handling is required.
- Endpoint documentation is delegated to [the linked Postman documentation](https://documenter.getpostman.com/view/29502061/2sA3JRXe6C).
- API-created bookings are associated with the travel agency, and the guide describes tickets being issued to that agent. Do not treat creation as a harmless read or assume booking and issuance are separately controllable without clarification.
- Agency branding may appear on invoices when configured with the supplier.
- Supplier notifications go to the agency/agent. Travelian must separately implement authorized end-customer notification delivery; do not promise automatic supplier-to-customer messages.
- Supplier UI is not supplied for embedding; Travelian must implement its own presentation.

The guide does not establish API request fields, response schemas, idempotency, financial charge timing, hold expiry, or reconciliation behavior.

## Updated authoritative documentation — working link

The user supplied a replacement [official Postman page](https://documenter.getpostman.com/view/29502061/2sBYAsvrLw). A read-only HTML download succeeded, and the page referenced a public collection JSON that was also retrieved successfully on 2026-10-23. No supplier APIs were called.

The collection confirms the existing auth form fields and the ticket/detail, seat hold/release, booking create/list/details/update/cancel, and inventory-status requests. It also includes separate Umrah/package operations outside this Group Ticketing increment. **Seat availability is a state-changing hold and must precede booking**, according to its description. Do not reuse Dhillu & Sahi's read-only availability semantics. Booking requires `agent_details[id]`, `agent_details[name]` and `agent_details[email]` for the current portal user; derive these from trusted identity, not arbitrary caller input.

Request formats include nested multipart passenger fields. The collection has **zero saved response examples** across its operations, so response schemas, success/failure identifiers, holds/expiry and reconciliation still require verification. The ledger URL is malformed in the supplied collection; do not silently substitute a guessed path. Update-body examples and description also need alignment before implementation.

## Original documentation retrieval result

The browser could not open the linked Postman page. A direct read-only HTTPS download of that exact public URL returned **HTTP 404**. This is a documentation-access failure, not a supplier connectivity/authentication failure and not evidence that the supplier operations do not exist. No alternative supplier URL or payload was guessed.

## Existing portal proxy: reuse candidates, not verified contracts

The sibling portal's `aws/skypass-proxy/index.mjs` and its README contain the following candidate mappings. These are historical application code, not supplier-authoritative proof. The updated collection confirms most request mappings below; response/error contracts and noted ambiguities remain unresolved.

| Candidate operation | Path referenced by existing portal code | Gateway status |
| --- | --- | --- |
| Filters | GET `/airlines-sectors` | Needs authoritative contract |
| Supplier ticket details | GET `/ticket/{ticket_id}` | Needs authoritative contract |
| Hold seats | POST `/hold-available-seats` | Needs state-change and expiry contract |
| Release seats | POST `/release-seats` | Needs state-change contract |
| Submit booking | POST `/submit-booking` | Needs full passenger/financial/idempotency contract |
| List bookings | GET `/bookings` | Needs query/response/ownership contract |
| Booking details | GET `/booking/{booking_id}` | Needs response/ownership contract |
| Cancellation | POST `/cancel-booking` | Needs charge/refund/status contract |
| Amendment | POST `/update-booking/{booking_id}` | Needs full update/financial contract |
| Ledger | GET `/ledger` | Needs query/response and finance authorization contract |
| Ticket status | POST `/get-tickets-status` | Needs request/response and semantics contract |

The old proxy refreshes tokens and retries an authenticated request after a 401. This behavior must not be carried over indiscriminately to creation, hold, release, amendments or cancellation. Retries for state changes require explicit supplier guarantees and gateway reconciliation safeguards.

## Required next input

The updated link resolves the missing request-documentation access. Next obtain response examples/schemas and clarify the incomplete contracts below, with credential values removed:

1. Production/test base URLs and auth encoding/token lifetime.
2. Ticket list pagination/filtering and ticket detail/status response schemas.
3. Hold/release fields, quantity rules, slot identifier meaning, hold ownership and expiry.
4. Complete booking/passenger fields, success identifiers and rejection formats; whether creation issues tickets or incurs an obligation.
5. Details/list/update/cancel/ledger requests, responses and account scope.
6. Idempotency support and how to find an existing booking when creation returned no identifier.

After the contract is available, implement isolated clients under `app/vendors/group_ticketing/skypass/`, normalize at that boundary, retain internal source references for supplier-specific selection, and register capability-aware gateway operations. Booking/financial/passenger-data access remains protected until trusted caller and ownership controls are implemented. Do not route a Skypass selection to Dhillu & Sahi because the current booking handler targets only that supplier.

Mocked verification must cover success, explicit rejection, invalid JSON/schema, auth failure, pagination, supplier-specific ticket selection, seat expiry/concurrency where documented, unknown creation outcomes, duplicate attempts and preservation of other vendors' successful inventory. Every state-changing request needs an asserted send count; no live booking or ticket issuance is authorized by this review.

Status: **updated official request collection retrieved; existing inventory retained; full lifecycle implementation still requires response contracts and clarification of malformed/ambiguous requests**. No runtime code, production configuration, static outbound IP, UI, SQL, public routing or supplier state was changed.
