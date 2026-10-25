# Agency server-to-server authentication

Local foundation; no Cognito clients, secrets, domains or resource servers have been created. Portal login remains separate. Agency backend integrations use Cognito OAuth client credentials, not user passwords or supplier credentials.

## Provisioning boundary

Before issuing real credentials, approve a Cognito resource-server identifier, full custom scope names, a pool token endpoint/domain, and dedicated confidential app clients. Each integration needs its own client, allowing **client credentials only**; do not modify the public `travelian-web` client. Confirm AWS M2M pricing before provisioning. Deliver the client secret securely to the agency's backend operator; never put it in browser code, database rows, source control, chat or log output. The gateway registry contains no secret.

Scopes are deliberately disabled by default:

```dotenv
INTEGRATION_SEARCH_SCOPE=
INTEGRATION_BOOKING_CREATE_SCOPE=
INTEGRATION_BOOKING_DETAILS_SCOPE=
```

Values must be approved full custom scope strings such as `<resource-server>/<scope>`, matching Cognito configuration exactly. There are no implicit scopes or group-based machine grants. Scope configuration does not create AWS resources.

After the Cognito client has been provisioned through an approved administrative process, register its mapping locally using an authenticated human administrator with configured agency-write permission:

```http
POST /v1/admin/agency-integrations
Authorization: Bearer <human admin access token>
Content-Type: application/json

{"client_id":"<provisioned-client-id>","agency_id":"<approved-agency-id>","scopes":["<approved-resource>/<search-scope>"]}
```

This only registers an existing client; it neither creates nor verifies its Cognito configuration or returns a secret. The agency must be approved and active. Duplicate issuer/client mappings return 409; portal client ID is rejected. Scope selection must be a subset of configured permissions. Separate read/write admin policies deny access by default.

Admin APIs:

- `GET /v1/admin/agency-integrations?agency_id=...&limit=20&offset=0`: list registry metadata, paginated up to 100 records.
- `POST /v1/admin/agency-integrations/{client_id}/revoke`: disable registry access, idempotently returning 204 for an existing client.

Registration/revocation audit history, safe AWS provisioning/secret delivery and rotation automation remain pending. These APIs must stay internal until those operational controls and approved admin policies are ready. Disabled clients cannot be re-enabled by re-registering; use a new approved client. No account/agency reassignment endpoint exists.

## Agency token flow

The agency backend sends an HTTPS request to the **approved Cognito domain**, not the issuer/JWKS URL:

```http
POST https://<approved-cognito-domain>/oauth2/token
Authorization: Basic <encoded-client-id-and-secret>
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&scope=<approved-resource>/<search-scope>
```

Use a secure OAuth client; the example contains placeholders only. Cognito returns an access token and expiry, not user ID/refresh tokens. Cache it securely until shortly before expiry; obtain a new token with client credentials rather than calling `/auth/refresh`. Do not send client secrets to inventory/booking endpoints. User `/auth/login`, `/auth/refresh`, `/auth/logout` remain human-session operations.

## Gateway verification

- `GET /v1/auth/integration`: returns machine identity and agency ID after configured search-scope checks; no token/secret is echoed.
- `GET /v1/integrations/group-ticketing/inventory`
- `POST /v1/integrations/group-ticketing/search`: same search body and combined `data` response as existing Group Ticketing search.

Protected integration entry points reuse existing aggregation and normalization. Existing public inventory/search retain their behavior; these aliases do not make the original routes private or introduce per-vendor failure summaries.

JWT signature, issuer, expiration, issued-at, token use and exact client ID are verified. Machine identity specifically requires `sub == client_id`; human routes reject it. A bounded unverified client ID is only a registry lookup hint before signature validation, never an identity decision. JWKS location is fixed by configured issuer. Only enabled registry entries belonging to approved active agencies pass. Each operation checks the required scope in both the signed token and the local registry. No header/body agency ID is trusted.

Registry/agency state is read per request, so disabling the mapping denies subsequent integration requests even if JWT signatures remain valid. This is gateway-local revocation, not Cognito token revocation and not cancellation of in-flight requests. Human/agency roles are never inherited by machine clients. Registration changes are not yet audited.

Shared booking routes recognize machine tokens and require their configured create/details scope and mapping, **then still return 503 without supplier dispatch**. Machine booking ownership, durable attempt/idempotency and reconciliation are incomplete. Do not enable bookings by merely registering a client. Existing public search access is unchanged.

## Local preparation and remaining work

Run `alembic upgrade head` locally; expected revision `0004_agency_integrations`. No startup migrations or production changes occur. Runtime uses existing private database settings. The one-human-user-per-agency constraint is unchanged; machine integrations are separate and an agency may have multiple individually revocable clients.

Tests use synthetic RSA tokens, mocked JWKS, mocked aggregation and disposable PostgreSQL. Missing scopes, wrong identity kind, disabled/unregistered mappings, suspension/unapproved agencies, duplicate registration and default-deny booking are covered. Live OAuth exchange, AWS client provisioning and secret rotation are not tested or performed. Production shared rate limits, static outbound IP/whitelist continuity, secret delivery, audit and capacity controls remain prerequisites.

AWS references: [client-credentials token endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html), [machine access-token subject](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-access-token.html), [custom scopes](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-define-resource-servers.html).
