"""Admin announcements against a real database (travelian#92): who may read and change them, the live list, and the
audit trail."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import admin_announcements as store
from tests.database.test_finance_payment_accounts import client_as as _client_as

PATH = "/v1/admin/announcements"


def client_as(connection, role):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.admin_announcements import router
    from app.core.authentication import require_authenticated_user

    finance_client, user_id = _client_as(connection, role)   # creates the user and the identity override
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = finance_client.app.dependency_overrides[require_authenticated_user]
    return TestClient(app), user_id


@pytest.fixture
def admin(database):
    connection, _, _ = database
    client, user_id = client_as(connection, "super_admin")
    return connection, client, user_id


def test_super_admin_posts_and_all_staff_see_it(admin):
    connection, client, user_id = admin
    response = client.post(PATH, json={"message": "  New group seats   on LHE-JED  "})
    assert response.status_code == 201
    created = response.json()
    assert created["message"] == "New group seats on LHE-JED" and created["status"] == "active"
    assert set(created) == {"id", "message", "created_at", "starts_at", "ends_at", "status", "ended_at"}
    staff, _ = client_as(connection, "staff")
    listed = staff.get(PATH)
    assert listed.status_code == 200
    assert listed.json()["items"] == [{k: created[k] for k in ("id", "message", "created_at", "starts_at", "ends_at")}]
    assert connection.execute(text("SELECT created_by_user_id FROM admin_announcements")).scalar() == user_id
    assert [(e["action"], e["actor_user_id"], e["actor_role"]) for e in store.events(created["id"])] == [("create", user_id, "super_admin")]


def test_empty_list(admin):
    _, client, _ = admin
    assert client.get(PATH).json() == {"items": []}
    assert client.get(f"{PATH}/all").json() == {"items": []}


def test_newest_first(admin):
    _, client, _ = admin
    first = client.post(PATH, json={"message": "First"}).json()
    second = client.post(PATH, json={"message": "Second"}).json()
    assert [item["id"] for item in client.get(PATH).json()["items"]] == [second["id"], first["id"]]


@pytest.mark.parametrize("role", ["staff", "agency_owner", "customer"])
def test_only_super_admin_may_post_end_or_delete(admin, role):
    connection, client, _ = admin
    created = client.post(PATH, json={"message": "Fare change today"}).json()
    other, _ = client_as(connection, role)
    assert other.post(PATH, json={"message": "Not allowed"}).status_code == 403
    assert other.post(f"{PATH}/{created['id']}/end").status_code == 403
    assert other.delete(f"{PATH}/{created['id']}").status_code == 403
    assert other.get(f"{PATH}/all").status_code == 403
    assert connection.execute(text("SELECT count(*) FROM admin_announcements")).scalar() == 1
    assert [item["id"] for item in client.get(PATH).json()["items"]] == [created["id"]]   # still live


@pytest.mark.parametrize("role", ["agency_owner", "customer"])
def test_agency_and_customer_roles_cannot_read_staff_announcements(admin, role):
    connection, client, _ = admin
    client.post(PATH, json={"message": "Staff only"})
    other, _ = client_as(connection, role)
    response = other.get(PATH)
    assert response.status_code == 403 and "Staff only" not in response.text


def test_end_takes_it_off_the_top_bar_and_is_audited(admin):
    connection, client, user_id = admin
    created = client.post(PATH, json={"message": "Ends soon"}).json()
    ended = client.post(f"{PATH}/{created['id']}/end")
    assert ended.status_code == 200 and ended.json()["status"] == "ended" and ended.json()["ended_at"]
    assert client.get(PATH).json()["items"] == []
    assert [item["status"] for item in client.get(f"{PATH}/all").json()["items"]] == ["ended"]
    assert client.post(f"{PATH}/{created['id']}/end").status_code == 404   # already ended
    assert [e["action"] for e in store.events(created["id"])] == ["create", "end"]
    assert connection.execute(text("SELECT ended_by_user_id FROM admin_announcements")).scalar() == user_id


def test_delete_hides_it_everywhere_but_keeps_the_record(admin):
    connection, client, user_id = admin
    created = client.post(PATH, json={"message": "Delete me"}).json()
    deleted = client.delete(f"{PATH}/{created['id']}")
    assert deleted.status_code == 200 and deleted.json() == {"id": created["id"], "deleted": True}
    assert client.get(PATH).json()["items"] == [] and client.get(f"{PATH}/all").json()["items"] == []
    assert client.delete(f"{PATH}/{created['id']}").status_code == 404
    assert client.post(f"{PATH}/{created['id']}/end").status_code == 404
    row = connection.execute(text("SELECT deleted_by_user_id, active FROM admin_announcements")).one()
    assert tuple(row) == (user_id, False)
    assert [e["action"] for e in store.events(created["id"])] == ["create", "delete"]


def test_an_ended_announcement_can_still_be_deleted(admin):
    _, client, _ = admin
    created = client.post(PATH, json={"message": "Old news"}).json()
    client.post(f"{PATH}/{created['id']}/end")
    assert client.delete(f"{PATH}/{created['id']}").status_code == 200
    assert [e["action"] for e in store.events(created["id"])] == ["create", "end", "delete"]


def test_unknown_id_is_404(admin):
    _, client, _ = admin
    assert client.post(f"{PATH}/00000000-0000-4000-8000-000000000000/end").status_code == 404
    assert client.delete(f"{PATH}/00000000-0000-4000-8000-000000000000").status_code == 404


def test_start_and_end_times(admin):
    _, client, _ = admin
    now = datetime.now(timezone.utc)
    later = client.post(PATH, json={"message": "Starts tomorrow", "starts_at": (now + timedelta(days=1)).isoformat()}).json()
    window = client.post(PATH, json={"message": "Live now", "starts_at": (now - timedelta(hours=1)).isoformat(),
                                     "ends_at": (now + timedelta(hours=1)).isoformat()}).json()
    assert later["status"] == "scheduled" and window["status"] == "active"
    assert [item["id"] for item in client.get(PATH).json()["items"]] == [window["id"]]
    assert {item["id"]: item["status"] for item in client.get(f"{PATH}/all").json()["items"]} == {later["id"]: "scheduled", window["id"]: "active"}


@pytest.mark.parametrize("body, detail", [
    ({"message": "x"}, "The announcement must be 2 to 240 characters."),
    ({"message": "x" * 241}, "The announcement must be 2 to 240 characters."),
    ({"message": "<script>alert(1)</script>"}, "Use plain text only: < and > are not allowed."),
    ({"message": "Past", "ends_at": "2020-01-01T00:00:00Z"}, "The end time must be in the future."),
    ({"message": "Backwards", "starts_at": "2099-01-02T00:00:00Z", "ends_at": "2099-01-01T00:00:00Z"}, "The end time must be after the start time."),
])
def test_invalid_posts_are_refused_and_nothing_is_saved(admin, body, detail):
    connection, client, _ = admin
    response = client.post(PATH, json=body)
    assert response.status_code == 422 and response.json()["detail"] == detail
    assert connection.execute(text("SELECT count(*) FROM admin_announcements")).scalar() == 0
    assert connection.execute(text("SELECT count(*) FROM admin_announcement_events")).scalar() == 0


def test_database_refuses_bad_rows_and_audit_rows_never_change(admin):
    connection, client, _ = admin
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(text("INSERT INTO admin_announcements (id, message) VALUES ('a', 'x')"))
    created = client.post(PATH, json={"message": "Audited"}).json()
    for statement in ("UPDATE admin_announcement_events SET actor_role = 'staff'", "DELETE FROM admin_announcement_events"):
        with pytest.raises(DBAPIError, match="never changed or deleted"):
            with connection.begin_nested():
                connection.execute(text(statement))
    assert len(store.events(created["id"])) == 1
