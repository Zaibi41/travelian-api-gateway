# Agency business contacts

Agency registration now requires `contact_name`, `contact_email` and `contact_phone` alongside `agency_name`. This is a request-contract change for both `POST /v1/agencies` and the existing `/v1/agencies/applications` alias. Name-only registration requests now return 422. Documents remain optional and are unrelated to these fields.

```json
{
  "agency_name": "Example Travels",
  "contact_name": "Contact Person",
  "contact_email": "office@example.com",
  "contact_phone": "+923001234567"
}
```

Names are nonblank, at most 200 characters and contain no control characters. Unicode names are supported. Contact email must be a plain syntactically valid address, at most 254 characters; its domain is normalized by the email validator. Validation does not perform DNS/mailbox verification. Phone must use an international `+` followed by 2–15 ASCII digits, starting with a nonzero digit, without spaces or punctuation. This checks format, not ownership or reachability. Never guess a country code for a supplied local number.

These are **unverified business contacts**, independent of Cognito login email, verified attributes and user identity. They do not grant permissions, change token claims or establish verified email/phone ownership. No contact uniqueness is imposed: multiple agencies may share a contact.

## Migration and existing agencies

Install updated requirements, then run explicitly against your local database:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

Expected revision: `0005_agency_contacts`. No migration is run during API startup. Existing agencies and relationships are retained with null contact fields; no fake data or Cognito-derived values are inserted. Approved legacy agencies are not suspended automatically. Deploy migrations before code that selects these columns. Downgrading this migration removes stored contacts and requires a reviewed recovery plan on populated databases.

## Profile updates and responses

Own/admin agency detail, application status, and authorized admin list responses include the three contact fields. Access remains restricted to the owning account or administrators with the existing read permission. They are not included in `/auth/me`, public inventory/search, or integration identity responses.

Both own/admin profile PATCH endpoints now accept any nonempty subset of agency name and contact fields, with mandatory `expected_revision`:

```json
{
  "contact_name": "Updated Person",
  "contact_email": "updated@example.com",
  "contact_phone": "+442079460123",
  "expected_revision": 4
}
```

Omitted fields retain their values. Explicit nulls and empty patches are rejected; no contact deletion operation is introduced. Updates preserve revision checks, transactions and audit events. Audit events record the action, not a copy of the contact values. Profile changes do not alter assignment, application status, agency status or Cognito state.

Applicants can update profiles only while pending review or information requested. Approved/rejected profiles remain administrator-editable under the existing policy; use the admin PATCH endpoint to populate an already approved test agency. After a successful update use the returned revision for future actions.

## Tests

Tests use synthetic contacts and disposable PostgreSQL. Coverage includes required and invalid fields, Unicode names, email normalization, phone formatting, nullable legacy-record migration, partial updates preserving omitted values and status, approved-profile restrictions, cross-account access, revision/audit behavior and transactional rollback. No real email/SMS delivery, account changes or supplier calls occur.
