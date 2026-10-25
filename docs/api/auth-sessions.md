# Cognito session APIs

Implemented locally, not deployed. Paths use the configured API prefix (normally `/v1`). Existing `/v1/auth/me` continues returning verified identity only. These routes do not change agencies, roles or supplier access.

| Method | Endpoint | JSON body |
|---|---|---|
| POST | `/v1/auth/login` | `username`, `password` |
| POST | `/v1/auth/refresh` | `refresh_token` |
| POST | `/v1/auth/logout` | `refresh_token` |
| POST | `/v1/auth/forgot-password` | `username` |
| POST | `/v1/auth/reset-password` | `username`, `confirmation_code`, `new_password` |
| GET | `/v1/auth/me` | None; send `Authorization: Bearer <access-token>` |

Login's username is the account's Cognito sign-in identifier (email for the inspected pool). Passwords and tokens go in HTTPS request bodies, never URLs. Do not paste real tokens in chat or shell history. Request schemas reject extra fields. Error responses omit input values and provider exception messages. Secret request fields are masked in model representations; no request bodies or token responses are logged by this implementation. Configure proxy/APM logging to exclude credentials before production use.

Login uses `InitiateAuth/USER_PASSWORD_AUTH`. Successful login returns `access_token`, `id_token`, `refresh_token`, `token_type: Bearer` and `expires_in` seconds. Use the access token for gateway requests, not the ID token. Tokens are delivered only in the intended response body, marked `Cache-Control: no-store` and `Pragma: no-cache`. The gateway does not persist tokens, passwords or browser sessions. Frontend credential storage and cookie/CSRF design need review when connecting the portal.

Refresh uses `InitiateAuth/REFRESH_TOKEN_AUTH` for the inspected public app client without refresh-token rotation. Its response returns new access/ID tokens and expiry; it normally omits `refresh_token`, so retain the existing refresh token. This implementation does not support rotating-refresh configurations, remembered device keys or confidential clients requiring a client secret. Review the flow if those settings change; errors fail closed.

Logout calls `RevokeToken` for the submitted refresh token and returns 204 after Cognito accepts it. It does not require an unexpired access token, allowing refresh-token holders to sign out after access expiry. It is not global sign-out, does not clear hosted-login cookies, and does not sign out other sessions. The frontend must clear its local credentials after successful logout. If revocation fails or times out, the endpoint returns an error rather than reporting success.

**Offline JWT validation can still accept revoked access tokens until expiration.** This is the existing verifier's limitation; logout does not add immediate gateway revocation enforcement. Decide on short token lifetimes and server-side revocation/session enforcement before enabling financial operations. Booking dispatch remains blocked.

## Configuration and limits

Uses `COGNITO_ISSUER` and `COGNITO_APP_CLIENT_ID`. SDK region comes from the configured issuer, independently of supplier `AWS_REGION`. Public Cognito APIs use unsigned requests; no IAM access keys, administrator permissions or client secret are required. The existing inspected client supports password and nonrotating refresh flows and has revocation enabled. No AWS resource configuration is changed by this implementation.

Invalid credentials, nonexistent/unconfirmed accounts and password-reset-required accounts return the same 401. Provider throttling returns 429. Missing configuration, timeouts, unsupported configuration and malformed provider results return generic 503. SDK calls run outside the ASGI event loop, use connection/read timeouts of 3/5 seconds, and are not automatically retried.

MFA, forced new passwords and other Cognito challenges return 409 **without issuing tokens or exposing the Cognito challenge session**. Challenge completion is not implemented; affected users must complete sign-in through an existing configured Cognito flow. Do not turn off MFA to work around this limit. Signup, signup email confirmation and challenge-response endpoints remain outside this increment. Forgot-password confirmation is supported separately below.

Shared authentication rate limiting, production HTTPS/exposure review and challenge support are still required before treating this as a complete public login service. These endpoints do not grant booking permissions.

## Verification

Tests mock the Cognito SDK; there are no real logins, token refreshes or revocations. Tests cover exact request mapping, token responses, no-store headers, missing configuration, provider errors, timeouts without retries, throttling, challenge denial, malformed responses and validation redaction. Existing signature tests continue verifying `/auth/me` authentication.

References: [InitiateAuth](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_InitiateAuth.html), [refresh flows](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-refresh-token.html), [revocation limitations](https://docs.aws.amazon.com/cognito/latest/developerguide/token-revocation.html).


## Password recovery

`POST /v1/auth/forgot-password` calls Cognito `ForgotPassword` with the sign-in username/email. It returns HTTP 200 with the same generic acknowledgement for eligible deliveries and account-specific eligibility errors; it never returns the reset code or masked destination. Operational failures return 503 and throttling returns 429, so this does not guarantee identical timings or conceal every provider-level difference. Shared abuse/rate limits remain necessary before public exposure.

`POST /v1/auth/reset-password` calls Cognito `ConfirmForgotPassword` using `username`, `confirmation_code` and `new_password`. No login token is needed; the reset code is the recovery credential. Success returns HTTP 200 with a confirmation message, not tokens. Then call `/v1/auth/login` with the new password.

Use these local examples with private values substituted only on your own machine (shell history can retain credentials):

```bash
curl --request POST 'http://127.0.0.1:8000/v1/auth/forgot-password' \
  --header 'Content-Type: application/json' \
  --data-raw '{"username":"YOUR_COGNITO_EMAIL"}'

curl --request POST 'http://127.0.0.1:8000/v1/auth/reset-password' \
  --header 'Content-Type: application/json' \
  --data-raw '{"username":"YOUR_COGNITO_EMAIL","confirmation_code":"YOUR_CURRENT_RESET_CODE","new_password":"YOUR_NEW_PASSWORD"}'
```

An invalid/expired code or invalid recovery request returns generic 400; a rejected password policy returns generic 400 without provider details. Cognito owns password policy and code expiration. This is a password-recovery code, not signup confirmation or MFA. Calling forgot-password requests a new code; if you already have a current recovery code, call reset-password directly. A real reset changes the Cognito account even when invoked through a locally running gateway. No real recovery operations were performed during implementation.

Codes/new passwords use secret-valued request models; validation errors do not echo input, and all success/error responses use no-store headers. No secrets are persisted. SDK retries remain disabled, and a timeout must not be presented as confirmed success. Verification uses mocked SDK operations only.

AWS contracts: [ForgotPassword](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_ForgotPassword.html), [ConfirmForgotPassword](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_ConfirmForgotPassword.html).
