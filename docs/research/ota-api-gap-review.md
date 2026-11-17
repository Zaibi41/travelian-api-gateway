# Travelian OTA API gap review

Reviewed 2026-10-25 against the supplied attachment, both linked product articles, authoritative identity/booking documentation, and the actual local repository. This is an incremental implementation roadmap, not a claim that all OTA APIs are complete. No external accounts or suppliers were exercised and no production state changed.

## Evidence and interpretation

[Travilo](https://travilo.io/blog/travel-business-ota-platform-features/) describes a combined travel platform spanning search, agency onboarding, ancillary products, payment tracking, booking operations and reporting. Its broader feature set is useful for discovering product gaps, not as a supplier request/response contract. Its document-upload feature does not override Travelian's decision that documents remain optional.

[Travelopro](https://www.travelopro.com/major-features-of-online-travel-agency.php) highlights supplier aggregation, agent administration, operational accounting, multi-channel sales and back-office visibility. These are product capability examples; its marketing statements are not proof of local legal requirements, performance guarantees or available supplier APIs.

[Amadeus's own booking documentation](https://github.com/amadeus4dev/developer-guides/blob/master/docs/resources/flights.md) separates offer search, price confirmation, order creation and order management. This supports keeping inventory discovery separate from committing bookings. It does not authorize using Amadeus endpoints/fields for Dhillu & Sahi or Skypass. Supplier-specific booking and ticket issuance requirements remain contract-dependent.

[AWS Cognito's account confirmation documentation](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_ConfirmSignUp.html) distinguishes signup verification from login and administrator invitations. Signup email confirmation cannot be implemented by reusing the password-reset endpoint.

[PCI SSC](https://www.pcisecuritystandards.org/faqs/1318/) prohibits retaining sensitive authentication data after authorization. Payment integration should use a chosen provider's hosted/tokenized flows and verified webhook contract; this review does not certify PCI compliance.

The attachment also mentions licensing/accreditation. Those statements are not verified legal advice. Applicable jurisdiction, business model, ticketing authority/consolidator and supplier agreements require authoritative confirmation; they are not requirements we can fulfill by adding HTTP endpoints.

## Actual repository status

| Capability | Current implementation | Gap / required evidence |
|---|---|---|
| Human identity | Cognito token verification, login, refresh, logout, reset and current profile | Signup/confirmation/resend, challenge completion and immediate offline token-revocation policy remain incomplete |
| Roles | Local customer/agency_owner/super_admin, operator bootstrap, active flag | Full Cognito registration synchronization/backfill; no assumptions based on email/group |
| Agency onboarding | Contacts, UUIDs, assignment, pending/review/approval, suspend/reactivate, history | Private optional documents if requested; notifications; administrative process |
| Agency machine integration | Client registry, scopes, revocation, protected search aliases | Real dedicated Cognito clients/scopes/domain, secure secret delivery/rotation and audit controls |
| Super-admin users | Added list/detail/roles/counts, role/status change and history in this increment | Operational hardening and signup coverage; only local profiles are counted |
| Group Ticketing | Combined inventory, filtered search, selected ticket and availability; isolated suppliers | Stable production-capacity controls; documented booking lifecycle for each supplier |
| Booking create/details | Guarded supplier mapping paths exist | Dispatch deliberately blocked; no complete persistent booking ownership, idempotent attempts or reconciliation |
| Live Ticketing | Flight search orchestration; named demo adapter | No production Live Ticketing supplier registration in `get_vendors`; Group Ticketing suppliers must not be misclassified |
| Hotels | Search request/response contract returns unavailable | No real hotel supplier adapter; not a working hotel booking engine |
| Quotes | Group quote acknowledgement with generated request ID | No durable request workflow/fulfillment; acknowledgement must not be sold as confirmed travel |
| Payments/wallets/credit | Not implemented | Provider agreement, tokenization/webhooks, currency/rounding/ledger/reservation rules |
| Ticket issuance/void/reissue/refund | Not implemented as end-to-end gateway workflows | Supplier-specific documented operations, financial reversals, approval and reconciliation rules |
| Pricing/commission | Supplier prices normalized; no agency pricing engine | Agreed margin/commission source, rounding/currency rules; user deferred this work |
| Notifications/support | No delivery/ticket workflow | Chosen provider and templates, consent/contact policy and durable delivery state |
| Operations | Health/readiness, deployment scripts, logging and vendor outcome isolation | Shared rate limits, capacity/load evidence, production DB permissions/pooling, alarms and recovery verification |

All current APIs can be enumerated from `/openapi.json` without invoking suppliers. Presence in OpenAPI is not proof of a production-ready integration.

## Implemented next: super-admin user management

Human requests require a verified Cognito token **and** active local `super_admin` role. No token group grants local privileges. New APIs:

| Method | Endpoint | Behavior |
|---|---|---|
| GET | `/v1/admin/users` | Paginated local profiles; role_id/active filters |
| GET | `/v1/admin/users/{user_id}` | User identity mapping, role, activity, agency, revision |
| GET | `/v1/admin/roles` | Role lookup table |
| GET | `/v1/admin/dashboard/overview` | Local users/agency/application counts from one SQL snapshot |
| PATCH | `/v1/admin/users/{user_id}/role` | Explicit audited role change |
| PATCH | `/v1/admin/users/{user_id}/status` | Audited local activation/deactivation |
| GET | `/v1/admin/users/{user_id}/history` | Paginated API-driven admin changes |

New public audit IDs are canonical UUIDv4; external Cognito identifiers remain unchanged. Changes require a reason and expected revision, reject self-modification and reject assigning agency_owner without an approved agency relationship. Administrative role/status changes serialize and recheck the actor inside the write transaction; stale updates fail. Role/status modifications and audit inserts commit together. The last active super administrator must be retained. These protections apply to this API, not arbitrary direct SQL by database owners.

Local deactivation blocks flows that consult local user state (current profile, agency/admin/booking authorization); it does not disable Cognito login itself, delete tokens or make existing public inventory require login. Machine-client access is independent and requires separate registry revocation. Admin privileges do not bypass booking guards or supplier contracts.

Dashboard numbers are explicitly labelled `local_profiles`; they are not all Cognito registrations or sales totals. No invented bookings, revenue, balances or supplier success values are returned. Bootstrap/approval role events remain in `user_role_events`; user admin history contains API-driven changes from `user_admin_events`, not fabricated historical events.

## Missing API backlog, in dependency order

The following names are **proposed**, not implemented endpoints or promised supplier operations.

| Phase | Proposed API family | Prerequisites / acceptance criteria |
|---|---|---|
| 1: identity completion | `/auth/signup`, `/auth/confirm-signup`, `/auth/resend-confirmation`, challenge response | Verified Cognito public API mapping, protected credential handling, mocked tests, shared abuse controls; users default to customer |
| 2: booking foundations | `/group-ticketing/booking-attempts`, own/admin booking lists/details | Durable ownership, idempotency keys scoped to principal, payload hashing, concurrency guards, immutable pricing snapshots, expiry and supplier reconciliation; unknown outcome never retried blindly |
| 3: supplier booking | Existing guarded booking routes | Authoritative supplier payload/response and booking-ID evidence, financial obligation rules, sandbox/controlled approval, no silent retries |
| 4: money | `/payments`, `/payments/{id}`, provider webhooks, `/refund-requests` | Chosen provider/currency/settlement contract; authenticated replay-safe webhooks, ledger and reconciliation; no generic fake paid statuses |
| 5: servicing | Own/admin cancellation, void, refund, reissue requests and ticket retrieval | Verified supplier support and deadlines; request != fulfillment; audited decisions and financial reversal behavior |
| 6: partner API provisioning | Admin client issue/rotate/revoke workflow | Approved Cognito M2M settings, secure one-time delivery, audit, static outbound IP and supplier whitelist continuity |
| 7: operational support | Support requests, notification preferences/deliveries, export jobs | Defined ownership/PII retention, provider delivery contract, bounded exports and job storage |
| 8: expansion | Product-specific Hotels/Live Ticketing booking, ancillaries, packages | Separate adapters, agreements, actual inventory/prices, domain-specific rules; do not borrow Group Ticketing contracts |
| Later optional | Wallet/credit/commission/promotions, documents, white label, downline agents | Explicit business policy and audit/financial safeguards; documents optional, one human per agency retained until approved change |

Do not add fake success endpoints just to populate this list. Requests touching supplier state, finance or AWS provisioning remain subject to existing restrictions. Review one phase at a time and keep public `/v2` routing disabled.

## Safe validation and deployment boundary

Migration `0007_user_administration` adds user revisions and API admin events; existing users/agency IDs are preserved. Tests use mocked identity and disposable PostgreSQL only. Tests must cover role denial, counts/filters, stale revisions, self-change denial, approval requirements, inactive accounts and rollback on audit failure. No live supplier, Cognito signup or money movement is part of testing.

For real deployment, separately review migration recovery, credential storage, last-admin provisioning, HTTPS, rate limiting, database role grants, per-process and aggregate capacity, alerting and the known offline JWT revocation limitation. A single healthy Lightsail instance is not evidence of high availability. Static supplier egress IP and whitelist requirements must remain explicit infrastructure constraints.
