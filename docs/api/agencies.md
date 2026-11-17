# Agency registration, profile and review APIs

Current authority: [PostgreSQL users and roles](users-and-roles.md). Migration `0006_users_roles` replaces human group-based admin/booking role checks; earlier group-policy notes below are historical. Explicit local administrator provisioning is required.

Local implementation only. No production settings or Cognito resources are changed. Documents are **optional**, not a condition of registration or approval. File upload/storage APIs are deferred until private storage and permissions are configured. No document infrastructure is needed to use this workflow.

## Applicant endpoints

All endpoints require an existing verified Cognito user access token. Signup/login remain Cognito responsibilities. No role or agency claim is required to apply.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/agencies` | Register own agency; generates ID; starts suspended/pending review |
| GET | `/v1/agencies/me` | Read own application and agency |
| PATCH | `/v1/agencies/me` | Update agency name while pending or information requested |
| POST | `/v1/agencies/me/resubmit` | Return information-requested application to pending review |
| GET | `/v1/agencies/me/history` | Read own paginated review history |

Registration body: `{"agency_name":"Your agency","contact_name":"Contact Person","contact_email":"office@example.com","contact_phone":"+923001234567"}`. Existing `/v1/agencies/applications` and `/v1/agencies/applications/me` remain supported. Responses now also include `revision`.

Profile update body: `{"agency_name":"Updated agency", "expected_revision":1}`. Registration now requires contact name, email and international phone as well; see [business contacts](agency-contacts.md). Legal and document fields remain deferred. Unknown fields are rejected; clients cannot assign account identity, agency ID, status or roles.

Resubmission and administrative action bodies require `expected_revision` and a nonblank `reason` of at most 1000 characters, without control characters. For resubmission, put the requested clarification in the reason. Application rejection is terminal in this increment; no automatic reapplication or ownership reassignment is provided.

## Admin endpoints

| Method | Path | Required policy |
|---|---|---|
| GET | `/v1/admin/agencies` | Read |
| GET | `/v1/admin/agencies/{agency_id}` | Read |
| PATCH | `/v1/admin/agencies/{agency_id}` | Write; agency name and contacts |
| GET | `/v1/admin/agencies/{agency_id}/history` | Read |
| POST | `/v1/admin/agencies/{agency_id}/request-information` | Review |
| POST | `/v1/admin/agencies/{agency_id}/approve` | Review |
| POST | `/v1/admin/agencies/{agency_id}/reject` | Review |
| POST | `/v1/admin/agencies/{agency_id}/suspend` | Review |
| POST | `/v1/admin/agencies/{agency_id}/reactivate` | Review |

Policy settings are `AGENCY_ADMIN_READ_GROUPS`, `AGENCY_ADMIN_WRITE_GROUPS` and `AGENCY_ADMIN_REVIEW_GROUPS`, each a JSON array of explicitly approved Cognito group names. Empty/invalid configuration returns 503; a valid policy with no matching signed group returns 403. No implicit permission is given to `admin`, `super_admin`, agency owners or any other group. Set these values only after reviewing who belongs to each approved group. Read, write and review grants do not imply one another. These permissions do not allow editing mappings, groups or credentials.

List/history pagination: `limit` defaults to 20, maximum 100; `offset` defaults to zero, maximum 100000. Listing returns a JSON array ordered by submission time and agency ID. Optional filters: `application_status` (`pending_review`, `more_information_required`, `approved`, `rejected`) and `agency_status` (`active`, `suspended`). These endpoints represent registered agencies with applications; legacy agency rows without applications are not exposed by this flow.

## Review and concurrency

- A pending application can be approved, rejected, or moved to information requested.
- Its owner can update the name and resubmit requested information. Rejected and approved profiles cannot be edited by the applicant in this increment.
- Approval marks the application approved and the agency active **in the database only**. It does not provision Cognito membership/claims, and booking dispatch remains closed.
- Approved active agencies can be suspended and approved suspended agencies can be reactivated. Pending/rejected agencies cannot be activated using these actions.
- A reviewer cannot approve, reject or request information on their own application.
- Changes lock the agency/application rows and compare `expected_revision`. Stale revisions, repeated transitions and invalid state changes return 409; refresh before deciding whether to submit a new action. There are no automatic retries.
- Every new submission and successful change records an event with verified actor identity, reason, timestamp and resulting revision. Changes and audit insert commit together; audit failure rolls back the change. Existing rows predating this migration have no fabricated history.
- API history excludes actor identity and never returns passwords or tokens. Review reasons are visible to the applicant: do not enter secrets or internal confidential notes.

There is no hard-delete endpoint: deletion would discard identity/review history. Use rejection or suspension. Authentication is checked before database access; unknown own records return 404. Database failures return generic 503 without raw SQL or connection errors.

## Local migration and testing

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

Expected head: `0003_agency_review`. Do this locally only; no migrations run during API startup. Keep database credentials private as described in [database setup](agency-database.md). Schema adds agency revision and `agency_events`.

Tests exercise real disposable PostgreSQL constraints with synthetic identities, mocked authentication and no supplier transport. They verify lifecycle, role separation, cross-account isolation, self-review prevention, stale revisions, field restrictions, pagination, audit history and rollback on audit failure. Existing tests cover JWT verification and default-deny booking behavior.

Production readiness still requires approved group assignment, restricted database roles, connection pooling/capacity and abuse controls, token revocation policy, optional document design if wanted, Cognito synchronization and complete booking ownership/idempotency. This local increment is not approval to enable public agency onboarding or live booking.

Contact-field migration and partial-update rules: [agency-contacts.md](agency-contacts.md). Current migration head is `0005_agency_contacts`.
