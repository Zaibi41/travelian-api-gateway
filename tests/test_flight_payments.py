"""Website flight payments (D43, D45, D48, D49): which path may issue a ticket, and when.

- Card: issuance starts only from a PayFast result the gateway verified (secured-key hash, amount) that made the booking
  fully paid. A forged, guessed or unverified result, a failed or partial payment, or a repeat never starts it.
- Ticketing off, or a provider the gateway can't issue for: nothing is sent to the supplier; paid, issuance pending.
- Bank transfer and JazzCash: held and staff alerted; nothing is issued automatically, ever.
Mocked suppliers and PayFast only; no database (the database version is tests/database/test_flight_payments_db.py).
"""

import asyncio
import hashlib
import inspect
import pathlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.db import issuance as issuance_store
from app.db import public_bookings
from app.main import app
from app.services import flight_payments, issuance, public_booking

client = TestClient(app)
MERCHANT, KEY = "14833", "test-secured-key"


@pytest.fixture
def payfast_on():
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)):
        yield


@pytest.fixture(autouse=True)
def fresh_limits():
    for limiter in (public_booking.SEARCH_LIMIT, public_booking.BOOK_LIMIT, public_booking.PAY_LIMIT, public_booking.CALENDAR_LIMIT):
        limiter._hits.clear()


def genuine(basket, err_code="000", **extra):
    digest = hashlib.sha256(f"{basket}|{KEY}|{MERCHANT}|{err_code}".encode()).hexdigest()
    return {"basket_id": basket, "err_code": err_code, "validation_hash": digest, **extra}


FORGERIES = [
    {"basket_id": "TRV-F-1-AB12", "err_code": "000", "transaction_amount": "22070.00", "validation_hash": "0" * 64},  # made up
    {"basket_id": "TRV-F-1-AB12", "err_code": "000",
     "validation_hash": hashlib.sha256(f"TRV-F-1-AB12|not-the-key|{MERCHANT}|000".encode()).hexdigest()},  # guessed key
    {**genuine("TRV-F-1-AB12", "002"), "err_code": "000"},  # a genuine failure turned into a success
    {"basket_id": "TRV-F-1-AB12", "err_code": "000"},  # no hash at all
    {**genuine("TRV-F-1-AB12"), "basket_id": "TRV-F-2-CD34"},  # a genuine hash moved to another basket
]


# ---------- card: only a verified, full PayFast payment can start issuance ----------

@pytest.mark.parametrize("forged", FORGERIES)
@pytest.mark.parametrize("path", ["return", "notify"])
def test_forged_or_unverified_payfast_never_records_a_payment_or_starts_issuance(payfast_on, forged, path):
    record = MagicMock(return_value=public_bookings.Outcome("TRV-F-1", "paid", "paid", "flight"))
    start = MagicMock()
    with patch.object(public_booking.public_bookings, "record_result", record), \
         patch.object(flight_payments, "start_after_verified_card_payment", start), \
         patch.object(issuance, "issue", AsyncMock()) as issue:
        if path == "return":
            response = client.post("/v1/public/payments/payfast/return", json=forged)
            assert response.json() == {"reference": None, "result": "not_verified"}
        else:
            response = client.post("/v1/public/payments/payfast/notify", data=forged)
            assert response.json() == {"status": "ignored"}
    record.assert_not_called()  # nothing is recorded
    start.assert_not_called()  # and nothing is issued
    issue.assert_not_awaited()


def test_payfast_off_never_starts_issuance():
    start = MagicMock()
    with patch.object(settings, "payfast_merchant_id", None), patch.object(flight_payments, "start_after_verified_card_payment", start):
        response = client.post("/v1/public/payments/payfast/notify", data={"basket_id": "B", "err_code": "000", "validation_hash": "x"})
    assert response.json() == {"status": "ignored"}
    start.assert_not_called()


