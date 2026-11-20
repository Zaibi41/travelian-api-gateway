"""Phase 1 payments (travelian#69/#74/#76, gateway#56) without a database: card fee maths, CSV money, permissions, slip files."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.core.field_visibility as fv
from app.api.routes.finance import csv_text, rupees
from app.core.authentication import require_authenticated_user
from app.main import app
from app.services import card_fee, payment_proof
from tests.core.test_finance import _client


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize("due,bps,fee", [
    (4_670_000, 300, 140_100),    # Rs 46,700 -> Rs 1,401
    (1_666_700, 300, 50_000),     # Rs 16,667 -> 500.01 -> Rs 500
    (1_650_000, 300, 49_500),     # Rs 16,500 -> Rs 495
    (1_683_400, 300, 50_500),     # Rs 16,834 -> 505.02 -> Rs 505
    (1_683_333, 300, 50_500),     # paisa due: 505.0 -> Rs 505
    (5_000, 300, 200),            # Rs 50 -> 1.5 -> Rs 2 (halves up)
    (100, 300, 0),                # Rs 1 -> 0.03 -> Rs 0
    (4_670_000, 0, 0),
    (4_670_000, 250, 116_800),    # 2.5%: 1167.5 -> Rs 1,168
])
def test_card_fee_rounds_to_the_nearest_rupee(due, bps, fee):
    assert card_fee.fee_minor(due, bps) == fee and fee % 100 == 0


def test_card_fee_quote_defaults_and_group_switch():
    q = card_fee.quote(4_670_000, "flight", {})
    assert (q.bps, q.fee_minor, q.total_minor, q.percent_label) == (300, 140_100, 4_810_100, "3%")
    assert card_fee.quote(4_670_000, "group", {}).fee_minor == 140_100          # group fares included by default
    off = card_fee.quote(4_670_000, "group", {"card_fee_on_group": False})
    assert (off.fee_minor, off.total_minor) == (0, 4_670_000)
    assert card_fee.quote(4_670_000, "flight", {"card_fee_bps": 250}).percent_label == "2.5%"


def test_card_fee_refuses_floats_and_bad_settings():
    with pytest.raises(TypeError):
        card_fee.fee_minor(100.0, 300)
    for bad in (-1, 1001, 3.0, True, "3"):
        with pytest.raises(ValueError):
            card_fee.validate("card_fee_bps", bad)


def test_csv_money_is_exact_and_formula_safe():
    assert [rupees(v) for v in (0, 5, 140_100, -2_050, None)] == ["0.00", "0.05", "1401.00", "-20.50", ""]
    assert csv_text(["A"], [["=HYPERLINK(1)"], ["+92 300"], ["TRV-1"]]).splitlines() == ["A", "'=HYPERLINK(1)", "'+92 300", "TRV-1"]


@pytest.mark.parametrize("path", ["/v1/admin/finance/payments", "/v1/admin/finance/payments.csv", "/v1/admin/finance/refunds-due",
                                  "/v1/admin/finance/settings/payments"])
@pytest.mark.parametrize("role,granted", [("staff", {fv.RECORD_PAYMENTS}), ("agency_owner", {fv.FINANCE}), ("customer", set())])
def test_payments_screens_need_finance(monkeypatch, path, role, granted):
    assert _client(monkeypatch, role, granted).get(path).status_code == 403


def test_slips_open_to_finance_or_record_payments_only(monkeypatch):
    from app.db import payment_slips

    monkeypatch.setattr(payment_slips, "get", lambda slip_id: (_ for _ in ()).throw(payment_slips.SlipRefused(404, "Payment slip not found.")))
    assert _client(monkeypatch, "staff").get("/v1/admin/finance/slips/s1/link").status_code == 403
    app.dependency_overrides.clear()
    assert _client(monkeypatch, "staff", {fv.RECORD_PAYMENTS}).get("/v1/admin/finance/slips/s1/link").status_code == 404
    app.dependency_overrides.clear()
    assert _client(monkeypatch, "agency_owner", {fv.FINANCE, fv.RECORD_PAYMENTS}).get("/v1/admin/finance/slips/s1/link").status_code == 403


def test_payment_settings_change_is_validated(monkeypatch):
    from app.db import finance

    saved = {}
    monkeypatch.setattr(finance, "get_settings", lambda connection=None: dict(saved))
    monkeypatch.setattr(finance, "set_setting", lambda key, value, actor, reason=None: saved.update({key: value}) or dict(saved))
    client = _client(monkeypatch, "super_admin")
    assert client.get("/v1/admin/finance/settings/payments").json() == {"card_fee_bps": 300, "card_fee_on_group": True, "card_fee_refunded": False}
    assert client.put("/v1/admin/finance/settings/payments", json={"card_fee_bps": 2000}).status_code == 422
    assert client.put("/v1/admin/finance/settings/payments", json={"card_fee_bps": 250, "reason": "PayFast rate"}).json()["card_fee_bps"] == 250


class FakeS3:
    def __init__(self, head=None):
        self.head = head or {}
        self.posted = None

    def generate_presigned_post(self, **kw):
        self.posted = kw
        return {"url": "https://bucket.s3.example/", "fields": {"key": kw["Key"]}}

    def head_object(self, Bucket, Key):  # noqa: N803
        if not self.head:
            raise RuntimeError("404")
        return self.head

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        return f"https://signed.example/{Params['Key']}?expires={ExpiresIn}"


def test_slip_upload_is_private_typed_and_size_limited(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "assets_bucket", "bucket")
    s3 = FakeS3()
    signed = payment_proof.sign_upload("application/pdf", client=s3)
    assert signed.asset_key.startswith("payment-proof/") and signed.asset_key.endswith(".pdf")
    assert ["content-length-range", 1, 5 * 1024 * 1024] in s3.posted["Conditions"]
    assert s3.posted["Fields"]["Cache-Control"] == "private, no-store" and "acl" not in str(s3.posted).lower()
    with pytest.raises(payment_proof.ProofError):
        payment_proof.sign_upload("image/svg+xml", client=s3)
    key = signed.asset_key
    assert payment_proof.inspect(key, client=FakeS3({"ContentType": "application/pdf", "ContentLength": 1200})) == ("application/pdf", 1200)
    for head in ({"ContentType": "text/html", "ContentLength": 10}, {"ContentType": "application/pdf", "ContentLength": 6 * 1024 * 1024}):
        with pytest.raises(payment_proof.ProofError):
            payment_proof.inspect(key, client=FakeS3(head))
    with pytest.raises(payment_proof.ProofError):
        payment_proof.inspect(key, client=FakeS3())  # never uploaded
    for bad in ("site/x.png", "payment-proof/../site/logo.png", "payment-proof/abc.exe"):
        assert not payment_proof.valid_key(bad)
    assert "expires=300" in payment_proof.signed_link(key, client=s3)


def test_public_slip_routes_need_the_token_or_the_booking_contact(monkeypatch):
    from app.db import public_bookings

    monkeypatch.setattr(public_bookings, "guest_can_open", lambda reference, token: token == "good-token-123")
    monkeypatch.setattr(public_bookings, "lookup", lambda reference, contact, trusted=False: {"reference": reference} if contact == "a@b.pk" else None)
    monkeypatch.setattr(payment_proof, "sign_upload", lambda content_type: payment_proof.SignedProofUpload("https://s3/", {}, "payment-proof/x.pdf", 5))
    client = TestClient(app)
    url = "/v1/public/bookings/TRV-ABC123/payment-slips/upload"
    assert client.post(url, json={"content_type": "application/pdf"}).status_code == 404
    assert client.post(url, json={"content_type": "application/pdf"}, headers={"X-Booking-Token": "wrong-token-1"}).status_code == 404
    assert client.post(url, json={"content_type": "application/pdf", "contact": "x@y.pk"}).status_code == 404
    assert client.post(url, json={"content_type": "application/pdf"}, headers={"X-Booking-Token": "good-token-123"}).status_code == 200
    assert client.post(url, json={"content_type": "application/pdf", "contact": "a@b.pk"}).status_code == 200
    assert client.post(url, json={"content_type": "text/html", "contact": "a@b.pk"}).status_code == 422


def test_storage_access_denied_is_not_blamed_on_the_customer():
    class Denied(Exception):
        response = {"Error": {"Code": "403"}}

    class S3(FakeS3):
        def head_object(self, Bucket, Key):  # noqa: N803
            raise Denied()

    with pytest.raises(payment_proof.ProofError) as error:
        payment_proof.inspect("payment-proof/11111111-1111-4111-8111-111111111111.pdf", client=S3())
    assert error.value.status == 503


def test_public_card_fee_quote_is_worked_out_by_the_gateway(monkeypatch):
    from app.db import finance

    monkeypatch.setattr(finance, "get_settings", lambda connection=None: {})
    body = TestClient(app).post("/v1/public/card-fee", json={"amount_minor": 4_670_000, "product": "flight"}).json()
    assert body == {"percent": "3%", "fee": {"amount_minor": 140_100, "currency": "PKR"}, "total": {"amount_minor": 4_810_100, "currency": "PKR"}}
    assert TestClient(app).post("/v1/public/card-fee", json={"amount_minor": 0}).status_code == 422
