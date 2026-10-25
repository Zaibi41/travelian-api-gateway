# Booking authorization: local foundation

Current authority: [PostgreSQL users and roles](users-and-roles.md). Migration `0006_users_roles` replaces human group-based admin/booking role checks; earlier group-policy notes below are historical. Explicit local administrator provisioning is required.

Status: BLOCKED for live booking. No Cognito groups or agency attribute exist yet, as confirmed by the operator. No AWS resources, local secrets or production configuration were changed.

Authentication verifies the configured Cognito access token. Authorization then requires an exact match against explicitly configured Cognito groups and a signed custom agency claim. Request headers, body fields, email domains and supplier agent IDs cannot establish an agency identity. Create and details permissions are independent; there is no implicit administrator bypass.

The settings remain disabled by default:

```dotenv
COGNITO_AGENCY_CLAIM=
BOOKING_CREATE_GROUPS=[]
BOOKING_DETAILS_GROUPS=[]
```

Group lists use JSON arrays. Names must be explicitly approved and must match signed `cognito:groups` values exactly. The agency claim must use the custom namespace, and its value must contain 1–128 ASCII letters, digits, underscores or hyphens, starting with a letter or digit. This is a proposed gateway contract; it does not describe an existing portal relationship. Internal authorization claims are excluded from `/v1/auth/me` responses.

Missing configuration returns 503; invalid identity returns 401 when authentication is configured; a configured policy denies missing agency claims or unapproved groups with 403. Even a fully entitled user still receives 503 from both booking routes. No setting currently enables supplier booking dispatch.

## Next setup, subject to approval

1. Confirm the existing app client belongs to the supplied pool. Approve one booking group and an agency attribute name. For example, `agency_booker` and `custom:agency_id` are candidate names, not provisioned resources. The same approved group can appear in both permission lists if agency users need both operations.
2. Make assignment administrative. Remove the agency attribute from **every** app client's writable attributes, including signup clients. Review federation mappings as well. Immutable attributes alone do not prevent caller assignment during signup if client write permission remains. Group membership must also be administrator managed.
3. Configure a trusted pre-token-generation integration to emit the approved agency claim into **access tokens**. Custom user attributes are not automatically access-token claims. Confirm pool feature-plan eligibility for a V2_0/V3_0 trigger; do not assume an ID-token attribute is sufficient. The trigger must use trusted assignment data and omit authorization claims for inactive or unassigned users.
4. Establish an authoritative active assignment with uniqueness for both Cognito `(issuer, sub)` and agency ID. Cognito custom attributes alone do not enforce one user per agency. Decide on durable storage and the administrative assignment/reassignment process before enabling bookings.
5. Persist booking ownership and supplier reference alongside the verified issuer, subject and agency. Resolve ownership locally before fetching supplier details. The supplied ownership helper returns the same 404 for missing and foreign records and requires details permission, but it is not wired to a repository yet. Account replacement needs an audited ownership-transfer policy because subject matching currently denies replacement accounts.
6. Add durable booking-attempt/idempotency and reconciliation handling before enabling supplier dispatch. Confirm server-controlled supplier-agent mapping; an agency ID is not a supplier agent ID. Decide token lifetime, revocation and account-disable behavior; offline JWT validation alone does not immediately revoke issued tokens.

The smallest next administrative step is approving the group and attribute names and reviewing the existing app client's attribute-write settings. Do not enable production booking by merely filling these environment values.

## Local verification

Tests use synthetic signed tokens and mocked JWKS/supplier transport only. They cover missing/invalid identity, exact role permissions, absent or malformed agency claims, forged agency headers/body fields, cross-agency/subject/issuer ownership, response redaction, and supplier denial even for entitled callers. Ownership tests exercise a policy helper, not persistence or a complete booking flow.

Inventory, search, single-ticket inventory details, availability and supplier PNR handling retain their existing behavior. No login, signup or Cognito provisioning endpoint is introduced.

AWS references: [custom attributes and app-client permissions](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-attributes.html), [pre-token generation and access-token customization](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-pre-token-generation.html), [access-token claims](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-access-token.html).

## Local database foundation

Versioned agency and Cognito assignment tables are now prepared. Follow [local database setup](agency-database.md). The booking guard now verifies an active stored assignment against the signed identity and agency claim. Booking ownership and durable attempt handling remain incomplete, so dispatch stays blocked. AWS read-only inspection subsequently confirmed an existing `agency_owner` group; reuse can be approved instead of creating the earlier candidate `agency_booker`. No agency custom attribute or token-generation trigger was present at inspection.
