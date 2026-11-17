# Agency applications: first stage

Implemented locally; not deployed. Application submission never activates an agency or grants Cognito groups, supplier-agent credentials or booking permission. The agency name is applicant supplied; generated IDs are opaque UUID-based identifiers.

## Local preparation

Install requirements and apply migrations only to your local database:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

Expected head: `0003_agency_review`. Refresh DBeaver to see `agency_applications`. Migration commands prompt for the database password when absent. The running API instead requires `DB_PASSWORD` privately configured in the ignored `.env`; it never prompts. Do not share it or commit it. Existing Cognito issuer/client settings must be configured locally to verify access tokens; agency-group membership and the custom agency claim are not prerequisites for applying.

## Submit

```http
POST /v1/agencies/applications
Authorization: Bearer <Cognito access token>
Content-Type: application/json

{"agency_name": "Your agency name", "contact_name": "Contact Person", "contact_email": "office@example.com", "contact_phone": "+923001234567"}
```

The prefix follows `API_PREFIX`. Name is a nonblank string of at most 200 characters; control characters are rejected and surrounding whitespace is trimmed. Additional request fields are rejected, including identity, status and group fields. Contact name, email and international phone are required; see [business contacts](agency-contacts.md). Documents remain optional and uploads are deferred.

201 returns `agency_id`, `agency_name`, `application_status: pending_review`, `agency_status: suspended`, and `submitted_at`. The verified issuer and subject are never accepted from the request body. The agency, assignment and application are created together in one transaction. Unique account mapping prevents repeated and competing submissions from creating multiple agencies; transaction rollback prevents orphan rows. A duplicate returns 409, including an account already assigned administratively. After a lost response, use the status endpoint to reconcile before submitting again. There are no automatic submission retries.

## Applicant status

```http
GET /v1/agencies/applications/me
Authorization: Bearer <Cognito access token>
```

Returns only the authenticated account's application, scoped by both issuer and subject. There is no public list or lookup-by-agency-ID endpoint. No matching application returns 404, including an administratively assigned agency with no application record. Database/configuration failure returns generic 503. Authentication failure follows existing 401/503 behavior.

## Limits and next stage

The broader [agency management APIs](agencies.md) now provide profile updates and administrative review with separately configured group permissions. Existing application paths remain supported. Documents are optional and uploads are deferred. Booking dispatch remains blocked; approval alone does not change Cognito permissions. Rate limits, production pooling and restricted database grants remain pending.

Tests use synthetic identities, dependency-overridden authentication and an isolated PostgreSQL schema. They cover creation, own-status isolation, forged administrative fields, duplicate rollback, invalid names, missing authentication, storage outage and partial failure. Existing token-verification tests cover invalid bearer credentials. No real identities are seeded or supplier requests made.

New registrations generate canonical UUIDv4 agency IDs (hyphenated, without an `agency-` prefix). Existing prefixed IDs remain valid and unchanged. Database identifiers remain string columns for backward compatibility; this generation change requires no data migration.

Contact-field migration and partial-update rules: [agency-contacts.md](agency-contacts.md). Current migration head is `0005_agency_contacts`.
