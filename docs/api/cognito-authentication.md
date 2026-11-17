# Local Cognito access-token verification

Current authority: [PostgreSQL users and roles](users-and-roles.md). Migration `0006_users_roles` replaces human group-based admin/booking role checks; earlier group-policy notes below are historical. Explicit local administrator provisioning is required.

Status: implemented locally; not deployed. Token verification establishes a user identity, not agency membership, booking ownership or permission to spend money. No new Cognito pool/client is created.

## Configuration

Install the updated `requirements.txt`. Optional settings are intentionally blank in `.env.example` and have no enabled production defaults:

```dotenv
COGNITO_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_wH7XSjisU
COGNITO_APP_CLIENT_ID=1h9q50o6q8112en493onho8lqg
```

These are the non-secret identifiers supplied by the user. The pool issuer was confirmed through public discovery; the client ID's association with the portal/pool has not been independently verified in the AWS console. Confirm it before enabling the configuration in any deployment. Edit only an approved local environment initially; do not create a replacement pool, send tokens in chat, or change production settings as part of local testing.

`COGNITO_ISSUER` is separate from the hosted login domain. It is also independent of `AWS_REGION`: the existing supplier SSM region can remain Singapore while Cognito uses Virginia. No AWS access keys, Cognito client secret or supplier credentials are needed to verify public-key signatures.

Only configured Cognito issuer URLs are accepted; incoming token `iss`/`jku` values cannot choose a signing-key URL. Other identity providers or AWS partitions need a separate reviewed configuration change.

## Call

After signing into the existing portal, the client sends the Cognito **access token**, not an ID/refresh token:

```http
GET /v1/auth/me
Authorization: Bearer <access-token>
```

The prefix follows `API_PREFIX`. The response contains only `subject`, `issuer` and `client_id`. It never returns the bearer token, refresh token, supplier credentials or passenger records. This does not change production DNS or public routing.

Use a local API client with the token held privately in its authorization field. Do not place real tokens in URLs, source code, screenshots shared in chat, or shell-history command arguments. Mocked tests require no real portal login or tokens.

## Checks and responses

- RS256 header and actual RSA signature using the configured pool's JWKS.
- Required issuer, subject, expiration, issued-at, client ID and token-use claims.
- Exact issuer and app-client match; `token_use=access`.
- Expiration, future issued-at and not-before checks; no clock-skew allowance currently configured.
- Nonblank string subject; app-only identity where `sub=client_id` is rejected.
- Exactly one Authorization header; malformed/missing bearer credentials are rejected when configured.
- Fixed maximum token length to bound parsing input.

401 with a Bearer challenge means missing/invalid identity. 503 means missing/invalid authentication configuration or an unavailable/unusable signing-key service. Errors are generic; raw tokens and internal key-fetch exceptions are not returned or logged by the verifier.

The verifier uses PyJWT with cryptographic support. Public key sets are cached per process for five minutes; individual keys are not cached indefinitely. Unknown key IDs use the library's bounded refresh cooldown, currently 30 seconds by default, so a newly rotated key can briefly be rejected until refresh is allowed. Key fetches have a three-second timeout and serialized refresh per process. Blocking key retrieval and RSA verification run outside the ASGI event loop. These are per-process controls, not a global production abuse/rate-limit system.

Offline signature validation does not detect every token revoked before expiration. Approve token lifetime/revocation behavior and privileged MFA policy before booking enablement. Signed groups, browser roles and service credentials do not automatically confer booking permissions. Server-side membership/ownership and any future API scope policy remain separate.

## Booking remains closed

The booking dependency now authenticates first:

- Authentication missing/invalid configuration: 503, no supplier access.
- Configured authentication but missing/invalid token: 401, no supplier access.
- Valid identity with missing agency policy: 503, no supplier access.
- Configured policy with missing agency or unapproved group: 403, no supplier access.
- Valid identity and agency entitlement: still 503 until authoritative assignment and booking ownership are integrated, no supplier access.

There is no enable flag, caller-header bypass, or automatic permission granted to an admin-labelled token. Existing PNR mapping tests explicitly override the whole guard in their isolated test app only. Inventory, search, single-ticket inventory details and availability have not been assigned a new login requirement in this increment.

See [booking authorization setup](booking-authorization.md) for the local group/agency policy and remaining blockers. Next: authoritative active user/agency assignments, supplier-agent mapping and persisted booking ownership; then durable booking attempts/idempotency and reconciliation. This implementation is not completion of booking security Phase 1 or approval for live booking.

## Verification

`tests/core/test_authentication.py` generates RSA private keys only in memory, signs synthetic tokens and mocks the JWKS HTTP response. It covers valid signatures, required claims, wrong issuer/client/use, expiration/future timestamps, forged signatures, unsupported algorithms, malformed/oversized tokens, unknown/missing key IDs, key rotation/cache, signing-service failure, missing configuration, duplicate headers, identity response redaction, and denial of supplier access even with valid identity. Test keys and tokens are not production credentials and are not saved.

References: [AWS Cognito verification](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-verifying-a-jwt.html), [PyJWT API and key caching](https://pyjwt.readthedocs.io/en/latest/api.html).
