# Product feature review: Travelian and PHPTRAVELS

Reviewed: 2026-10-23. Status: **proposed product scope for review**. Companion to the [production architecture](production-architecture.md).

## 1. Conclusion and evidence limits

**Priority update from the user:** build incrementally. First complete reliable data loading and a single Dhillu & Sahi Group Ticketing booking flow, with the required security, ownership, duplicate prevention and supplier-contract controls. Treat the broader comparison below as a future enhancement backlog, not a requirement to implement the whole platform before the first narrow release. See the architecture's immediate delivery scope. Additional suppliers, products, agency distribution and HA infrastructure are separately scheduled increments; current production remains unchanged.


Travelian needs an explicit B2B/B2C product roadmap alongside its security and availability design. The architecture foundation remains appropriate, but the initial specification did not describe pricing, agency finance, documents, reporting, and after-sales workflows in enough product detail.

PHPTRAVELS is a useful feature benchmark. Reviewing public pages verifies what the vendor advertises, not its implementation, security, availability, supplier compatibility, or fitness for Travelian. No authenticated demo, source-code audit, purchase, supplier booking, or runtime reliability test was performed. No recommendation to buy PHPTRAVELS or migrate to PHP is implied.

Sources reviewed:

- **S1:** [Platform overview](https://phptravels.com/) — advertises B2B/B2C channels, travel products, supplier integrations, API/webhooks and mobile delivery.
- **S2:** [Feature catalogue](https://phptravels.com/features) — lists billing/invoicing, content, promotions, localization and configurable product capabilities.
- **S3:** [B2B portal](https://phptravels.com/b2b-travel-portal) — describes agency networks, layered pricing, credit controls, scoped access, branded documents and reporting.
- **S4:** [Agent wallet](https://phptravels.com/b2b-agent-wallet) — describes deposits, finance approval, per-agent ledgers, credit allowance, repayment and refunds.
- **S5:** [Documentation landing page](https://docs.phptravels.com/) — accessible introductory documentation; not evidence of internal authorization or high availability.

Vendor sources inform feature categories only. Travelian priorities and acceptance criteria below are our recommendations. Vendor examples of holding, debiting or confirming a booking must not be copied as Dhillu & Sahi financial semantics: those require its own authoritative contract.

## 2. Feature comparison and recommended priority

Definitions: **P0** is a prerequisite for any enabled live booking; **P1** is a candidate initial commercial release feature, required only for the enabled sales/payment model; **P2** is later expansion. A screen, interface, localStorage record or synthetic test is not a completed production capability. Status describes the inspected repositories, not deployed production verification.

| Capability | Public reference | Travelian evidence and current gap | Proposed priority / owner |
| --- | --- | --- | --- |
| B2B/B2C identities and agency membership | S1, S3 | Cognito exists; user/agency models exist; browser-derived roles and missing trusted membership block authorization | P0: portal identity |
| Staff, agency hierarchy and scoped access | S3 | `agencyId`, `parentAgencyId`, `ownerUserId` declared; UI filtering is not backend enforcement | P0: membership and access isolation; optional delegation requires explicit approval |
| Search, availability and supplier aggregation | S1 | Dhillu & Sahi gateway read flows tested per user; other product/supplier maturity varies | Preserve working Group Ticketing; add suppliers only with verified contracts |
| Booking ownership and lifecycle | S3 | Browser/server booking representations lack consistent trusted owner linkage; local gateway guard denies creation/details | P0: portal commerce and gateway attempts |
| Role-specific controls, MFA and audit | S1, S3 | Browser role/navigation checks exist; no complete backend permission flow; sign-in code reports MFA challenges without completing them | P0: backend least privilege/audit; privileged MFA policy and supported sign-in flow before privileged launch |
| Agency/tier/channel pricing and commission | S3 | Markup engine, pricing-rule API and backend pricing service exist; complete tenant-safe rule governance and quote binding not established | P1: server-side commercial pricing; before selling with differentiated prices |
| Wallet deposits, approvals and statements | S4 | Wallet models and UI/React-state operations exist; not proof of a durable financial ledger | P1: finance module; P0 ledger/reservation controls if wallet is enabled at launch |
| Credit limits, exposure, repayments and overdue policy | S3, S4 | No verified authoritative credit facility found in reviewed membership/booking flow | P2 by default; becomes P0 if credit-funded booking is enabled |
| Customer checkout and payment reconciliation | S1 | Payment API/PayFast backend exist; binding payment, booking ownership and settlement needs verification | P1; financial correctness is P0 for the selected payment method |
| Invoices, vouchers and ticket delivery | S2, S3 | Ticket/invoice PDF helpers and ticket records exist; authoritative issuance and ownership linkage incomplete | P1: documents module; never represent a local PDF as supplier ticket issuance |
| Amendment, cancellation and refund service | S3, S4 | Portal provider action routes exist; Dhillu & Sahi lifecycle contracts incomplete | P1: documented operational handling; automate only supported supplier actions |
| Reporting: booking volume, margin and settlement | S3 | Report screens/models exist; not verified as tenant-safe reports from reconciled financial records | P1: operations/finance read models |
| CMS, offers, coupons and communications | S2 | Content, coupons, notifications and public pages already exist in the portal | P1: reuse and secure existing modules; defer unnecessary rebuild |
| Languages and currencies | S2 | LocaleContext supports language/currency choices; display choice is not settlement/FX support | P1: define supported launch languages and settlement currency; P2 broader FX/settlement |
| Hotels, tours, Umrah, visas and transfers | S1, S2 | Portal pages/models and some backend services exist; gateway Hotels adapter is not a complete booking integration | P2 per product, unless separately prioritized; require contracts and product-specific rules |
| White-label distribution and mobile/partner APIs | S1 | Existing branding is not evidence of tenant custom-domain management or partner authorization | P2: design reusable contracts; do not build multi-brand provisioning or apps in security Phase 1 |
| Other products such as rail, ferries or eSIM | S1 | No inspected Travelian supplier contracts establish these integrations | Deferred candidates, not assumed requirements |

Product names alone do not establish equivalent behavior. Group Ticketing must remain separate from individual flight fares and tour/group-package products. Similar vendor terminology does not authorize moving Dhillu & Sahi into Live Ticketing.

## 3. Required architecture refinements

Keep the two-backend boundary. Add explicit modules inside the portal backend before considering more independently deployed services:

| Proposed module | Responsibility and acceptance criterion |
| --- | --- |
| Identity and agency management | Verified subject-to-membership; active status; reviewed delegation; no inferred access from email/localStorage |
| Commercial pricing | Versioned supplier/channel/agency pricing rules, precedence, currency and rounding; bind accepted totals to quote; keep supplier net/margins out of unauthorized responses |
| Finance | Chosen payment methods, ledger/reservations, approvals, settlement and refunds; idempotent callbacks; no UI-controlled balances |
| Documents | Ownership-checked invoices/vouchers; immutable/versioned commercial references; supplier-backed ticket status; private authorized downloads |
| After-sales operations | Service requests and history; human work queue for unsupported amendments/refunds; no invented supplier endpoints |
| Reporting | Tenant-scoped read models from authoritative records; financial totals reconcile to ledger; restrict net-rate visibility separately |
| Content and communications | Reuse existing content/notification services; authorized edits, delivery retries, consent and privacy controls |

Gateway responsibilities stay supplier integration, normalization, durable attempts, supplier error isolation and reconciliation. Commercial credit policy and agency markup configuration do not belong inside vendor adapters.

Extend the proposed data model with pricing-rule versions, accepted commercial price breakdowns, invoice/document versions, deposit approvals, settlement references and after-sales requests as their features are approved. Credit facilities and FX records remain optional until those business models are selected. These are proposals, not new schemas or migrations in this change.

For permitted multiple markup layers, define rule order, ownership of each layer, commission basis, rounding, tax handling and disclosure before implementation. Lock the accepted calculation version with the booking. Do not recalculate a historical invoice using today's rules.

A parent/sub-agency hierarchy is a distribution relationship, not automatic permission to view all descendant passengers, supplier costs or financial records. Document delegation, finance scope and visibility independently.

The current static outbound IP **52.76.26.224** remains protected. Broader product scope increases supplier-contract and IP-allowlisting work; it does not change the egress migration gate or authorize a new public route.

## 4. Revised Phase 1 and roadmap

Avoid mixing three different meanings of phase:

1. **Architecture Step 1:** now includes this product scope review as well as infrastructure/security design. Feature priorities and launch channel must be reviewed before freezing the implementation backlog.
2. **Booking Security Phase 1:** remains narrowly focused on verified identity, membership, role permissions, agent binding and details ownership. It is still BLOCKED on authoritative integration decisions; the local fail-closed guard stays. Broader features do not justify bypassing it.
3. **First commercial release:** complete a narrow Group Ticketing flow for the approved channel/payment method, with dependable booking attempts, agreed financial handling, essential documents, operational support and reconciliation. Then expand products and channels with their own acceptance gates.

Recommended sequence:

- Decide B2B-first versus simultaneous B2B/B2C launch. B2B-first is the proposed smaller pilot, not a final business decision.
- Select prepaid-only versus approved credit. Defer credit unless it is necessary; if selected, include financial limits and concurrent-spend testing before booking enablement.
- Complete security and booking reliability prerequisites from the architecture specification.
- Complete launch pricing, payment/ledger, documents, support and reporting for that selected flow.
- Prove the redundant platform and static egress in staging, then conduct a separately approved production pilot.
- Expand to other suppliers/products, currencies, white-label distribution or native apps only after prioritization and contract verification.

Automated refund, ticketing or cancellation must not be promised merely because a UI or competitor page describes it. Until supplier support is confirmed, define the permitted manual operation and truthful customer status.

## 5. Acceptance evidence and decisions

| Workstream | Minimum evidence before declaring it complete |
| --- | --- |
| Identity/agency | Missing/forged/expired identity denied; impersonation/cross-agency tests; authorization outage fails closed |
| Pricing | Correct channel/agency rules, versioned acceptance, deterministic rounding, no unauthorized supplier-net exposure |
| Wallet/credit when enabled | Durable approval/ledger; simultaneous-spend protection; replay-safe deposits/refunds; Unknown attempt reservation policy; exposure cannot silently exceed approved limits |
| Booking | Exactly one intended dispatch per durable attempt, duplicates/crashes/timeouts handled; supplier contract verification; no live booking under this review |
| Documents | Authorized access, truthful supplier status, consistent invoice/booking amounts and references |
| After-sales | Approved manual/automated paths, actor/history, supplier and finance reconciliation |
| Reporting | Scoped queries; totals reconciled against authoritative records; no browser-derived financial truth |
| Platform | Agreed load/failover/restore evidence and supplier-approved static source IPs; public marketing claims are not an availability test |

Open decisions: launch channel; required initial products; agent/sub-agency tiers; markup/commission rules; prepaid/credit policy; finance approval roles; net-rate visibility; document obligations; supported languages/currencies; permitted after-sales actions; API/mobile/white-label timing. These require business approval, not inferred requirements from a competitor.

No code, runtime tests, infrastructure or production configuration were changed for this review. Review outcome: **retain the security/HA architecture; expand the product specification before freezing the release backlog**. Full PHPTRAVELS feature parity is neither verified nor automatically adopted.
