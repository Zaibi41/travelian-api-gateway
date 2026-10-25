# Local agency database migrations

This increment creates schema only. It does not seed real identities, activate agencies, alter Cognito, or enable booking. No database connection occurs at API startup. Do not apply to production as part of this procedure.

## Apply locally

From the gateway repository, install dependencies:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Defaults target `127.0.0.1:5432`, database `travelian`, user `travelian_local`. If yours differ, edit `DB_HOST`, `DB_PORT`, `DB_NAME`, and `DB_USER` privately in your ignored `.env`. These settings are separate from supplier credentials. Leave `DB_PASSWORD` absent or empty to use the hidden terminal prompt; do not put passwords in shell commands or chat. Optional stored passwords use the ignored `.env` and must remain private.

Review generated SQL without connecting:

```bash
.venv/bin/python -m alembic upgrade head --sql
```

Then apply to your **local** database only:

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

Each online command prompts for the database password if it is not configured. Expected revision after all current migrations: `0003_agency_review`. Refresh DBeaver's `travelian → Schemas → public → Tables` to see `agencies`, `agency_cognito_assignments`, and `alembic_version`.

Read-only confirmation in DBeaver:

```sql
SELECT version_num FROM alembic_version;
SELECT id, name, status FROM agencies;
SELECT agency_id, issuer, subject FROM agency_cognito_assignments;
```

All business tables should initially be empty. Do not insert guessed Cognito subjects, real user details in test fixtures, or supplier agent IDs as agency IDs.

## Schema guarantees and limits

- Agency IDs follow the gateway's ASCII ID format; names must be nonblank. New agencies start `suspended`; only `active` and `suspended` are accepted.
- An assignment's agency ID is its primary key: at most one account per agency.
- `(issuer, subject)` is unique: at most one agency per Cognito account.
- Foreign keys prohibit orphan assignments and deletion of agencies with assignments.
- Timestamps use timezone-aware PostgreSQL types and insertion defaults. Future administrative writes must explicitly update `updated_at`; there is no automatic update trigger.
- The schema permits unassigned agencies. The booking guard now looks up the verified `(issuer, subject)` on each entitled request and requires an active agency matching the signed agency claim. Administrative provisioning, audit history, reassignment policy and booking ownership/attempt storage remain pending. Successful lookup still ends in 503; supplier dispatch stays blocked.

There is no public provisioning endpoint. Use separate restricted runtime and migration roles in production; the current local owner account is for development. Run migrations once through an approved migration process, not per API worker. A downgrade drops both business tables and their data; do not run it on populated databases without a reviewed recovery plan.

## Tests

Offline SQL generation and secret redaction run with the normal test suite. PostgreSQL constraint and upgrade/downgrade tests require `TRAVELIAN_TEST_DATABASE_URL` pointing to a disposable database where the test role may create schemas. They use a random schema inside a rolled-back transaction. Never supply a production URL. Without the explicit test setting, the PostgreSQL test skips rather than using the local application's database.

Migration framework: [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html). Connection construction uses [SQLAlchemy URL objects](https://docs.sqlalchemy.org/en/20/core/engines.html), preserving special password characters without string concatenation.

## Runtime assignment lookup

Runtime requires a privately configured `DB_PASSWORD`; it never prompts or uses an implicit passwordless local identity. Missing credentials or a database failure returns a generic 503. Unknown, suspended, or mismatched agency assignments return 403. Database work runs outside the event loop, with three-second connection/statement limits and read-only transactions. Connections are not pooled in this initial increment, and no identity decisions are cached. Review pooling, capacity limits and transaction-time checks before live dispatch; an assignment can change after a lookup. Inventory/search do not open database connections.

Agency self-registration is now available locally through the [application APIs](agency-applications.md). Migration `0002_agency_applications` adds pending applications. No agency is activated automatically.

Agency profile and review operations are described in [agency management](agencies.md); review changes include transactional history. Documents are optional.
