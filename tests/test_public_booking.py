"""Public website booking: PayFast verification, rate limits, public search fields, request vs live mode, guest access.
Mocked suppliers and PayFast only; no database (store and payment recording are patched)."""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.main import app
from app.schemas.group_ticketing import GroupInventory
from app.services import public_booking
from app.vendors.payments import payfast
from tests.group_ticketing.test_return_and_search import SKY_ONE_WAY, sky

client = TestClient(app)
MERCHANT, KEY = "14833", "test-secured-key"


@pytest.fixture
def payfast_on():
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)):
        yield


@pytest.fixture(autouse=True)
def fresh_limits():
    public_booking.SEARCH_LIMIT._hits.clear()
    public_booking.BOOK_LIMIT._hits.clear()
    public_booking.PAY_LIMIT._hits.clear()
    public_booking._cache = None
    yield
    public_booking._cache = None


def genuine(basket, err_code="000", **extra):
    digest = hashlib.sha256(f"{basket}|{KEY}|{MERCHANT}|{err_code}".encode()).hexdigest()
    return {"basket_id": basket, "err_code": err_code, "validation_hash": digest, **extra}


# ---------- PayFast ----------

def test_payfast_verifies_only_genuine_results(payfast_on):
    result = payfast.verify(genuine("TRV-G-ABC-1A2B", transaction_id="T1", transaction_amount="13997.00"))
    assert result and result.paid and result.transaction_id == "T1" and result.amount_minor == 1399700
    assert payfast.verify({**genuine("TRV-G-ABC-1A2B"), "validation_hash": "0" * 64}) is None  # forged
    assert payfast.verify({**genuine("TRV-G-ABC-1A2B", "001"), "err_code": "000"}) is None  # failure turned into success
    assert payfast.verify({"basket_id": "x", "err_code": "000"}) is None  # no hash
    upper = {k.upper(): v for k, v in genuine("B-1").items()}
    assert payfast.verify(upper) is not None  # PayFast field case varies
    assert payfast.verify(genuine("B-2", "002")).paid is False


