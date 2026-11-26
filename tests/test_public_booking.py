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
    public_booking.CALENDAR_LIMIT._hits.clear()
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
    assert flights[0]["fares"][0]["selling_price"]["amount_minor"] == 2251100  # 2% markup + the Rs 20 platform fee
    text = first.text.lower()
    for hidden in ("sooperfare", "southair", "supplier", "providers", "defd968f", "value|ev"):
        assert hidden not in text, hidden


def test_website_flight_booking_is_only_a_request_never_held_ticketed_or_given_a_pnr():
    """B19 / D43: a guest's flight booking is repriced by the gateway and saved as a request with the full customer and
    travelers; no reservation, no PNR, nothing paid or issued."""
    from tests.sooperfare.test_flows import ADULT, fake_sf, southair_down
    from app.db import supplier_bookings
    from app.services import flight_holds, live_flights

    public_booking._flight_cache.clear()
    live_flights._airlines.clear()
    live_flights._sessions.clear()
    live_flights._quotes.clear()
    sf = fake_sf()
    real_search, real_reprice = live_flights.search, live_flights.reprice

    async def search(request, viewer):
        return await real_search(request, viewer, client_factory=southair_down, sooperfare_factory=lambda: sf)

    async def reprice(search_id, fare_ids, viewer):
        return await real_reprice(search_id, fare_ids, viewer, sooperfare_factory=lambda: sf)

    saved = {}
    people = []

    def save_people(request, reference, actor, owner, links, document=None):
        people.append((reference, actor, owner, request.contact.email, [p.document_number for p in request.passengers]))
        return []

    def save_request(**kwargs):
        saved.update(kwargs)
        return "TRV-R-WEB1"

    view = SimpleNamespace(reference="TRV-R-WEB1", status="requested", payment_status="unpaid", booking_status="requested",
                           total_minor=0, currency="PKR")
    created = public_booking.PublicBookingView(reference="TRV-R-WEB1", status="requested", payment_status="unpaid", live=False, can_pay=False,
                                               total=public_booking.PublicMoney(amount_minor=2207000, currency="PKR"),
                                               paid=public_booking.PublicMoney(amount_minor=0, currency="PKR"))
    with patch.object(live_flights, "search", search), patch.object(live_flights, "reprice", reprice), \
         patch.object(settings, "flight_markup_percent", __import__("decimal").Decimal("0")), \
         patch.object(supplier_bookings, "save_request", save_request), patch.object(flight_holds, "resolve_saved", lambda request, viewer: (request, {})), \
         patch.object(flight_holds, "_save_people", save_people), \
         patch.object(public_booking.public_bookings, "mark_website_reference", lambda reference, token_hash: None), \
         patch.object(public_booking, "booking_view", AsyncMock(return_value=created)):
        results = client.post("/v1/public/flights/search", json={"origin": "KHI", "destination": "LHE", "departure_date": "2026-12-10"}).json()
        fare_id = results["journeys"][0]["flights"][0]["fares"][0]["id"]
        contact = {"first_name": "Test", "last_name": "Customer", "email": "guest@example.invalid", "phone_code": "92", "phone": "3001234567"}
        response = client.post("/v1/public/flights/requests", json={"search_id": results["search_id"], "fare_ids": [fare_id],
                                                                    "contact": contact, "passengers": [ADULT]})
        missing_email = client.post("/v1/public/flights/requests", json={"search_id": results["search_id"], "fare_ids": [fare_id],
                                                                         "contact": {**contact, "email": None}, "passengers": [ADULT]})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reference"] == "TRV-R-WEB1" and body["booking"]["status"] == "requested" and body["access_token"]
    assert "pnr" not in str(body).lower() or body["booking"].get("final_pnr") is None
    sf.reservation.assert_not_awaited()  # never held at the supplier
    assert saved["channel"] == "website" and saved["customer"].email == "guest@example.invalid" and saved["customer"].phone == "3001234567"
    assert [(t.first_name, t.document_number) for t in saved["travelers"]] == [("Test", "3520112345671")]  # full details saved
    assert saved["selling_total_minor"] == 2205000 + 2000  # the gateway's price, never the page's
    assert missing_email.status_code == 422
    # B23: the guest is saved to (or matched with) Customers & Travelers, as Travelian's own (no agency owner, no staff actor).
    assert people == [("TRV-R-WEB1", None, None, "guest@example.invalid", ["3520112345671"])]


