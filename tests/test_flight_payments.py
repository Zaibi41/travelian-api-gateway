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


PRODUCTION = "https://payfast-production.example"  # any host but ipguat.apps.net.pk counts as production


@pytest.fixture
def payfast_on():
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)), \
         patch.object(settings, "payfast_base_url", PRODUCTION):
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


def _after(target, ticketing=True, issue=None, payfast_production=True):
    """after_verified_card_payment with the fallback (hold check, re-hold, red alert) mocked: alerts lists
    ("auto_issued", note) and ("needs_staff", reason)."""
    from app.services import flight_fallback

    alerts = []

    async def alert(kind, target, note, pnr):
        alerts.append((kind, note))

    async def needs_staff(reference, reason, **kwargs):
        alerts.append(("needs_staff", reason))
        return "needs_staff"

    issue = issue or AsyncMock(return_value=issuance.Outcome("issued", "issued", "Ticket issued.", {0: "2147483647001"}))
    with patch.object(flight_payments.store, "load", lambda reference: target), patch.object(flight_payments, "_alert", alert), \
         patch.object(flight_fallback, "needs_staff", needs_staff), \
         patch.object(settings, "southair_ticketing_enabled", ticketing), patch.object(settings, "sooperfare_ticketing_enabled", ticketing), \
         patch.object(issuance, "issue", issue), patch.object(flight_payments.payfast, "production", lambda: payfast_production):
        result = asyncio.run(flight_payments.after_verified_card_payment("TRV-F-1"))
    return result, issue, alerts


def test_ticketing_on_issues_at_once_with_the_gateway_as_issuer():
    result, issue, alerts = _after(_target())
    assert result == "issued"
    issue.assert_awaited_once()
    assert issue.await_args.args[0] == "TRV-F-1" and issue.await_args.args[1] is flight_payments.AUTO_ISSUER
    assert flight_payments.AUTO_ISSUER.user_id is None and not flight_payments.AUTO_ISSUER.visibility.cost_and_margin
    assert [kind for kind, *_ in alerts] == ["auto_issued"]


def test_ticketing_off_never_calls_the_supplier_and_goes_to_staff():
    result, issue, alerts = _after(_target(), ticketing=False)
    assert result == "needs_staff"
    issue.assert_not_awaited()
    assert [kind for kind, _ in alerts] == ["needs_staff"] and "switched off" in alerts[0][1]


def test_sooperfare_is_never_issued_from_travelian_even_with_its_flag_on():
    """SooperFare is issued by staff from the admin only (gateway#45): a paid booking gets a staff red alert, never an issue call."""
    with patch.object(settings, "sooperfare_ticketing_enabled", True), patch.object(settings, "sooperfare_confirm_enabled", True):
        result, issue, alerts = _after(_target(provider="sooperfare"), ticketing=True)
    assert result == "needs_staff"
    issue.assert_not_awaited()
    assert [kind for kind, _ in alerts] == ["needs_staff"] and "never automatically" in alerts[0][1]


