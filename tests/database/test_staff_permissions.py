import uuid

import pytest
import sqlalchemy as sa

from app.core import field_visibility as fv


def make_user(connection, role_id):
    user_id = str(uuid.uuid4())
    connection.execute(sa.text(
        "INSERT INTO users (id, cognito_issuer, cognito_sub, role_id) VALUES (:id, 'https://issuer.test', :sub, :role)"
    ), {"id": user_id, "sub": f"sub-{user_id}", "role": role_id})
    return user_id


def visibility(connection, user_id, role_id, role):
    return fv.resolve_visibility(role, fv.granted_permissions(connection, user_id, role_id))


def test_migration_adds_staff_role_and_permission_tables(database):
    connection, metadata, _ = database
    assert connection.scalar(sa.text("SELECT name FROM roles WHERE id=4")) == "staff"
    assert {"role_permissions", "user_permissions"} <= set(metadata.tables)


def test_staff_sees_nothing_until_granted_per_user_or_per_role(database):
    connection, _, _ = database
    staff = make_user(connection, 4)
    assert visibility(connection, staff, 4, "staff") == fv.NONE
    connection.execute(sa.text("INSERT INTO user_permissions (user_id, permission, reason) VALUES (:u, 'see_supplier_names', 'test')"), {"u": staff})
    assert visibility(connection, staff, 4, "staff") == fv.FieldVisibility(supplier_names=True, cost_and_margin=False)
    connection.execute(sa.text("INSERT INTO role_permissions (role_id, permission) VALUES (4, 'see_cost_and_margin')"))
    assert visibility(connection, staff, 4, "staff") == fv.ALL


def test_agency_and_customer_get_nothing_even_with_a_grant_row(database):
    connection, _, _ = database
    for role_id, role in ((2, "agency_owner"), (1, "customer")):
        user = make_user(connection, role_id)
        connection.execute(sa.text("INSERT INTO user_permissions (user_id, permission, reason) VALUES (:u, 'see_cost_and_margin', 'test')"), {"u": user})
        assert visibility(connection, user, role_id, role) == fv.NONE


def test_only_the_two_permissions_exist(database):
    connection, _, _ = database
    user = make_user(connection, 4)
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(sa.text("INSERT INTO user_permissions (user_id, permission, reason) VALUES (:u, 'see_everything', 'x')"), {"u": user})
