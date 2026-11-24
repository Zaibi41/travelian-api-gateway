"""travelian#202 routes without a database: what the detail shows to whom, and who may act."""

from datetime import datetime, timezone
from unittest.mock import patch

from app.core.config import settings
from app.db import offline_settlement
from app.db.offline_settlement import Settlement
from app.services import ticket_documents
from tests.core.test_admin_bookings import STAFF, STAFF_COST, SUPER_ADMIN, booking, client

SETTLED = Settlement(
    supplier_paid_from="credit", supplier_payment_reference="SF-CREDIT-77", supplier_paid_at=datetime(2026, 11, 23, 9, tzinfo=timezone.utc),
    ticket_document_key="ticket-documents/0f8fad5b-d9cb-469f-a165-70867728950e.pdf", ticket_document_type="application/pdf",
    waived_minor=100_600, original_total_minor=7_600_600, settled_reason="Agreed Rs 75,000 with the reseller",
    settled_at=datetime(2026, 11, 23, 8, tzinfo=timezone.utc), issued_at=datetime(2026, 11, 23, 10, tzinfo=timezone.utc), billed_minor=7_488_600,
)
ROWS = [booking(1, total_minor=7_500_000, supplier_cost_minor=7_600_600)]


def detail(monkeypatch, viewer):
    monkeypatch.setattr(offline_settlement, "read", lambda reference: SETTLED)
    return client(monkeypatch, rows=ROWS, viewer=viewer).get("/v1/admin/bookings/TRV-1").json()


def test_detail_shows_the_settlement_and_the_supplier_payment_to_cost_viewers_only(monkeypatch):
    admin = detail(monkeypatch, SUPER_ADMIN)
    assert admin["settlement"]["waived"] == {"amount_minor": 100_600, "currency": "PKR"}
    assert admin["settlement"]["original_total"]["amount_minor"] == 7_600_600
    assert admin["ticket_document"] is True and admin["issued_at"].startswith("2026-11-23T10")
    payment = admin["supplier_payment"]
    assert (payment["paid_from"], payment["transfer_reference"], payment["billed"]["amount_minor"]) == ("credit", "SF-CREDIT-77", 7_488_600)

    staff = detail(monkeypatch, STAFF)
    assert "supplier_payment" not in staff and "SF-CREDIT-77" not in str(staff) and "7488600" not in str(staff)
    assert staff["settlement"]["reason"] == "Agreed Rs 75,000 with the reseller"


def test_actions_need_their_permissions(monkeypatch):
    api = client(monkeypatch, rows=ROWS, viewer=STAFF_COST)
    assert api.post("/v1/admin/bookings/TRV-1/settle-due", json={"reason": "Agreed with the reseller"}).status_code == 403
    tickets = {"ticket_numbers": [{"position": 0, "ticket_number": "2142345678901"}]}
    assert api.post("/v1/admin/bookings/TRV-1/issuance/record", json=tickets).status_code == 403
    assert api.post("/v1/admin/bookings/TRV-1/ticket-document/upload", json={"content_type": "application/pdf"}).status_code == 403
    assert api.put("/v1/admin/bookings/TRV-1/ticket-document", json={"asset_key": SETTLED.ticket_document_key}).status_code == 403


def test_upload_is_refused_cleanly_without_storage(monkeypatch):
    api = client(monkeypatch, rows=ROWS, viewer=SUPER_ADMIN)
    with patch.object(settings, "assets_bucket", None):
        answer = api.post("/v1/admin/bookings/TRV-1/ticket-document/upload", json={"content_type": "application/pdf"})
    assert answer.status_code == 503
    assert api.post("/v1/admin/bookings/TRV-1/ticket-document/upload", json={"content_type": "text/html"}).status_code == 422


def test_document_keys_are_checked():
    assert ticket_documents.valid_key(SETTLED.ticket_document_key)
    for key in ("payment-proof/0f8fad5b-d9cb-469f-a165-70867728950e.pdf", "ticket-documents/../x.pdf",
                "ticket-documents/0f8fad5b-d9cb-469f-a165-70867728950e.html", None):
        assert not ticket_documents.valid_key(key)