def test_payment_after_the_hold_ended_is_never_sent_for_ticketing():
    target = _target()
    late = issuance_store.Target(**{**target.__dict__, "hold_expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)})
    result, issue, alerts = _after(late)
    assert result == "needs_staff"
    issue.assert_not_awaited()
    assert "after the airline hold ended" in alerts[0][1]


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
def test_a_failed_issuance_is_never_shown_as_issued_and_goes_to_staff(issue):
    result, _, alerts = _after(_target(), issue=issue)
    assert result == "needs_staff"
    assert [kind for kind, _ in alerts] == ["needs_staff"]


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
         patch.object(settings, "payfast_secured_key", SecretStr(KEY)), patch.object(settings, "payfast_base_url", PRODUCTION), \
         patch.object(public_booking, "payment_methods", AsyncMock(return_value=methods or SimpleNamespace(bank_transfer=True, jazzcash=True))), \
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


def test_card_on_a_sooperfare_fare_holds_the_fare():
    """Decision 19 Nov 2026 (reversed): card and 1Link are offered for SooperFare fares too."""
    response, hold, create, _ = _book("card")  # the mocked search returns SooperFare fares
    assert response.status_code == 201, response.text
    hold.assert_awaited_once()
    create.assert_not_awaited()


def test_card_is_payable_for_every_supplier_while_payfast_is_set_up(payfast_on):
    assert public_booking.card_allowed("southair") and public_booking.card_allowed("sooperfare")
    assert public_booking.flight_can_pay(_row(provider_id="sooperfare"), datetime.now(timezone.utc))
    with patch.object(settings, "payfast_merchant_id", None):
        assert not public_booking.card_allowed("sooperfare")
    response, hold, *_ = _book("card", card_ok=lambda provider: False)  # PayFast not set up: refused before any hold
    assert response.status_code == 409 and response.json()["code"] == "method_unavailable"
    hold.assert_not_awaited()


def test_payment_options_offer_card_for_sooperfare_fares():
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
         patch.object(settings, "payfast_base_url", PRODUCTION), \
         patch.object(settings, "sooperfare_booking_enabled", True), patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), \
         patch.object(settings, "payfast_secured_key", SecretStr(KEY)), patch.object(public_booking, "payment_methods", AsyncMock(return_value=methods)):
        results = client.post("/v1/public/flights/search", json={"origin": "KHI", "destination": "LHE", "departure_date": "2026-12-10"}).json()
        fare_id = results["journeys"][0]["flights"][0]["fares"][0]["id"]
        options = client.post("/v1/public/flights/payment-options", json={"search_id": results["search_id"], "fare_ids": [fare_id]})
        with patch.object(settings, "public_flight_booking_enabled", False):
            off = client.post("/v1/public/flights/payment-options", json={"search_id": results["search_id"], "fare_ids": [fare_id]})
        expired = client.post("/v1/public/flights/payment-options", json={"search_id": "no-such-search", "fare_ids": [fare_id]})
    # alfalah: Bank Alfalah card (not configured here).
    assert options.json() == {"held": True, "card": True, "alfalah": False, "bank_transfer": True, "jazzcash": False}
    assert off.json() == {"held": False, "card": False, "alfalah": False, "bank_transfer": False, "jazzcash": False}
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
    assert event.await_args.args[0] == ("book_only_hold" if method == "book_only" else "bank_transfer_hold")
    assert "Nothing is issued automatically" in event.await_args.kwargs["note"] and event.await_args.kwargs["pnr"] == "ZWHLZG"


def test_bank_transfer_is_refused_without_a_receiving_account():
    response, hold, _, _ = _book("bank_transfer", methods=SimpleNamespace(bank_transfer=False, jazzcash=False))
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


# ---------- guards (19 Nov 2026): PayFast mode and payment-method availability ----------

def test_uat_payfast_never_offers_card_or_issues_automatically():
    """PayFast on UAT (ipguat.apps.net.pk): no card / 1Link at checkout, and a paid booking goes to staff (red alert)
    instead of auto-issuing, even with every ticketing flag on."""
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)), \
         patch.object(settings, "payfast_base_url", "https://ipguat.apps.net.pk"), patch.object(settings, "southair_ticketing_enabled", True):
        assert payfast_mode() == ("UAT", False)
        assert not public_booking.card_allowed("southair") and not public_booking.card_allowed("sooperfare")
        assert not public_booking.flight_can_pay(_row(), datetime.now(timezone.utc))
        assert flight_payments.startup_summary() == "PayFast mode: UAT, auto-issuing: off"
    result, issue, alerts = _after(_target(), ticketing=True, payfast_production=False)
    assert result == "needs_staff"
    issue.assert_not_awaited()
    assert "PayFast is in" in alerts[0][1]


def test_production_payfast_offers_card_and_auto_issues_with_ticketing_on(payfast_on):
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert payfast_mode() == ("production", True)
        assert flight_payments.startup_summary() == "PayFast mode: production, auto-issuing: on"
    with patch.object(settings, "southair_ticketing_enabled", False):
        assert flight_payments.startup_summary() == "PayFast mode: production, auto-issuing: off"
    with patch.object(settings, "payfast_merchant_id", None):
        assert flight_payments.startup_summary() == "PayFast mode: not configured, auto-issuing: off"


def payfast_mode():
    from app.vendors.payments import payfast

    return payfast.mode(), flight_payments.auto_issuing()


def test_sooperfare_ticketing_flag_on_never_reaches_a_supplier_call(payfast_on):
    """SOOPERFARE_TICKETING_ENABLED=true is harmless: no SooperFare issue call exists; a card-paid SooperFare booking goes
    to staff with a red alert."""
    with patch.object(settings, "sooperfare_ticketing_enabled", True):
        assert not issuance.ticketing_on("sooperfare")
        result, issue, alerts = _after(_target(provider="sooperfare"), ticketing=True)
    assert result == "needs_staff"
    issue.assert_not_awaited()


