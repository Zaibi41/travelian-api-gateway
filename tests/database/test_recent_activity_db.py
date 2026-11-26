"""gateway#87: the dashboard's Recent activity from booking events (newest first, no details, unrecorded holds only with
supplier access, back office only)."""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.routes import admin_dashboard as routes
from app.core.field_visibility import ALL, NONE, BackOfficeUser, require_back_office, resolve_actions
from app.db import booking_payments, payment_accounts
from tests.database.test_booking_people import ACTOR, save

ADMIN = BackOfficeUser(ACTOR, "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser(ACTOR, "staff", NONE)


def api(viewer):
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    if viewer is None:
        def deny():
            raise HTTPException(403, "Back office only.")
        app.dependency_overrides[require_back_office] = deny
    else:
        app.dependency_overrides[require_back_office] = lambda: viewer
    return TestClient(app)


def test_newest_first_with_labels_and_never_event_details(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)  # hold_created
    account = payment_accounts.add("bank_transfer", "Meezan Bank")
    from datetime import datetime, timezone
    booking_payments.record(reference, amount_minor=10_000, currency="PKR", method="bank_transfer", account_id=account.id,
                            payer_reference="TXN-ACT-1", received_at=datetime.now(timezone.utc), actor_user_id=ACTOR)
    answer = api(STAFF).get("/v1/admin/recent-activity", params={"limit": 5})
    assert answer.status_code == 200
    items = answer.json()["items"]
    assert [(i["action"], i["label"], i["booking_id"]) for i in items[:2]] == [
        ("payment_recorded", "Payment recorded", reference), ("hold_created", "Hold placed", reference)]
    assert items[0]["actor_user_id"] == ACTOR and items[0]["product"] == "flight"
    for leak in ("TXN-ACT-1", "Meezan", "63B55C", "amount", "details", "southair"):
        assert leak not in answer.text
    assert len(api(STAFF).get("/v1/admin/recent-activity", params={"limit": 1}).json()["items"]) == 1


def test_unrecorded_hold_events_only_with_supplier_access_and_back_office_only(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    connection.execute(text("UPDATE bookings SET booking_status = 'hold_unrecorded' WHERE reference = :r"), {"r": reference})
    assert api(STAFF).get("/v1/admin/recent-activity").json()["items"] == []
    assert [i["booking_id"] for i in api(ADMIN).get("/v1/admin/recent-activity").json()["items"]] == [reference]
    assert api(None).get("/v1/admin/recent-activity").status_code == 403
    assert api(STAFF).get("/v1/admin/recent-activity", params={"limit": 51}).status_code == 422