@pytest.mark.parametrize("outcome", [
    public_bookings.Outcome("TRV-F-1", "failed"),
    public_bookings.Outcome("TRV-F-1", "already_paid"),  # a repeat of a result already applied
    public_bookings.Outcome("TRV-F-1", "already_failed"),
    public_bookings.Outcome("TRV-F-1", "amount_mismatch"),  # genuine, but not the amount the gateway asked for
    public_bookings.Outcome("", "unknown_basket"),
    public_bookings.Outcome("TRV-F-1", "paid", "part_paid", "flight"),  # not fully paid
    public_bookings.Outcome("TRV-G-1", "paid", "paid", "group"),  # group fares: staff issue (unchanged)
])
def test_only_a_verified_payment_that_fully_pays_a_flight_starts_issuance(payfast_on, outcome):
    start = MagicMock()
    with patch.object(public_booking.public_bookings, "record_result", MagicMock(return_value=outcome)), \
         patch.object(flight_payments, "start_after_verified_card_payment", start):
        client.post("/v1/public/payments/payfast/notify", data=genuine("TRV-F-1-AB12", transaction_amount="22070.00"))
    start.assert_not_called()


@pytest.mark.parametrize("path", ["return", "notify"])
def test_a_verified_full_flight_payment_starts_issuance_once(payfast_on, path):
    start = MagicMock()
    record = MagicMock(return_value=public_bookings.Outcome("TRV-F-1", "paid", "paid", "flight"))
    with patch.object(public_booking.public_bookings, "record_result", record), \
         patch.object(flight_payments, "start_after_verified_card_payment", start):
        params = genuine("TRV-F-1-AB12", transaction_id="T1", transaction_amount="22070.00")
        if path == "return":
            assert client.post("/v1/public/payments/payfast/return", json=params).json() == {"reference": "TRV-F-1", "result": "paid"}
        else:
            assert client.post("/v1/public/payments/payfast/notify", data=params).json() == {"status": "ok"}
    start.assert_called_once_with("TRV-F-1")
    assert record.call_args.kwargs["amount_minor"] == 2207000  # the amount is checked against the attempt by record_result


def test_only_payfast_result_reaches_automatic_issuance():
    """No other code path (staff payments, bank transfer, JazzCash, the admin) calls the automatic issuance."""
    callers = []
    for path in pathlib.Path("app").rglob("*.py"):
        text = path.read_text()
        if "after_verified_card_payment" in text and path.name != "flight_payments.py":
            callers.append(str(path))
    assert callers == ["app/services/public_booking.py"]
    source = inspect.getsource(public_booking.payfast_result)
    assert "payfast.verify(params)" in source and source.index("payfast.verify(params)") < source.index("start_after_verified_card_payment")
    assert "after_verified_card_payment" not in inspect.getsource(public_booking.request_flight)
    assert "after_verified_card_payment" not in inspect.getsource(public_booking._hold_flight)


# ---------- after a verified card payment ----------

def _target(provider="southair", paid=2207000, product="flight"):
    return issuance_store.Target(id="b1", reference="TRV-F-1", pnr="63B55C", provider=provider, product=product, booking_status="held",
                                 issuance_status="not_issued", hold_expires_at=datetime.now(timezone.utc) + timedelta(hours=3),
                                 currency="PKR", total_minor=2207000, supplier_cost_minor=2150000, supplier_ref="REF", paid_minor=paid,
                                 traveler_types=[(0, "ADT")])


def _after(target, ticketing=True, issue=None):
    alerts = []

    async def alert(kind, target, note, pnr):
        alerts.append((kind, note, pnr))

    issue = issue or AsyncMock(return_value=issuance.Outcome("issued", "issued", "Ticket issued.", {0: "2147483647001"}))
    with patch.object(flight_payments.store, "load", lambda reference: target), patch.object(flight_payments, "_alert", alert), \
         patch.object(settings, "southair_ticketing_enabled", ticketing), patch.object(settings, "sooperfare_ticketing_enabled", ticketing), \
         patch.object(issuance, "issue", issue):
        result = asyncio.run(flight_payments.after_verified_card_payment("TRV-F-1"))
    return result, issue, alerts


def test_ticketing_on_issues_at_once_with_the_gateway_as_issuer():
    result, issue, alerts = _after(_target())
    assert result == "issued"
    issue.assert_awaited_once()
    assert issue.await_args.args[0] == "TRV-F-1" and issue.await_args.args[1] is flight_payments.AUTO_ISSUER
    assert flight_payments.AUTO_ISSUER.user_id is None and not flight_payments.AUTO_ISSUER.visibility.cost_and_margin
    assert [kind for kind, *_ in alerts] == ["auto_issued"]


