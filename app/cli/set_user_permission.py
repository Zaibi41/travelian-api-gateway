"""Grant or revoke a field-visibility permission (spec v3.0 D23); operator access required.

Permissions: see_supplier_names, see_cost_and_margin, reveal_identity. They only take effect for
the staff role: super_admin already sees everything, and agency and customer
roles can never be granted them.

    .venv/bin/python -m app.cli.set_user_permission --user-id UUID --permission see_supplier_names --reason "..."
    .venv/bin/python -m app.cli.set_user_permission --role staff --permission see_cost_and_margin --reason "..."
    add --revoke to remove a grant
"""

import argparse
import sys
from uuid import UUID

from sqlalchemy import text

from app.core.field_visibility import PERMISSIONS


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--user-id", type=UUID)
    target.add_argument("--role", choices=["staff"], help="grant to every user with this role")
    parser.add_argument("--permission", choices=PERMISSIONS, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args(argv)
    if not args.reason.strip():
        parser.error("--reason must not be blank")

    from app.db.agency_applications import application_engine

    with application_engine().begin() as connection:
        if args.role:
            role_id = connection.scalar(text("SELECT id FROM roles WHERE name=:name"), {"name": args.role})
            if args.revoke:
                connection.execute(text("DELETE FROM role_permissions WHERE role_id=:r AND permission=:p"), {"r": role_id, "p": args.permission})
            else:
                connection.execute(text("INSERT INTO role_permissions (role_id, permission) VALUES (:r, :p) ON CONFLICT DO NOTHING"), {"r": role_id, "p": args.permission})
            print(f"{'Revoked' if args.revoke else 'Granted'} {args.permission} for role {args.role}.")
            return 0
        role = connection.scalar(text("SELECT r.name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=:id"), {"id": str(args.user_id)})
        if role is None:
            print("No such local user.", file=sys.stderr)
            return 1
        if role != "staff" and not args.revoke:
            print(f"Refused: permissions apply to staff only (user's role is {role}).", file=sys.stderr)
            return 1
        if args.revoke:
            connection.execute(text("DELETE FROM user_permissions WHERE user_id=:u AND permission=:p"), {"u": str(args.user_id), "p": args.permission})
        else:
            connection.execute(
                text("INSERT INTO user_permissions (user_id, permission, reason) VALUES (:u, :p, :reason) ON CONFLICT DO NOTHING"),
                {"u": str(args.user_id), "p": args.permission, "reason": args.reason.strip()},
            )
    print(f"{'Revoked' if args.revoke else 'Granted'} {args.permission} for user {args.user_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
