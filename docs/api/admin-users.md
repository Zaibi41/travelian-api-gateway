# Super-admin users and overview

Use an active local `super_admin` token. Cognito group names alone do not authorize these endpoints. Apply `alembic upgrade head` locally; expected revision `0007_user_administration`. No migration is applied automatically.

| Request | Method and path |
|---|---|
| List Users | `GET /v1/admin/users?limit=20&offset=0&role_id=1&active=true` |
| Get User | `GET /v1/admin/users/{user_id}` |
| List Roles | `GET /v1/admin/roles` |
| Overview | `GET /v1/admin/dashboard/overview` |
| Change Role | `PATCH /v1/admin/users/{user_id}/role` |
| Change Status | `PATCH /v1/admin/users/{user_id}/status` |
| History | `GET /v1/admin/users/{user_id}/history?limit=20&offset=0` |

All paths follow `API_PREFIX`. User IDs are canonical UUIDs. Pagination defaults to 20, maximum 100; offset maximum 100000. Filters are optional. Roles: 1 customer, 2 agency_owner, 3 super_admin. Reads never return passwords, tokens or client secrets. User records include role, active flag, current revision, issuer/subject mapping, creation time and optional agency ID.

Change-role body:

```json
{"role_id": 3, "expected_revision": 1, "reason": "Approved operations administrator"}
```

Change-status body:

```json
{"active": false, "expected_revision": 2, "reason": "Access suspended by operator"}
```

Reasons are nonblank, at most 1000 characters and contain no control characters. Extra fields are rejected. Self-modification is denied; use another administrator or the reviewed operator recovery process. The last active super administrator is protected. Assigning agency_owner requires an already approved assignment and never creates one. Revision mismatch or no-op change returns 409; refresh before deciding whether to retry. Missing user returns 404, insufficient privileges 403 and database failures generic 503. Audit failure rolls back the change.

Overview counts only local profiles and stored agency/application records, labelled `user_coverage: local_profiles`. They do not include unseen Cognito accounts, revenues, bookings or balances. Complete signup statistics need registration event synchronization and backfill. Agency owner counts and active agency counts are different measures.

Role changes/deactivation are effective on subsequent local authorization checks even with an older signed token. They do not mutate Cognito, revoke its sessions or change machine integrations automatically. Existing public search routes remain public. All booking dispatch remains blocked. History shows API user-administration events; bootstrap and agency-approval role events remain in `user_role_events`.

The full [OTA feature gap review](../research/ota-api-gap-review.md) separates implemented APIs from proposed work and contract blockers.
