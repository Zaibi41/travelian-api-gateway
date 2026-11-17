# Travelian API

A starter API gateway for Travelian's OTA. Product areas are separated into Live Ticketing, Group Ticketing, and Hotels so each can have independent supplier adapters and workflows behind one API host.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive API docs.

| Endpoint | Purpose | Response |
| --- | --- | --- |
| `GET /health` | Application is responding | HTTP 200, `{"status":"ok"}` |
| `GET /ready` | Application is ready to serve requests | HTTP 200, `{"status":"ready"}` after startup configuration loads; otherwise HTTP 503, `{"status":"not_ready"}` |

These endpoints are also available under `/v1` (or the configured API prefix). Readiness tracks application startup and shutdown; it does not call supplier APIs.

## Caller identity

See [local Cognito token verification](docs/api/cognito-authentication.md) for configuration and `GET /v1/auth/me`. Identity verification alone does not enable bookings or establish agency membership.

## Product APIs

All product APIs are versioned under `/v1` and grouped by domain:

| Domain | Endpoint | Purpose | Current state |
| --- | --- | --- | --- |
| Live Ticketing | `POST /v1/live-ticketing/flights/search` | Search individual flight fares | Demo adapter; concurrent fan-out with per-vendor outcomes |
| Group Ticketing | `POST /v1/group-ticketing/flight-quotes` | Submit a group flight quote request (10+ passengers) | Accepts request with `202`; sourcing workflow pending |
| Group Ticketing | `GET /v1/group-ticketing/inventory` | Fetch all group inventory from configured suppliers | Concurrent Dhillu & Sahi and Skypass fan-out; combined Travelian `data` response |
| Group Ticketing | `POST /v1/group-ticketing/search` | Filter one-way inventory by route, date, and adults | Defaults to one adult; same combined response |
| Group Ticketing | `GET /v1/group-ticketing/tickets/{ticket_id}` | Retrieve one current inventory ticket by opaque ID | Refreshes combined inventory; no seat reservation |
| Group Ticketing | `POST /v1/group-ticketing/check-availability` | Check PNR seats and current price | Dhillu & Sahi authenticated availability check |
| Group Ticketing | `POST /v1/group-ticketing/bookings` | Book passengers against a group PNR | Dhillu & Sahi authenticated booking; no automatic retries |
| Group Ticketing | `POST /v1/group-ticketing/booking-details` | Fetch booking, passenger, payment, and group details | Dhillu & Sahi authenticated lookup |
| Hotels | `POST /v1/hotels/destinations`, `POST /v1/hotels/search` | Destination suggestions; hotels with rates by destination, dates and rooms | RateHawk (docs/api/hotels-ratehawk.md) |
| RateHawk console | `GET /v1/admin/suppliers/ratehawk/operations`, `POST /v1/admin/suppliers/ratehawk/operations/{key}` | All 48 RateHawk operations, Super Admin only | Writes behind `RATEHAWK_WRITE_ENABLED` |

Each area has its own route module and vendor package. Keep one supplier per module/package so credentials, mapping, logs, and debugging stay isolated. Supplier outcomes are recorded internally. Group inventory and search return a single Travelian response containing combined group records, without gateway-added per-vendor status fields.

## Group Ticketing inventory and search

See the [combined inventory response contract](docs/api/group-ticketing-inventory.md) for fields, current vendor coverage, empty results, and partial-failure behavior.

Fetch all currently available groups:

```bash
curl -sS 'http://127.0.0.1:8000/v1/group-ticketing/inventory'
```

Search one-way groups by origin, destination, departure date, and adult count (defaults to one):

```bash
curl -sS -X POST 'http://127.0.0.1:8000/v1/group-ticketing/search' \
  -H 'Content-Type: application/json' \
  -d '{
    "origin": "LHE",
    "destination": "RUH",
    "departure_date": "2026-10-21",
    "adults": 1
  }'
```

