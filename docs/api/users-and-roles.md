# Local users and roles

PostgreSQL is now the authority for human roles. Cognito verifies signup/login identity and issues tokens; no passwords or client secrets are stored in `users`. Machine integrations remain separate.

| role_id | role | Meaning |
|---|---|---|
| 1 | customer | Default verified account, including pending applicants |
| 2 | agency_owner | Agency application approved |
| 3 | super_admin | Explicitly assigned administrator |

`users` has a canonical UUID ID, unique `(cognito_issuer, cognito_sub)`, `role_id`, active flag and timestamps. `agency_cognito_assignments.user_id` is unique and references the same issuer/subject identity. Existing mappings remain unchanged. Roles are not writable through registration or profile APIs. Names are used in permission code instead of scattered numeric comparisons.

## Migration and bootstrap

This is an intentional authorization change: Cognito group membership no longer grants gateway admin permission. The old `AGENCY_ADMIN_*_GROUPS` environment settings no longer control these admin routes. In the initial three-role model, super_admin has agency read/write/review permissions; no implicit grants to customers or agency owners exist.

1. Install requirements and run `alembic upgrade head` against the approved **local** database. Expected revision: `0006_users_roles`.
2. Log in with the intended administrator's existing Cognito account and call `/v1/auth/me`. A new observed verified account gets customer role only. Save its returned `user_id`.
3. A trusted local operator explicitly grants the role:

```bash
.venv/bin/python -m app.cli.set_user_role \
  --user-id YOUR_LOCAL_USER_UUID \
  --role super_admin \
  --reason 'Initial local administrator provisioning'
```

The command changes the configured database only and writes an audit event. It does not update Cognito. It is an operator tool, not a public API: restrict database access. Never execute it on production without approval. It only accepts existing local profiles, and cannot assign agency_owner to bypass agency approval. Review who receives access before running it. There is no automatic administrator seeded from an email, a token group or an agency application.

Existing agency assignments are backfilled with UUID user profiles. Already approved applicants receive agency_owner; other applicants receive customer. No super_admin is inferred. Suspension preserves the agency_owner role but agency operations still require active agency status. Local account deactivation blocks supported human identity/agency/admin flows. Signup synchronization and full Cognito backfill remain separate work.

## Current identity and lifecycle

`GET /v1/auth/me` retains subject, issuer and client_id and adds user_id, role_id, role and agency/application status. It now requires a working migrated database; outage returns 503 rather than guessing a role. No raw groups/tokens are returned.

- Registration creates/links the customer profile and agency application atomically.
- Approval promotes customer to agency_owner in the same transaction as review/audit; existing super_admin roles are never overwritten by approval.
- Suspension/reactivation do not change the role; agency status determines availability.
- Super-admin self-review remains denied. Admins cannot use their role as an agency booking permission.
- Booking gates use local role and active approved assignment for people, while machine checks remain registry/scope based. All booking dispatch remains unconditionally blocked pending ownership/idempotency.

## Reporting

```sql
SELECT u.id, u.cognito_sub, r.name AS role
FROM users u JOIN roles r ON r.id=u.role_id;

SELECT r.name AS role, count(u.id) AS user_count
FROM roles r LEFT JOIN users u ON u.role_id=r.id
GROUP BY r.id, r.name ORDER BY r.id;
```

These counts include local profiles/backfilled assignments, not every registered Cognito account. Profiles are created when supported verified API flows observe the user; signup-event synchronization and reconciliation are needed for complete registration reporting. Pending applicants remain customers and can be counted separately by joining application records. No email, phone or password is inferred from unverified token contents.

Production still requires audited operational provisioning, restricted database permissions, signup synchronization/reconciliation and complete booking safety. No AWS changes are performed by this implementation.
