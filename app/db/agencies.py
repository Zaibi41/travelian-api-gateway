"""Agency operations: row-locked transitions with transactional audit records."""

from contextlib import contextmanager

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.agency_applications import application_engine
from app.schemas.agency_applications import AgencyApplicationResponse

_SELECT = """
SELECT a.id AS agency_id, a.name AS agency_name, a.status AS agency_status,
       p.status AS application_status, p.submitted_at, a.revision, a.contact_name, a.contact_email, a.contact_phone
FROM agencies a JOIN agency_applications p ON p.agency_id = a.id
"""


@contextmanager
def transaction():
    try:
        with application_engine().begin() as connection:
            yield connection
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(503, "Agency service unavailable.") from None


def resolve(connection, user, agency_id=None, lock=False):
    from app.db.users import ensure_user

    ensure_user(connection, user)
    params = {}
    if agency_id is None:
        where = "a.id = (SELECT agency_id FROM agency_cognito_assignments WHERE issuer=:issuer AND subject=:subject)"
        params = {"issuer": user.issuer, "subject": user.subject}
    else:
        where = "a.id=:id"
        params = {"id": agency_id}
    row = (
        connection.execute(
            text(_SELECT + " WHERE " + where + (" FOR UPDATE OF a, p" if lock else "")),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Agency not found.")
    return AgencyApplicationResponse.model_validate(dict(row))


def detail(user, agency_id=None):
    with transaction() as connection:
        return resolve(connection, user, agency_id)


def listing(limit, offset, application_status=None, agency_status=None):
    conditions, params = [], {"limit": limit, "offset": offset}
    if application_status:
        conditions.append("p.status=:application_status")
        params["application_status"] = application_status
    if agency_status:
        conditions.append("a.status=:agency_status")
        params["agency_status"] = agency_status
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with transaction() as connection:
        rows = connection.execute(
            text(
                _SELECT
                + where
                + " ORDER BY p.submitted_at, a.id LIMIT :limit OFFSET :offset"
            ),
            params,
        ).mappings()
        return [AgencyApplicationResponse.model_validate(dict(row)) for row in rows]


def change(user, body, action, agency_id=None):
    with transaction() as connection:
        if action == "approved":
            connection.execute(text("SELECT pg_advisory_xact_lock(73411921)"))
        record = resolve(connection, user, agency_id, lock=True)
        if record.revision != body.expected_revision:
            raise HTTPException(409, "Agency changed; refresh before retrying.")
        name, status, application = (
            record.agency_name,
            record.agency_status,
            record.application_status,
        )
        reason = getattr(body, "reason", None)
        if action == "profile_updated":
            if agency_id is None and application not in (
                "pending_review",
                "more_information_required",
            ):
                raise HTTPException(
                    409, "Agency profile cannot be edited in this state."
                )
            name = body.agency_name if body.agency_name is not None else name
        elif action == "resubmitted" and application == "more_information_required":
            application = "pending_review"
        elif action in ("approved", "rejected", "information_requested"):
            # Administrative reviewers cannot decide their own application.
            owns = connection.scalar(
                text(
                    "SELECT 1 FROM agency_cognito_assignments WHERE agency_id=:id AND issuer=:issuer AND subject=:subject"
                ),
                {
                    "id": record.agency_id,
                    "issuer": user.issuer,
                    "subject": user.subject,
                },
            )
            if owns:
                raise HTTPException(403, "Self-review is not permitted.")
            if application != "pending_review":
                raise HTTPException(409, "Invalid agency review transition.")
            application = {
                "approved": "approved",
                "rejected": "rejected",
                "information_requested": "more_information_required",
            }[action]
            status = "active" if action == "approved" else "suspended"
        elif action == "suspended" and application == "approved" and status == "active":
            status = "suspended"
        elif (
            action == "reactivated"
            and application == "approved"
            and status == "suspended"
        ):
            status = "active"
        else:
            raise HTTPException(409, "Invalid agency transition.")
        if action == "approved":
            connection.execute(
                text(
                    "INSERT INTO user_role_events (id,user_id,previous_role_id,new_role_id,reason) SELECT gen_random_uuid()::text,u.id,1,2,'Agency application approved; see agency review history' FROM users u JOIN agency_cognito_assignments x ON x.user_id=u.id WHERE u.role_id=1 AND x.agency_id=:id"
                ),
                {"id": record.agency_id},
            )
            connection.execute(
                text(
                    "UPDATE users SET role_id=2,revision=revision+1,updated_at=now() WHERE role_id=1 AND id=(SELECT user_id FROM agency_cognito_assignments WHERE agency_id=:id)"
                ),
                {"id": record.agency_id},
            )
        revision = record.revision + 1
        params = {
            "id": record.agency_id,
            "name": name,
            "status": status,
            "application": application,
            "revision": revision,
        }
        contacts = {
            key: getattr(record, key)
            for key in ("contact_name", "contact_email", "contact_phone")
        }
        if action == "profile_updated":
            contacts.update(
                body.model_dump(
                    exclude_unset=True, exclude={"agency_name", "expected_revision"}
                )
            )
        params.update(contacts)
        connection.execute(
            text(
                "UPDATE agencies SET contact_name=:contact_name, contact_email=:contact_email, contact_phone=:contact_phone, name=:name, status=:status, revision=:revision, updated_at=now() WHERE id=:id"
            ),
            params,
        )
        connection.execute(
            text(
                "UPDATE agency_applications SET status=:application, updated_at=now() WHERE agency_id=:id"
            ),
            params,
        )
        connection.execute(
            text(
                "INSERT INTO agency_events (agency_id, action, reason, actor_issuer, actor_subject, revision) VALUES (:id,:action,:reason,:issuer,:subject,:revision)"
            ),
            {
                "id": record.agency_id,
                "action": action,
                "reason": reason,
                "issuer": user.issuer,
                "subject": user.subject,
                "revision": revision,
            },
        )
        return resolve(connection, user, record.agency_id)


def history(user, agency_id, limit, offset):
    with transaction() as connection:
        record = resolve(connection, user, agency_id)
        return (
            connection.execute(
                text(
                    "SELECT action, reason, revision, created_at FROM agency_events WHERE agency_id=:id ORDER BY revision DESC LIMIT :limit OFFSET :offset"
                ),
                {"id": record.agency_id, "limit": limit, "offset": offset},
            )
            .mappings()
            .all()
        )