Both endpoints return the same combined response shape: `{ "data": [...] }`. Records use the generic Travelian schema and omit supplier names and routing identifiers. Search fetches configured vendor inventory concurrently and applies route/date/capacity filters. Supplier-specific routing remains internal to the gateway.

## Live Ticketing flight search

```bash
curl -X POST http://127.0.0.1:8000/v1/live-ticketing/flights/search \
  -H 'Content-Type: application/json' \
  -d '{
    "origin": "KHI",
    "destination": "DXB",
    "departure_date": "2026-11-15",
    "adults": 1,
    "cabin": "economy",
    "currency": "PKR"
  }'
```

The response contains normalized offers and a `vendor_results` entry for each supplier (`success`, `failed`, or `timeout`) with its response time. If one supplier fails or returns no offers, results from others are still returned with `status: "partial"`. Supplier calls run concurrently and are bounded by `VENDOR_TIMEOUT_SECONDS`.

## Adding a live flight supplier

1. Create a dedicated package at `app/vendors/live_ticketing/<supplier_name>/` and implement `FlightVendor` using the supplier's async API/client.
2. Map supplier results into `FlightOffer` and `FlightSegment` in `app/schemas/flights.py`.
3. Register the adapter in the live ticketing vendor registry and load credentials from environment-backed settings. Keep credentials out of source control.

The included `DemoVendor` returns sample data so the API can be exercised before supplier contracts and credentials are available. It should be disabled when deploying real search traffic (`ENABLE_DEMO_VENDOR=false`).

### Dhillu & Sahi

Dhillu & Sahi is a **Group Ticketing vendor**. Its inventory, availability, booking, and booking-details operations belong to Group Ticketing. The supplied integration details define `POST https://api.dhilluandsahi.com/api/login` with JSON `email` and `password`, returning a token under `success.token`. Group inventory uses `POST /api/pnr-respect-to-mysector-user` with a Bearer token and optional JSON `my_sector_id`. The isolated client and response normalizer live in `app/vendors/group_ticketing/dhillu_sahi/`. Set `DHILLU_SAHI_EMAIL` and `DHILLU_SAHI_PASSWORD` in a deployment secret manager or local ignored `.env`; do not commit credentials. `my_sector_id` is a sector group id; there is no call that lists them. Set the ids as `DHILLU_SAHI_SECTOR_GROUP_IDS` (comma-separated; there is no default): the gateway makes one listing call per id and merges them, dropping repeats by supplier ticket id; a failing id is logged and the others are still shown. Unset or empty: the listing is never called, Dhillu & Sahi is left out like any unconfigured supplier, and one warning naming the setting is logged at startup. `DHILLU_SAHI_GROUP_SECTOR_ID` is no longer read. The gateway fetches inventory at `GET /v1/group-ticketing/inventory`, or filters it at `POST /v1/group-ticketing/search`. When no configured supplier can provide inventory, these routes return a generic HTTP 503. The supplier is not called for live flight searches because no live-flight contract was provided.

For local development, copy `.env.example` to `.env` in the project root and set `DHILLU_SAHI_EMAIL` and `DHILLU_SAHI_PASSWORD`. `.env` is git-ignored; do not paste credentials into source files or commit them. For deployment, provide the same names through your platform's secret/environment configuration. The app reads them through `pydantic-settings` when it starts. Exercise integrations through the actual versioned Group Ticketing routes in the API docs or with HTTP requests to the running application.

See the [Skypass integration coverage and contract blocker](docs/api/skypass-integration-status.md) for the supplied guide, existing inventory support, and missing lifecycle documentation.

Skypass Group Ticketing inventory uses `SKYPASS_ENDPOINT`, `SKYPASS_CLIENT_ID` (sent as the documented `api_key` form field), and `SKYPASS_CLIENT_SECRET` (sent as `api_secret`). The adapter authenticates with `POST /auth`, reads `access_token`, and requests `GET /tickets` with Bearer authorization. It maps all documented Skypass ticket types and their outbound sector legs into the shared inventory schema. Both inventory and filtered search can include `Two Way` records. Seat availability is left unknown because `open_seats_slot` is an identifier, not a seat count.

