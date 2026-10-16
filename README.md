# Travelian API

A starter API gateway for Travelian's OTA. Product areas are separated into Live Ticketing, Group Ticketing, and Hotels so each can have independent supplier adapters and workflows behind one API host.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive API docs. The `/v1/health` endpoint reports service health.

## Product APIs

All product APIs are versioned under `/v1` and grouped by domain:

| Domain | Endpoint | Purpose | Current state |
| --- | --- | --- | --- |
| Live Ticketing | `POST /v1/live-ticketing/flights/search` | Search individual flight fares | Demo adapter; concurrent fan-out with per-vendor outcomes |
| Group Ticketing | `POST /v1/group-ticketing/flight-quotes` | Submit a group flight quote request (10+ passengers) | Accepts request with `202`; sourcing workflow pending |
| Group Ticketing | `GET /v1/group-ticketing/inventory` | Fetch all group inventory from configured suppliers | Concurrent Dhillu & Sahi and Skypass fan-out; combined Travelian `data` response |
| Group Ticketing | `POST /v1/group-ticketing/search` | Filter one-way inventory by route, date, and adults | Defaults to one adult; same combined response |
| Group Ticketing | `POST /v1/group-ticketing/check-availability` | Check PNR seats and current price | Dhillu & Sahi authenticated availability check |
| Group Ticketing | `POST /v1/group-ticketing/bookings` | Book passengers against a group PNR | Dhillu & Sahi authenticated booking; no automatic retries |
| Group Ticketing | `POST /v1/group-ticketing/booking-details` | Fetch booking, passenger, payment, and group details | Dhillu & Sahi authenticated lookup |
| Hotels | `POST /v1/hotels/search` | Search hotel inventory by destination and stay dates | Contract in place; supplier adapters pending |

Each area has its own route module and vendor package. Keep one supplier per module/package so credentials, mapping, logs, and debugging stay isolated. Supplier outcomes are recorded internally. Group inventory and search return a single Travelian response containing combined group records, without gateway-added per-vendor status fields.

## Group Ticketing inventory and search

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

Dhillu & Sahi is a **Group Ticketing vendor**. Its inventory, availability, booking, and booking-details operations belong to Group Ticketing. The supplied integration details define `POST https://api.dhilluandsahi.com/api/login` with JSON `email` and `password`, returning a token under `success.token`. Group inventory uses `POST /api/pnr-respect-to-mysector-user` with a Bearer token and optional JSON `my_sector_id`. The isolated client and response normalizer live in `app/vendors/group_ticketing/dhillu_sahi/`. Set `DHILLU_SAHI_EMAIL`, `DHILLU_SAHI_PASSWORD`, and the supplier's account sector value as `DHILLU_SAHI_GROUP_SECTOR_ID` in a deployment secret manager or local ignored `.env`; do not commit credentials. The gateway fetches inventory at `GET /v1/group-ticketing/inventory`, or filters it at `POST /v1/group-ticketing/search`. The supplier returned HTTP 404 when inventory was requested without `my_sector_id`; the documented example `9` previously returned groups, but it has not been confirmed as the account-wide inventory selector. Configure the confirmed value before using inventory/search. When no configured supplier can provide inventory, these routes return a generic HTTP 503. The supplier is not called for live flight searches because no live-flight contract was provided.

For local development, copy `.env.example` to `.env` in the project root and set `DHILLU_SAHI_EMAIL` and `DHILLU_SAHI_PASSWORD`. `.env` is git-ignored; do not paste credentials into source files or commit them. For deployment, provide the same names through your platform's secret/environment configuration. The app reads them through `pydantic-settings` when it starts. Exercise integrations through the actual versioned Group Ticketing routes in the API docs or with HTTP requests to the running application.

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

## Production deployment

Put the service behind HTTPS and a managed DNS name such as `api.travelian.pk`. Before production traffic, add supplier-specific auth/retries and rate limits, request tracing, metrics, and a durable booking/offer lifecycle. Flight search fan-out is async within a request; longer workflows such as bookings should use persisted state and background jobs rather than keeping an HTTP request open.
