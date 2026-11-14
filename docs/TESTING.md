# Testing the gateway on a new machine

How to run travelian-api-gateway locally for testing the admin dashboard. Commands assume Linux or macOS with `bash`.

Never commit `.env`, a real password, a token, or any booking export. `.env` is git-ignored; keep it that way.

## 1. Prerequisites

- Python 3.12 or newer.
- PostgreSQL 14 or newer, running locally.
- Git access to this repo, on branch `feature/admin-dashboard-home`.

## 2. Python environment

From the gateway folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Settings (`.env`)

```bash
cp .env.example .env
```

Fill `.env` in privately. The gateway reads it from the gateway folder (not from where you start it), and real environment variables override it.

Leave anything you don't need empty: a feature whose settings are missing reports itself as "not configured" instead of failing.

### Needed to sign in and see the admin

| Variable | What it is |
| --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Your local PostgreSQL database (section 4). If `DB_PASSWORD` is empty, migrations prompt for it. |
| `COGNITO_ISSUER` | The Cognito user pool's issuer URL (`https://cognito-idp.<region>.amazonaws.com/<pool id>`). It must be the same pool the frontend signs in with. |
| `COGNITO_APP_CLIENT_ID` | The Cognito app client ID the frontend uses. |
| `CORS_ALLOWED_ORIGINS` | JSON list of exact browser origins allowed to call the gateway, e.g. `["http://localhost:8080"]`. No wildcard, no trailing slash. |

### Optional

| Variable | What it is |
| --- | --- |
| `API_PREFIX`, `LOG_LEVEL` | Usually left as in `.env.example`. |
| `DASHBOARD_EXPIRING_HOLD_HOURS`, `DASHBOARD_TIMEZONE` | Dashboard "expiring holds" window (hours, 1–168) and the time zone for date filters. |
| `COGNITO_AGENCY_CLAIM`, `BOOKING_CREATE_GROUPS`, `BOOKING_DETAILS_GROUPS` | Agency booking policy. Keep empty until approved. |
| `AGENCY_ADMIN_*_GROUPS` | Legacy group settings, no longer used for admin access. |
| `INTEGRATION_*_SCOPE` | Machine-integration scopes. Keep empty until approved. |
| `AWS_REGION`, `SSM_PARAMETER_PREFIX` | Load supplier settings from AWS Parameter Store instead of `.env`. |
| `VENDOR_TIMEOUT_SECONDS`, `VENDOR_MAX_CONCURRENCY` | Supplier call limits. |
| `ENABLE_DEMO_VENDOR` | No longer used by flight search. Leave `false`. |

### Suppliers (optional; ask the owner for credentials)

| Variable | What it is |
| --- | --- |
| `DHILLU_SAHI_BASE_URL`, `DHILLU_SAHI_EMAIL`, `DHILLU_SAHI_PASSWORD`, `DHILLU_SAHI_TIMEOUT_SECONDS`, `DHILLU_SAHI_GROUP_SECTOR_ID` | Group Ticketing supplier. |
| `SKYPASS_ENDPOINT`, `SKYPASS_CLIENT_ID`, `SKYPASS_CLIENT_SECRET` | Second Group Ticketing supplier. |
| `GROUP_TICKETING_MARKUP_PERCENT`, `GROUP_TICKETING_MARKUP_FIXED_MINOR` | Group selling price = supplier price + percent + fixed amount (in paisa). Unset (or empty) counts as 0: fares sell at the listing price per adult. Use the approved markup. |
| `GROUP_HOLD_MINUTES`, `GROUP_HOLD_JOB_INTERVAL_SECONDS`, `GROUP_HOLD_RELEASE_ATTEMPTS` | Travelian's own group hold time (default 30 minutes), how often the expiry job runs (30 s), and how many times it retries a failed supplier release (5). |
| `GROUP_TICKETING_DEFAULT_CURRENCY` | Currency for group fares that arrive without one. |
| `SOUTHAIR_BASE_URL`, `SOUTHAIR_USERNAME`, `SOUTHAIR_PASSWORD`, `SOUTHAIR_TIMEOUT_SECONDS` | Southair flights (AEROST OTA API). |
| `FLIGHT_MARKUP_PERCENT`, `FLIGHT_MARKUP_FIXED_MINOR` | Flight selling price = supplier fare + percent + fixed amount (in paisa) per passenger. With both 0, no price is shown and nothing can be confirmed or held. |
| `SOUTHAIR_BOOKING_ENABLED` | Allows hold-only bookings (creates a real PNR at the supplier). Keep `false` unless running a supervised UAT test. |
| `SOUTHAIR_TICKETING_ENABLED` | Allows ticket issuance. Keep `false`. |