@pytest.mark.parametrize("method", ["bank_transfer", "jazzcash"])
def test_bank_transfer_and_jazzcash_only_hold_and_alert_staff(method, request):
    node = request.node
    """D43: neither method confirms anything itself. The booking stays held, staff are alerted to check the money,
    record it and only then issue. (JazzCash is off today; this is the rule once it is on.)"""
    from app.vendors.payments import jazzcash

    booking = SimpleNamespace(id="b1", reference="TRV-GRP-1", itinerary=[{"sector": "LHE-JED"}], total_minor=9_000_000,
                              currency="PKR", pnr="ABC123", hold_expires_at=None)
    event = AsyncMock()
    request = public_booking.PublicGroupBookingRequest(
        ticket_id="11111111-1111-1111-1111-111111111111", adults=1, fare_terms_acknowledged=True, payment_method=method,
        contact={"first_name": "Test", "last_name": "Guest", "phone": "+923001234567", "email": "guest@example.invalid"},
        passengers=[{"type": "ADT", "title": "MR", "first_name": "Test", "last_name": "Guest", "date_of_birth": "1990-01-01",
                     "id_type": "PSPT", "document_number": "AB1234567", "document_country": "PAK", "document_expiry": "2030-01-01",
                     "nationality": "PAK"}])
    with patch.object(settings, "public_group_booking_enabled", True), patch.object(jazzcash, "enabled", lambda: True), \
         patch.object(public_booking, "payment_methods", AsyncMock(return_value=SimpleNamespace(bank_transfer=True, jazzcash=True))), \
         patch.object(public_booking.group_holds, "hold", AsyncMock(return_value=SimpleNamespace(reference="TRV-GRP-1"))), \
         patch.object(public_booking.store, "load", lambda reference: booking), \
         patch.object(public_booking.public_bookings, "mark_website", lambda booking_id, token_hash: None), \
         patch.object(public_booking.notifications, "booking_event", event), \
         patch.object(public_booking, "booking_view", AsyncMock(return_value=public_booking.PublicBookingView(
             reference="TRV-GRP-1", status="held", payment_status="unpaid", live=True, can_pay=False,
             total=public_booking.PublicMoney(amount_minor=9_000_000, currency="PKR"), paid=public_booking.PublicMoney(amount_minor=0, currency="PKR")))):
        created = __import__("asyncio").run(public_booking.book_group(request))
    assert node.customer_emails == [("TRV-GRP-1", method, "card")]   # the customer gets the account details (and QR) by email
    assert created.booking.status == "held" and created.booking.payment_status == "unpaid"
    event.assert_awaited_once()
    assert event.await_args.args[0] == "bank_transfer_hold" and "Nothing is issued automatically" in event.await_args.kwargs["note"]


def test_every_group_booking_path_saves_its_people():
    """B23: website group requests and website live holds save (or match) the customer and travelers, like staff holds."""
    import inspect
    from app.services import group_holds

    source = inspect.getsource(group_holds._hold)  # the body of group_holds.hold
    request_branch = source[source.index("if request_only:  # a request"):source.index("# 2. The hold.")]
    assert "_save_people" in request_branch
    tail = source[source.index("hold_created"):]
    assert tail.index("_save_people") < tail.index("if website:")  # saved before the website returns


def test_calendar_reads_have_their_own_limit_and_take_a_start_date():
    """B24 / D46: a page asking the calendar again while dates fill in never uses up the live-search allowance, and the
    results page's date strip asks from its first date."""
    from datetime import date, timedelta

    seen = []

    async def calendar(origin, destination, adults, cabin, start=None):
        seen.append(start)
        return public_booking.FlightCalendar(origin=origin, destination=destination, days=[], pending=False)

    start = (date.today() + timedelta(days=10)).isoformat()
    with patch.object(public_booking, "flight_calendar", calendar):
        answers = [client.post("/v1/public/flights/calendar", json={"origin": "LHE", "destination": "KHI", "start": start}).status_code
                   for _ in range(40)]
    assert set(answers) == {200}  # 40 reads in a minute: over the 30-search limit, within the calendar's own
    assert not public_booking.SEARCH_LIMIT._hits.get("testclient")  # no live-search allowance used
    assert seen[0] == date.fromisoformat(start)


def test_same_group_flight_from_several_suppliers_shows_once_at_the_lowest_fare():
    """travelian#228: identical flights (legs, dates, times) from two suppliers -> one ticket, the cheaper one."""
    from app.services.public_booking import lowest_fare_per_flight

    def ticket(tid, price, number="PA470", day="2026-12-28"):
        return {"id": tid, "selling_price": {"amount_minor": price, "currency": "PKR"},
                "legs": [{"direction": "outbound", "flight_number": number, "origin": "LHE", "destination": "JED",
                          "departure_date": day, "departure_time": "23:00"}]}

    view = {"data": [
        {"id": "dhillu", "tickets": [ticket("a", 17_500_000), ticket("c", 9_000_000, number="SV739")]},
        {"id": "alhaider", "tickets": [ticket("b", 16_900_000)]},
        {"id": "skypass", "tickets": [ticket("d", 18_000_000), ticket("e", 15_000_000, day="2026-12-29")]},
        {"id": "unknown", "tickets": [{"id": "x", "selling_price": {"amount_minor": 1, "currency": "PKR"}}]},  # too little to match: kept
    ]}
    out = lowest_fare_per_flight(view)
    ids = sorted(t["id"] for g in out["data"] for t in g["tickets"])
    assert ids == ["b", "c", "e", "x"]               # PA470 on 28 Dec once (16,900), the others untouched
    assert [g["id"] for g in out["data"]] == ["dhillu", "alhaider", "skypass", "unknown"]