def test_ticketing_off_never_calls_the_supplier_and_tells_staff_issuance_is_pending():
    result, issue, alerts = _after(_target(), ticketing=False)
    assert result == "pending"
    issue.assert_not_awaited()
    assert [kind for kind, *_ in alerts] == ["paid_issuance_pending"] and "switched off" in alerts[0][1]


def test_sooperfare_is_never_issued_from_travelian_even_with_its_flag_on():
    """SooperFare ticket issuing (/v1/booking/confirm) is on hold (docs/roadmap.md): paid, issuance pending, staff alerted."""
    result, issue, alerts = _after(_target(provider="sooperfare"), ticketing=True)
    assert result == "pending"
    issue.assert_not_awaited()
    assert [kind for kind, *_ in alerts] == ["paid_issuance_pending"] and "portal" in alerts[0][1]


def test_not_fully_paid_or_not_a_flight_never_issues():
    for target in (_target(paid=100), _target(product="group")):
        result, issue, alerts = _after(target)
        assert result in ("pending", "not_flight") and alerts == []
        issue.assert_not_awaited()


@pytest.mark.parametrize("issue", [
    AsyncMock(side_effect=issuance_store.IssuanceRefused(409, "The hold has expired or is about to expire.")),
    AsyncMock(side_effect=RuntimeError("boom")),
    AsyncMock(return_value=issuance.Outcome("not_issued", "not_issued", "The supplier didn't issue the ticket.")),
    AsyncMock(return_value=issuance.Outcome("unknown", "unknown", "The ticket's status is unknown.")),
])
def test_a_failed_issuance_is_never_shown_as_issued_and_staff_are_alerted(issue):
    result, _, alerts = _after(_target(), issue=issue)
    assert result == "failed"
    assert [kind for kind, *_ in alerts] == ["auto_issue_failed"]


def test_the_background_runner_keeps_the_task_until_it_finishes():
    seen = []

    async def after(reference, client_factory=None):
        seen.append(reference)
        return "issued"

    async def run():
        with patch.object(flight_payments, "after_verified_card_payment", after):
            flight_payments.start_after_verified_card_payment("TRV-F-1")
            assert len(flight_payments._running) == 1
            await asyncio.gather(*flight_payments._running)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert seen == ["TRV-F-1"] and not flight_payments._running


# ---------- what the guest sees ----------

def _row(**overrides):
    row = {"product": "flight", "provider_id": "southair", "pnr": "63B55C", "booking_status": "held", "payment_status": "unpaid",
           "issuance_status": "not_issued", "hold_expires_at": datetime.now(timezone.utc) + timedelta(hours=3)}
    return {**row, **overrides}


def test_paid_flight_shows_in_progress_when_the_gateway_issues_and_pending_when_staff_do(payfast_on):
    paid = _row(payment_status="paid")
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert public_booking.flight_paid_status(paid) == "processing"  # "payment received, issuance in progress"
        assert public_booking.flight_paid_status({**paid, "provider_id": "sooperfare"}) == "confirmed"
    with patch.object(settings, "southair_ticketing_enabled", False):
        assert public_booking.flight_paid_status(paid) == "confirmed"  # "payment received, issuance pending"
        assert public_booking.flight_paid_status({**paid, "issuance_status": "unknown"}) == "processing"


def test_card_payment_is_offered_only_on_a_live_hold_with_time_left(payfast_on):
    now = datetime.now(timezone.utc)
    assert public_booking.flight_can_pay(_row(), now)
    for row in (_row(booking_status="requested", pnr=None), _row(payment_status="paid"), _row(issuance_status="processing"),
                _row(hold_expires_at=now + timedelta(minutes=10)), _row(hold_expires_at=None), _row(product="group")):
        assert not public_booking.flight_can_pay(row, now), row
    with patch.object(settings, "payfast_merchant_id", None):
        assert not public_booking.flight_can_pay(_row(), now)


# ---------- booking: hold, then the chosen method ----------