## 4. Database

Create a database and a user for it (names are examples; choose your own password):

```bash
sudo -u postgres createuser --pwprompt travelian_local
sudo -u postgres createdb -O travelian_local travelian
```

Put the same values in `DB_*`, then create the tables:

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic current        # should end in "(head)"
```

Run `alembic upgrade head` again whenever you pull new migrations. (Latest: `0019_payment_accounts`, Travelian's receiving accounts; `0018_booking_itinerary`, the flights of each new hold.)

Receiving accounts (the Payments tab's "Received into" choice) are managed in the portal under
Finance > Accounts & Ledgers (Super Admin; `/v1/admin/finance/payment-accounts`: list all, add,
deactivate), or on the server:

```bash
.venv/bin/python -m app.cli.payment_accounts add --method bank_transfer --label "Meezan Bank, Travelian current account"
.venv/bin/python -m app.cli.payment_accounts list
.venv/bin/python -m app.cli.payment_accounts deactivate --id <account id>
```

Methods: `bank_transfer`, `jazzcash`, `1link`. Use a name staff recognise, not a full account number. A payment can only be recorded into an active account of its method. If a migration is missing, the admin pages fail; the dashboard shows "Agency service unavailable."

### Sample bookings are not in git

The 18 sample bookings on the original machine came from a private CSV export containing passenger data, so they are deliberately not committed. A fresh database has **no bookings**: the dashboard and Bookings show zeros or empty tables, and no "Sample data" label.

If you are given an approved export privately, import it with `.venv/bin/python -m app.cli.import_bookings_csv PATH.csv` (dry run), then `--apply`. Remove the rows with `--remove`. Never commit the CSV.

## 5. Start the gateway

```bash
cd travelian-api-gateway
.venv/bin/uvicorn app.main:app --reload
curl -s localhost:8000/v1/health        # {"status":"ok"}
```

## 6. Give yourself access

Sign in once through the frontend (see the frontend's `docs/TESTING.md`). That creates your local profile with the default role. Then ask the owner, or use your database access, to find your user ID in the `users` table and make yourself super admin:

```bash
.venv/bin/python -m app.cli.set_user_role --user-id <your user id> --role super_admin --reason "local testing"
```

To test the Staff role and its two permissions (D23):

```bash
.venv/bin/python -m app.cli.set_user_role --user-id <id> --role staff --reason "testing"
.venv/bin/python -m app.cli.set_user_permission --user-id <id> --permission see_supplier_names --reason "testing"
```

## 7. Run the tests

```bash
.venv/bin/python -m pytest -q
```

This runs every test that needs no database. Tests that need one are skipped unless you point them at a **disposable** database. Never use your real one; the tests create and drop schemas.

```bash
sudo -u postgres createdb -O travelian_local travelian_test
export TRAVELIAN_TEST_DATABASE_URL='postgresql+psycopg://travelian_local:<password>@localhost:5432/travelian_test'
.venv/bin/python -m pytest -q
unset TRAVELIAN_TEST_DATABASE_URL
```

Expected on 10 November 2026: 674 passed, 107 skipped without a test database; 781 passed with one. Unit tests never reach the database your `.env` points at (tests/conftest.py); tests that need one use the disposable test database. Holds need no identity-storage settings (docs/pii-storage.md).

Supplier tests use a mocked supplier and never call a real one.