@pytest.mark.parametrize("accounts, bank, wallet", [
    ([], False, False),
    ([("bank_transfer", "Travelian Ltd", None, "PK36MEZN0001010105555555"), ("jazzcash", "Travelian", None, None)], False, False),  # IBAN only / no number
    ([("bank_transfer", "Travelian Ltd", "0101-0105555555", None), ("jazzcash", "Travelian Wallet", "03001234567", None)], True, True),
])
def test_bank_transfer_and_jazzcash_are_offered_only_with_a_full_account_number(accounts, bank, wallet):
    from app.db import payment_accounts

    rows = [SimpleNamespace(method=m, label=t, account_title=t, account_number=n, iban=i, qr_asset_key=None, bank_name="Bank")
            for m, t, n, i in accounts]
    with patch.object(payment_accounts, "list_accounts", lambda include_inactive=False: rows), \
         patch.object(settings, "bank_transfer_enabled", True), patch.object(settings, "jazzcash_enabled", True), \
         patch("app.api.routes.admin_uploads.site_assets", lambda: {}):
        methods = asyncio.run(public_booking.payment_methods())
        with patch.object(settings, "jazzcash_enabled", False):
            off = asyncio.run(public_booking.payment_methods())
    assert (methods.bank_transfer, methods.jazzcash) == (bank, wallet)
    assert off.jazzcash is False  # JAZZCASH_ENABLED off: never offered
    if wallet:
        assert methods.jazzcash_accounts[0].account_number == "03001234567"


def test_jazzcash_checkout_shows_its_qr_image():
    from app.db import payment_accounts

    rows = [SimpleNamespace(id="j1", method="jazzcash", label="Travelian Wallet", account_title="Travelian", account_number="03001234567",
                            iban=None, qr_asset_key="payment-qr/abc.png", bank_name=None)]
    with patch.object(payment_accounts, "list_accounts", lambda include_inactive=False: rows), \
         patch.object(settings, "jazzcash_enabled", True), patch.object(settings, "assets_public_base_url", "https://assets.example"), \
         patch("app.api.routes.admin_uploads.site_assets", lambda: {}):
        methods = asyncio.run(public_booking.payment_methods())
    wallet = methods.jazzcash_accounts[0]
    assert (wallet.account_number, wallet.qr_url) == ("03001234567", "https://assets.example/payment-qr/abc.png")
    assert wallet.qr_download_url == "/v1/public/payment-qr/j1"


def test_book_only_alert_has_its_own_title(request):
    from app.services import notifications

    asyncio.run(notifications.booking_event("book_only_hold", reference="TRV-F-BO1", passenger="MR TEST Booking", route=None,
                                            fare_minor=100, currency="PKR", pnr="ABC123", hold_expires_at=None, note="Book Only"))
    kind, reference, _ = request.node.notifications[-1]
    assert (kind, reference) == ("book_only_hold", "TRV-F-BO1")


def test_book_only_alert_title_text():
    import inspect

    from app.services import notifications

    assert '"book_only_hold": "Held (Book Only) — no payment yet"' in inspect.getsource(notifications.booking_event)


def test_payfast_allow_uat_switch_turns_card_and_auto_issuing_on_for_uat_testing():
    """PAYFAST_ALLOW_UAT: testing everything on UAT credentials. Off: UAT never offers card or issues automatically."""
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)), \
         patch.object(settings, "payfast_base_url", "https://ipguat.apps.net.pk"), patch.object(settings, "southair_ticketing_enabled", True):
        with patch.object(settings, "payfast_allow_uat", True):
            assert flight_payments.startup_summary() == "PayFast mode: UAT (allowed for testing), auto-issuing: on"
            assert public_booking.card_allowed("southair") and public_booking.flight_can_pay(_row(), datetime.now(timezone.utc))
        with patch.object(settings, "payfast_allow_uat", False):
            assert flight_payments.startup_summary() == "PayFast mode: UAT, auto-issuing: off"
            assert not public_booking.card_allowed("southair")
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)), \
         patch.object(settings, "payfast_base_url", PRODUCTION), patch.object(settings, "payfast_allow_uat", True), \
         patch.object(settings, "southair_ticketing_enabled", True):
        assert flight_payments.startup_summary() == "PayFast mode: production, auto-issuing: on"  # the switch only matters on UAT