### AWS Parameter Store configuration

Create the parameters in **AWS Systems Manager → Parameter Store**, in the same AWS region where the API runs. Use the exact names below. These names assume `SSM_PARAMETER_PREFIX=/travelian/prod`:

| Parameter name | Type | Value to enter |
| --- | --- | --- |
| `/travelian/prod/group-ticketing/dhillu-sahi/base-url` | `String` | Dhillu & Sahi base URL |
| `/travelian/prod/group-ticketing/dhillu-sahi/email` | `SecureString` | Dhillu & Sahi account email |
| `/travelian/prod/group-ticketing/dhillu-sahi/password` | `SecureString` | Dhillu & Sahi account password |
| `/travelian/prod/group-ticketing/dhillu-sahi/group-sector-id` | `String` | Confirmed sector ID for the account |
| `/travelian/prod/group-ticketing/skypass/endpoint` | `String` | Skypass API host, for example `https://dev.skypass.pk` |
| `/travelian/prod/group-ticketing/skypass/client-id` | `SecureString` | Skypass API key (`api_key`) |
| `/travelian/prod/group-ticketing/skypass/client-secret` | `SecureString` | Skypass API secret (`api_secret`) |

For another environment, such as staging, change the prefix and use it consistently in every parameter name. For example, with `SSM_PARAMETER_PREFIX=/travelian/staging`, the Dhillu & Sahi password name becomes `/travelian/staging/group-ticketing/dhillu-sahi/password`.

Set these application environment variables in the ECS task definition, EC2 service, or local `.env`:

```dotenv
AWS_REGION=<the-region-containing-the-parameters>
SSM_PARAMETER_PREFIX=/travelian/prod
```

At startup, the API requests the configured names from Parameter Store in one `GetParameters` call with decryption enabled. A value found in Parameter Store overrides its environment setting. If an individual parameter is missing, that setting falls back to the matching environment variable, such as `DHILLU_SAHI_PASSWORD` or `SKYPASS_CLIENT_SECRET`. If AWS is unreachable or denies access, startup fails instead of silently using potentially stale credentials. Leave `SSM_PARAMETER_PREFIX` empty to use environment settings only. Values load once at startup; restart the API after changing parameters.

