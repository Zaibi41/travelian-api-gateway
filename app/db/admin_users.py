"""Human super-admin operations; no Cognito or financial side effects."""

from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text

from app.db.agencies import transaction
from app.db.users import ensure_user
from app.schemas.admin_users import UserRecord

_USER = """SELECT u.id AS user_id,u.cognito_issuer,u.cognito_sub,u.role_id,r.name AS role,
    u.active,u.revision,u.created_at,x.agency_id
    FROM users u JOIN roles r ON r.id=u.role_id
    LEFT JOIN agency_cognito_assignments x ON x.user_id=u.id"""


def detail(connection, user_id):
    row = (
        connection.execute(text(_USER + " WHERE u.id=:id"), {"id": str(user_id)})
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "User not found.")
    return UserRecord.model_validate(dict(row))


def get_user(user_id):
    with transaction() as connection:
        return detail(connection, user_id)


def list_users(limit, offset, role_id=None, active=None):
    conditions, params = [], {"limit": limit, "offset": offset}
    if role_id is not None:
        conditions.append("u.role_id=:role")
        params["role"] = role_id
    if active is not None:
        conditions.append("u.active=:active")
        params["active"] = active
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with transaction() as connection:
        return [
            UserRecord.model_validate(dict(row))
            for row in connection.execute(
                text(
                    _USER
                    + where
                    + " ORDER BY u.created_at,u.id LIMIT :limit OFFSET :offset"
                ),
                params,
            ).mappings()
        ]


def roles():
    with transaction() as connection:
        return (
            connection.execute(text("SELECT id,name FROM roles ORDER BY id"))
            .mappings()
            .all()
        )


def overview():
    # One SQL statement gives all counts from the same database snapshot.
    with transaction() as connection:
        return dict(
            connection.execute(
                text("""SELECT
            count(*) AS total_users,count(*) FILTER (WHERE active) AS active_users,
            count(*) FILTER (WHERE role_id=1) AS customers,
            count(*) FILTER (WHERE role_id=2) AS agency_owners,
            count(*) FILTER (WHERE role_id=3) AS super_admins,
            (SELECT count(*) FROM agencies) AS total_agencies,
            (SELECT count(*) FROM agencies WHERE status='active') AS active_agencies,
            (SELECT count(*) FROM agencies WHERE status='suspended') AS suspended_agencies,
            (SELECT count(*) FROM agency_applications WHERE status='pending_review') AS pending_applications,
            (SELECT count(*) FROM agency_applications WHERE status='rejected') AS rejected_applications
            FROM users""")
            )
            .mappings()
            .one()
        )


def change_user(identity, user_id, body, action):
    with transaction() as connection:
        # Serialize role/status administration and recheck the actor inside the
        # transaction so concurrent demotion cannot use a stale permission.
        connection.execute(text("SELECT pg_advisory_xact_lock(73411921)"))
        actor = ensure_user(connection, identity)
        if actor.role != "super_admin":
            raise HTTPException(403, "User administration access denied.")
        if actor.id == str(user_id):
            raise HTTPException(403, "Self-modification is not permitted.")
        target = (
            connection.execute(
                text(
                    "SELECT id,role_id,active,revision FROM users WHERE id=:id FOR UPDATE"
                ),
                {"id": str(user_id)},
            )
            .mappings()
            .one_or_none()
        )
        if target is None:
            raise HTTPException(404, "User not found.")
        if target["revision"] != body.expected_revision:
            raise HTTPException(409, "User changed; refresh before retrying.")
        role, active = target["role_id"], target["active"]
        if action == "role_changed":
            role = body.role_id
            if role == 2 and not connection.scalar(
                text(
                    "SELECT 1 FROM agency_cognito_assignments x JOIN agency_applications p ON p.agency_id=x.agency_id WHERE x.user_id=:id AND p.status='approved'"
                ),
                {"id": str(user_id)},
            ):
                raise HTTPException(
                    409, "Agency owner role requires an approved agency assignment."
                )
        else:
            active = body.active
        if role == target["role_id"] and active == target["active"]:
            raise HTTPException(409, "User already has the requested state.")
        if (
            target["role_id"] == 3
            and target["active"]
            and (role != 3 or not active)
            and connection.scalar(
                text("SELECT count(*) FROM users WHERE role_id=3 AND active")
            )
            <= 1
        ):
            raise HTTPException(
                409, "The last active super administrator must be retained."
            )
        revision = target["revision"] + 1
        params = {
            "id": str(user_id),
            "role": role,
            "active": active,
            "revision": revision,
        }
        connection.execute(
            text(
                "UPDATE users SET role_id=:role,active=:active,revision=:revision,updated_at=now() WHERE id=:id"
            ),
            params,
        )
        connection.execute(
            text(
                "INSERT INTO user_admin_events (id,user_id,actor_id,action,reason,previous_role_id,new_role_id,previous_active,new_active,revision) VALUES (:event,:id,:actor,:action,:reason,:previous_role,:role,:previous_active,:active,:revision)"
            ),
            {
                **params,
                "event": str(uuid4()),
                "actor": actor.id,
                "action": action,
                "reason": body.reason,
                "previous_role": target["role_id"],
                "previous_active": target["active"],
            },
        )
        if role != target["role_id"]:
            connection.execute(
                text(
                    "INSERT INTO user_role_events (id,user_id,previous_role_id,new_role_id,reason) VALUES (:event,:id,:previous,:role,:reason)"
                ),
                {
                    "event": str(uuid4()),
                    "id": str(user_id),
                    "previous": target["role_id"],
                    "role": role,
                    "reason": body.reason,
                },
            )
        return detail(connection, user_id)


def history(user_id, limit, offset):
    with transaction() as connection:
        detail(connection, user_id)
        return (
            connection.execute(
                text(
                    "SELECT id,actor_id,action,reason,previous_role_id,new_role_id,previous_active,new_active,revision,created_at FROM user_admin_events WHERE user_id=:id ORDER BY revision DESC LIMIT :limit OFFSET :offset"
                ),
                {"id": str(user_id), "limit": limit, "offset": offset},
            )
            .mappings()
            .all()
        )