def _book(method="card", live=True, restricted=False, hold=None, methods=None, card_ok=None):
    """A website flight booking through the real route, with a SooperFare fare from the mocked search."""
    from tests.sooperfare.test_flows import ADULT, fake_sf, southair_down
    from app.services import flight_holds, flight_requests, live_flights, restricted_airlines
    from app.vendors.payments import jazzcash

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

    hold = hold or AsyncMock(return_value=SimpleNamespace(
        booking_id="TRV-F-WEB1", pnr="ZWHLZG", time_limit=datetime.now(timezone.utc) + timedelta(hours=3),
        selling_total=SimpleNamespace(amount_minor=2207000, currency="PKR")))
    create = AsyncMock(return_value=SimpleNamespace(booking_id="TRV-R-WEB1"))
    event = AsyncMock()
    view = public_booking.PublicBookingView(reference="TRV-F-WEB1", status="held" if live else "requested", payment_status="unpaid",
                                            live=live, can_pay=live, total=public_booking.PublicMoney(amount_minor=2207000, currency="PKR"),
                                            paid=public_booking.PublicMoney(amount_minor=0, currency="PKR"))
    with patch.object(live_flights, "search", search), patch.object(live_flights, "reprice", reprice), \
         patch.object(settings, "flight_markup_percent", Decimal("0")), patch.object(settings, "public_flight_booking_enabled", live), \
         patch.object(settings, "sooperfare_booking_enabled", True), patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), \
         patch.object(settings, "payfast_secured_key", SecretStr(KEY)), patch.object(jazzcash, "enabled", lambda: True), \
         patch.object(public_booking, "payment_methods", AsyncMock(return_value=methods or SimpleNamespace(bank_transfer=True))), \
         patch.object(restricted_airlines, "segment_restricted", lambda segment: restricted), \
         patch.object(flight_holds, "hold", hold), patch.object(flight_requests, "create", create), \
         patch.object(public_booking.notifications, "booking_event", event), \
         patch.object(public_booking.public_bookings, "mark_website_reference", lambda reference, token_hash: None), \
         patch.object(public_booking.public_bookings, "record_result", MagicMock()) as record, \
         patch.object(issuance, "issue", AsyncMock()) as issue, \
         patch.object(flight_payments, "start_after_verified_card_payment", MagicMock()) as start, \
         patch.object(public_booking, "booking_view", AsyncMock(return_value=view)), \
         patch.object(public_booking, "card_allowed", card_ok or public_booking.card_allowed):
        results = client.post("/v1/public/flights/search", json={"origin": "KHI", "destination": "LHE", "departure_date": "2026-12-10"}).json()
        fare_id = results["journeys"][0]["flights"][0]["fares"][0]["id"]
        contact = {"first_name": "Test", "last_name": "Customer", "email": "guest@example.invalid", "phone_code": "92", "phone": "3001234567"}
        response = client.post("/v1/public/flights/requests", json={"search_id": results["search_id"], "fare_ids": [fare_id],
                                                                    "contact": contact, "passengers": [ADULT], "payment_method": method})
    # Booking never pays or issues anything, whatever the method.
    record.assert_not_called()
    issue.assert_not_awaited()
    start.assert_not_called()
    return response, hold, create, event


def test_flag_off_keeps_website_flights_as_requests():
    response, hold, create, _ = _book(live=False)
    assert response.status_code == 201 and response.json()["booking"]["status"] == "requested"
    hold.assert_not_awaited()
    create.assert_awaited_once()


def test_restricted_airline_stays_a_request_with_the_flag_on():
    response, hold, create, _ = _book(restricted=True)
    assert response.status_code == 201
    hold.assert_not_awaited()
    create.assert_awaited_once()


def test_card_on_a_sooperfare_fare_is_refused_by_the_gateway_before_anything_is_held():
    """Decision 19 Nov 2026: no card for SooperFare fares (bank transfer or Book Only). Enforced server-side."""
    response, hold, create, _ = _book("card")  # the mocked search returns SooperFare fares
    assert response.status_code == 409 and response.json()["code"] == "method_unavailable"
    hold.assert_not_awaited()
    create.assert_not_awaited()
    ok, *_ = _book("bank_transfer")
    assert ok.status_code == 201