The API workload should use its AWS IAM role; do not put AWS access keys in `.env`. Grant the role `ssm:GetParameters` only for the relevant path. For the production prefix above, the resource ARN pattern is `arn:aws:ssm:<region>:<account-id>:parameter/travelian/prod/*`. If you encrypt `SecureString` values with a customer-managed KMS key, also grant `kms:Decrypt` for that key. See [AWS Parameter Store access guidance](https://docs.aws.amazon.com/systems-manager/latest/userguide/ps-restrict-parameter-access.html) and [SecureString decryption permissions](https://docs.aws.amazon.com/systems-manager/latest/userguide/ps-restrict-decryption.html).

Group availability checks use `POST /v1/group-ticketing/check-availability` with `{ "pnr": "33MBD5", "adult": 1, "child": 1, "price": 78000 }`. The adapter logs in and calls the documented `/api/check-availbility` endpoint with the Bearer token and those JSON fields. A valid upstream `data.status: false` means the vendor successfully answered that seats are unavailable; it is distinct from a vendor request failure or timeout.

**Booking security — integration blocked:** `POST /v1/group-ticketing/bookings` and `POST /v1/group-ticketing/booking-details` verify configured Cognito access tokens first (401 for missing/invalid identity; 503 if authentication is unconfigured/unavailable). The local [agency authorization policy](docs/api/booking-authorization.md) denies unapproved groups or missing agency claims with 403 when configured; otherwise it returns 503. Even an entitled identity receives 503 until authoritative agency assignment and booking ownership are integrated. Supplier access remains blocked. This intentionally breaks previously unauthenticated booking access. Supplier credentials, caller-provided agent IDs, and identity/role headers cannot enable these endpoints. Inventory, search, availability, and PNR handling are unchanged. `GET /v1/group-ticketing/bookings/{booking_id}` is not implemented; it must not be introduced as an unprotected alternative.

To enable bookings, supply the existing trusted identity-provider verification contract, authenticated subject-to-agency/supplier-agent mapping, approved role/permission policy, and an authoritative booking-to-agency ownership source (including bookings created outside this gateway). Then replace the fail-closed guard with verification and per-operation authorization; retain protection on the existing POST details route. No production identities or credentials are provisioned by this repository. Valid-role, agent-impersonation, and cross-agency authorization acceptance tests remain blocked until those contracts exist. Test-only dependency overrides in PNR mapping tests do not enable production access.

See the [Dhillu & Sahi selection and booking mapping](docs/api/dhillu-sahi-workflow.md) for ticket IDs, preserved supplier timestamps/sold counts, passenger-aware availability and login-derived supplier agent identity. Booking remains fail-closed pending trusted caller authorization.

Group booking uses `POST /v1/group-ticketing/bookings`. It validates that every passenger array (`titles`, `firstname`, `lastname`, `dob`, `passport_no`, `passport_expiry_date`, `type`) has the same nonzero length and that `updated_at` is timezone-aware. It maps directly to the supplier's `/api/add-booking` contract. This is a state-changing operation and is not automatically retried; a timeout is reported as an indeterminate vendor failure and should be reconciled with the supplier before the client retries.

Booking details use `POST /v1/group-ticketing/booking-details` with `{ "booking_id": 2749 }`. The supplier adapter calls `/api/agent-booking-detail` using Bearer authentication and returns normalized booking and group objects. Known fields are typed; additional supplier fields are preserved to accommodate optional admin/payment/flight details.

### Project layout

```text
app/
  api/routes/                 # HTTP routes split by product domain
  schemas/                    # Normalized API request/response models
  services/                   # Domain orchestration and supplier fan-out
  vendors/
    group_ticketing/
      dhillu_sahi/            # Dhillu & Sahi auth and group-ticketing adapter
    hotels/                   # Hotel supplier adapters
    demo.py                   # Local sample live-flight supplier
```

## Architecture roadmap

The [production architecture and migration specification](docs/architecture/production-architecture.md) covers the portal and gateway redesign, identity and agency ownership, booking reliability, mandatory static outbound IPs, high availability, migration gates, and unresolved decisions. It is a draft for review, not approval to provision resources or enable bookings. It distinguishes current user-reported production status from historical deployment-audit observations below.

## Production deployment

Pull requests into `main` run CI on Python 3.12. A push to `main` runs the production workflow: test, package that commit and its resolved runtime dependencies, verify SHA-256 locally and remotely, upload over verified SSH, activate a release, restart systemd, and verify `GET /health`. Manual workflow runs deploy only when the selected branch is `main`. Merging into `main` triggers deployment, so merge only after deployment approval and the prerequisites below are complete.

Production uses the existing Lightsail instance `travelian-api-prod-02` in `ap-southeast-1`, outbound IP `52.76.26.224`, SSH user `deploy`, and `/opt/travelian-api`. The service binds to `127.0.0.1:8000` with one Uvicorn worker. Public traffic reaches it through Caddy on the same instance: `https://api.travelian.pk/v1/*` goes to the gateway and every other path goes on to the legacy API Gateway + Lambda (`deploy/Caddyfile`). PostgreSQL runs on the same instance. One-time server setup, migrations and the DNS cutover are in [deploy/CUTOVER.md](deploy/CUTOVER.md) (`deploy/server-setup.sh`, `deploy/migrate.sh`); the CI deployment itself still changes no DNS, proxy or firewall settings.

Before the first merge:

- An administrator must enable `travelian-api.service` at boot; it was disabled during the 2026-10-23 audit. Existing deployment sudo permissions do not include enablement and need not be broadened.
- Provision `/opt/travelian-api/shared/.env`, readable by `deploy` with mode `600`. It was missing during the audit. Set `ENABLE_DEMO_VENDOR=false` and configure authoritative supplier settings or `AWS_REGION=ap-southeast-1` and `SSM_PARAMETER_PREFIX` with working Parameter Store access. Deployment refuses to activate without the shared environment file. Health/readiness do not certify supplier configuration or availability.
- Confirm the production environment contains `LIGHTSAIL_SSH_PRIVATE_KEY`, `LIGHTSAIL_HOST`, and the previously verified `LIGHTSAIL_SSH_KNOWN_HOSTS`. The audit confirmed these names exist. Add a `main` deployment policy and required PR checks when the GitHub plan permits; the environment currently has no protection rules, and the branch-protection API returned a plan restriction. The workflow independently checks `main` and tests before deployment.
- Agree on a separate, approved `/v2` proxy integration and access controls before public traffic. The booking routes have no application-level authentication. For a proxy preserving the `/v2` path, configure `API_PREFIX=/v2`; otherwise agree on the rewrite first. Existing DNS and production routing must remain untouched by this workflow.

Release directories are named `<commit-SHA>-<workflow-run-ID>-<attempt>`. A workflow rerun creates a fresh directory, including after a failed first deployment. Existing releases are never overwritten or automatically deleted. Both preparation/activation and manual rollback use the shared deployment lock; extraction also takes the lock. Partial directories remain for diagnosis, while new attempts use different directories.

The deployment script requires Python 3.12, creates a virtual environment per release, installs the artifact's `requirements.lock` using binary wheels without a pip cache, checks dependencies, and atomically switches `current`. Health verification makes up to 30 attempts with a three-second curl timeout and two-second pauses (about 150 seconds maximum). A failed activation restores, restarts, and verifies the previous release and exits nonzero. On a failed first deployment, it stops the service and removes the failed active link. A failed stop is reported as critical. There is no prior application to restore on the first deployment.

After a healthy deployment, the release receives `.deployment-success`. Manual rollback accepts only a release with that marker and its Python environment:

```bash
bash /opt/travelian-api/current/deploy/rollback.sh <previous-release-ID>
```

Rollback restarts and verifies the target; if it fails, it restores and verifies the original release and returns failure. An already active rollback target must still pass health verification. Shared configuration is not versioned or reverted with application code; keep it compatible with retained rollback releases. Releases are retained without automatic cleanup; monitor disk usage.

After deployment, verify `systemctl is-active travelian-api.service`, `systemctl is-enabled travelian-api.service`, and `curl --fail http://127.0.0.1:8000/health` over SSH. Expected health is `{"status":"ok"}`. Check `/ready` and `journalctl -u travelian-api.service` for startup failures. A localhost health check does not verify the separate public `/v2` routing.

See [the production audit](deploy/PRODUCTION_AUDIT.md) for findings, validation, and the approval boundary.

Agency onboarding: [local application submission and applicant status](docs/api/agency-applications.md). Applications remain pending and agencies suspended; booking dispatch is blocked.

Agency management: [profile, admin review and audit APIs](docs/api/agencies.md). Documents are optional; admin policies deny access by default.

Cognito session APIs: [login, refresh, logout and current identity](docs/api/auth-sessions.md). Challenge completion and immediate offline access-token revocation remain outside this increment.

Agency machine integration: [client credentials, mapping and scoped inventory/search](docs/api/agency-integrations.md). AWS provisioning remains pending; no client secrets are stored by the gateway.

Agency registration requires business contact name, email and international phone. See [contact fields and migration](docs/api/agency-contacts.md) before using name-only registration examples.

Human roles now come from PostgreSQL `users`/`roles`, not Cognito groups. See [users, roles and mandatory local admin bootstrap](docs/api/users-and-roles.md) before migrating. Booking dispatch remains blocked.

OTA scope: [researched API gap review](docs/research/ota-api-gap-review.md). New [super-admin user management and overview](docs/api/admin-users.md) are local-only; supplier/payment work remains contract-gated.