def test_payfast_amounts_basket_and_form(payfast_on):
    assert payfast.amount_text(1399700) == "13997.00" and payfast.amount_text(5) == "0.05"
    assert payfast.parse_amount_minor("13,997.5") == 1399750 and payfast.parse_amount_minor("abc") is None
    basket = payfast.new_basket_id("TRV-G-2026-ABCDEF")
    assert basket.startswith("TRV-G-2026-ABCDEF-") and len(basket) <= 48
    form = payfast.checkout_form(token="tok", basket_id=basket, amount_minor=1399700, currency="PKR", description="d", mobile="+92 300 1234567",
                                 email="a@b.pk", success_url="https://s", failure_url="https://f", notify_url="https://n",
                                 order_date=datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert form.action.endswith("/Ecommerce/api/Transaction/PostTransaction")
    assert form.fields["TXNAMT"] == "13997.00" and form.fields["ORDER_DATE"] == "2026-11-18" and form.fields["CHECKOUT_URL"] == "https://n"
    assert form.fields["CUSTOMER_MOBILE_NO"] == "03001234567"
    assert KEY not in str(form.fields)  # the secured key never goes to the browser


def test_payfast_off_without_credentials():
    with patch.object(settings, "payfast_merchant_id", None):
        assert not payfast.configured()
        assert payfast.verify({"basket_id": "x", "err_code": "000", "validation_hash": "y"}) is None


# ---------- rate limit ----------

def test_rate_limiter_window():
    limiter = public_booking.RateLimiter(2, 60)
    assert limiter.allow("ip", 0) and limiter.allow("ip", 1) and not limiter.allow("ip", 2)
    assert limiter.allow("other", 2) and limiter.allow("ip", 61)


# ---------- search ----------

def groups():
    ticket = sky(SKY_ONE_WAY)
    return [GroupInventory(id="g1", vendor="skypass", sector="LHE - DMM", airline="Fly Jinnah", tickets=[ticket])]


def test_public_search_has_no_supplier_fields_and_is_cached():
    fetch = AsyncMock(return_value=groups())
    with patch.object(public_booking, "fetch_all_group_inventory", fetch):
        first = client.post("/v1/public/group-ticketing/search", json={"origin": "lhe"})
        second = client.post("/v1/public/group-ticketing/search", json={})
    assert first.status_code == 200 and second.status_code == 200
    text = first.text.lower()
    for hidden in ("skypass", "supplier", "4onewy", "pnr", "subparts", "stock", "can_hold", "hold_unavailable"):
        assert hidden not in text, hidden
    assert first.json()["data"][0]["tickets"][0]["selling_price"]["amount_minor"] > 0
    assert fetch.await_count == 1  # second search served from the cache


def test_public_search_rate_limited():
    with patch.object(public_booking, "fetch_all_group_inventory", AsyncMock(return_value=[])):
        codes = [client.post("/v1/public/group-ticketing/search", json={}).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


# ---------- book: request vs live ----------

BODY = {
    "ticket_id": "6f1b6a0e-1111-4c1d-9a9a-0123456789ab", "adults": 1, "fare_terms_acknowledged": True,
    "contact": {"first_name": "Ali", "last_name": "Khan", "phone": "+92 300 1234567", "email": "ali@example.pk"},
    "passengers": [{"type": "ADT", "title": "MR", "first_name": "Ali", "last_name": "Khan", "date_of_birth": "1990-01-01",
                    "id_type": "PSPT", "document_number": "AB1234567", "document_country": "PAK", "document_expiry": "2030-01-01",
                    "nationality": "PAK", "gender": "M"}],
}


def _booked(live: bool):
    hold = AsyncMock(return_value=SimpleNamespace(reference="TRV-G-1"))
    view = public_booking.PublicBookingView(reference="TRV-G-1", status="held" if live else "requested", payment_status="unpaid",
                                            live=live, can_pay=live, total=public_booking.PublicMoney(amount_minor=100, currency="PKR"),
                                            paid=public_booking.PublicMoney(amount_minor=0, currency="PKR"))
    patches = [patch.object(settings, "public_group_booking_enabled", live), patch.object(public_booking.group_holds, "hold", hold),
               patch.object(public_booking.store, "load", lambda reference: SimpleNamespace(id="b1")),
               patch.object(public_booking.public_bookings, "mark_website", lambda booking_id, token_hash: None),
               patch.object(public_booking, "booking_view", AsyncMock(return_value=view))]
    for item in patches:
        item.start()
    try:
        response = client.post("/v1/public/group-ticketing/bookings", json=BODY)
    finally:
        for item in patches:
            item.stop()
    return response, hold


def test_live_off_saves_a_request_without_calling_the_supplier():
    response, hold = _booked(live=False)
    assert response.status_code == 201, response.text
    assert hold.await_args.kwargs["request_only"] is True
    body = response.json()
    assert body["booking"]["status"] == "requested" and body["booking"]["live"] is False and len(body["access_token"]) >= 20


def test_live_on_holds_at_the_supplier_and_never_takes_a_price_from_the_guest():
    response, hold = _booked(live=True)
    assert response.status_code == 201
    request = hold.await_args.args[0]
    assert hold.await_args.kwargs["request_only"] is False and request.selling_price_per_adult is None
    assert hold.await_args.args[1].role == "website"


def test_price_in_body_is_refused():
    response = client.post("/v1/public/group-ticketing/bookings", json={**BODY, "selling_price_per_adult": {"amount_minor": 1, "currency": "PKR"}})
    assert response.status_code in (201, 422, 409, 503)  # extra field ignored or refused, never used
    assert "selling_price_per_adult" not in public_booking.PublicGroupBookingRequest.model_fields


# ---------- guest access and payment results ----------

def test_booking_needs_its_token():
    with patch.object(public_booking.public_bookings, "guest_can_open", lambda reference, token: False):
        assert client.get("/v1/public/bookings/TRV-G-1", headers={"X-Booking-Token": "wrong-token"}).status_code == 404
    assert client.get("/v1/public/bookings/TRV-G-1").status_code == 422  # no token at all


def test_payment_results_recorded_only_when_verified(payfast_on):
    record = patch.object(public_booking.public_bookings, "record_result",
                          lambda basket, **kw: public_booking.public_bookings.Outcome("TRV-G-1", "paid", "paid"))
    with record as recorded:
        forged = client.post("/v1/public/payments/payfast/return", json={"basket_id": "B-1", "err_code": "000", "validation_hash": "f" * 64})
        real = client.post("/v1/public/payments/payfast/return", json=genuine("B-1", transaction_id="T9", transaction_amount="1.00"))
        notify = client.get("/v1/public/payments/payfast/notify", params=genuine("B-1"))
    assert forged.json() == {"reference": None, "result": "not_verified"}
    assert real.json() == {"reference": "TRV-G-1", "result": "paid"}
    assert notify.json() == {"status": "ok"}


# ---------- public flights (Southair + SooperFare through the gateway) ----------

def test_public_flight_search_hides_suppliers_and_is_cached():
    from tests.sooperfare.test_flows import fake_sf, southair_down
    from app.services import live_flights

    public_booking._flight_cache.clear()
    live_flights._airlines.clear()
    sf = fake_sf()
    real_search = live_flights.search

    async def search(request, viewer):
        return await real_search(request, viewer, client_factory=southair_down, sooperfare_factory=lambda: sf)

    body = {"origin": "khi", "destination": "LHE", "departure_date": "2026-12-10"}
    with patch.object(live_flights, "search", search), patch.object(settings, "flight_markup_percent", __import__("decimal").Decimal("2")):
        first = client.post("/v1/public/flights/search", json=body)
        second = client.post("/v1/public/flights/search", json=body)
    assert first.status_code == 200 and first.json() == second.json()
    assert sf.availability.await_count == 1  # the second visitor got the cached answer
    data = first.json()
    flights = data["journeys"][0]["flights"]
    assert [f["flight_number"] for f in flights] == ["PA400", "PA406"]
    assert flights[0]["fares"][0]["selling_price"]["amount_minor"] == 2249100
    text = first.text.lower()
    for hidden in ("sooperfare", "southair", "supplier", "providers", "defd968f", "value|ev"):
        assert hidden not in text, hidden