def test_card_is_never_payable_or_offered_for_sooperfare(payfast_on):
    assert public_booking.card_allowed("southair") and not public_booking.card_allowed("sooperfare")
    assert not public_booking.flight_can_pay(_row(provider_id="sooperfare"), datetime.now(timezone.utc))
    with patch.object(settings, "payfast_merchant_id", None):
        assert not public_booking.card_allowed("southair")


def test_payment_options_hide_card_for_sooperfare_fares():
    from tests.sooperfare.test_flows import fake_sf, southair_down
    from app.services import live_flights

    public_booking._flight_cache.clear()
    live_flights._sessions.clear()
    sf = fake_sf()
    real_search = live_flights.search

    async def search(request, viewer):
        return await real_search(request, viewer, client_factory=southair_down, sooperfare_factory=lambda: sf)

    methods = SimpleNamespace(bank_transfer=True, jazzcash=False)
    with patch.object(live_flights, "search", search), patch.object(settings, "public_flight_booking_enabled", True), \
         patch.object(settings, "sooperfare_booking_enabled", True), patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), \
         patch.object(settings, "payfast_secured_key", SecretStr(KEY)), patch.object(public_booking, "payment_methods", AsyncMock(return_value=methods)):
        results = client.post("/v1/public/flights/search", json={"origin": "KHI", "destination": "LHE", "departure_date": "2026-12-10"}).json()
        fare_id = results["journeys"][0]["flights"][0]["fares"][0]["id"]
        options = client.post("/v1/public/flights/payment-options", json={"search_id": results["search_id"], "fare_ids": [fare_id]})
        with patch.object(settings, "public_flight_booking_enabled", False):
            off = client.post("/v1/public/flights/payment-options", json={"search_id": results["search_id"], "fare_ids": [fare_id]})
        expired = client.post("/v1/public/flights/payment-options", json={"search_id": "no-such-search", "fare_ids": [fare_id]})
    assert options.json() == {"held": True, "card": False, "bank_transfer": True, "jazzcash": False}
    assert off.json() == {"held": False, "card": False, "bank_transfer": False, "jazzcash": False}
    assert expired.status_code == 404
    assert "sooperfare" not in options.text.lower()


def test_card_holds_the_fare_for_the_guest_and_nothing_else():
    response, hold, create, event = _book("card", card_ok=lambda provider: True)  # as for a Southair fare
    assert response.status_code == 201, response.text
    assert response.json()["reference"] == "TRV-F-WEB1" and response.json()["booking"]["status"] == "held"
    hold.assert_awaited_once()
    create.assert_not_awaited()
    assert hold.await_args.args[1].role == "website"
    assert hold.await_args.args[0].confirmed_total.amount_minor == 2207000  # the gateway's price, never the page's
    event.assert_not_awaited()  # the hold's own staff alert is sent by the hold


@pytest.mark.parametrize("method", ["bank_transfer", "jazzcash", "book_only"])
def test_bank_transfer_and_jazzcash_hold_and_alert_staff_and_never_issue(method):
    response, hold, _, event = _book(method)
    assert response.status_code == 201, response.text
    hold.assert_awaited_once()
    event.assert_awaited_once()
    assert event.await_args.args[0] == "bank_transfer_hold" and "Nothing is issued automatically" in event.await_args.kwargs["note"]
    assert event.await_args.kwargs["pnr"] == "ZWHLZG"


def test_bank_transfer_is_refused_without_a_receiving_account():
    response, hold, _, _ = _book("bank_transfer", methods=SimpleNamespace(bank_transfer=False))
    assert response.status_code == 409 and response.json()["code"] == "method_unavailable"
    hold.assert_not_awaited()


def test_a_failed_hold_shows_the_guest_one_plain_sentence_and_no_supplier_details():
    from app.services import flight_holds

    failing = AsyncMock(side_effect=flight_holds.HoldError(
        502, "The supplier created a hold (PNR ZWHLZG) but its total couldn't be verified, so it wasn't saved. Don't take payment for it.",
        code="total_unverified", pnr="ZWHLZG"))
    response, *_ = _book("bank_transfer", hold=failing)
    assert response.status_code == 502
    assert response.json() == {"detail": public_booking.HOLD_NOT_PLACED, "code": "hold_failed"}
    assert "ZWHLZG" not in response.text and "supplier" not in response.text.lower()
