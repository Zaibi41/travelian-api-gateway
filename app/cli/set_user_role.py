"""Assign a role to an existing verified local profile; operator access required."""

import argparse
from uuid import UUID

from sqlalchemy import text

from app.db.agencies import transaction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--role", choices=["customer", "super_admin", "staff"], required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    if not args.reason.strip() or len(args.reason) > 1000:
        parser.error("A nonblank reason of at most 1000 characters is required.")
    with transaction() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(73411921)"))
        row = (
            connection.execute(
                text("SELECT id,role_id FROM users WHERE id=:id FOR UPDATE"),
                {"id": str(args.user_id)},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            parser.error("User not found. First call /v1/auth/me.")
        role_id = connection.scalar(
            text("SELECT id FROM roles WHERE name=:name"), {"name": args.role}
        )
        connection.execute(
            text(
                "UPDATE users SET role_id=:role,revision=revision+1,updated_at=now() WHERE id=:id"
            ),
            {"id": str(args.user_id), "role": role_id},
        )
        connection.execute(
            text(
                "INSERT INTO user_role_events (id,user_id,previous_role_id,new_role_id,reason) VALUES (gen_random_uuid()::text,:id,:previous,:role,:reason)"
            ),
            {
                "id": str(args.user_id),
                "previous": row["role_id"],
                "role": role_id,
                "reason": args.reason.strip(),
            },
        )
    print("Local role updated. Cognito was not modified.")


if __name__ == "__main__":
    main()
