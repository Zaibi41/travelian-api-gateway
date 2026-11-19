"""Real concurrency against PostgreSQL: PayFast's notification and the customer's return arrive at the same moment.

Unlike the other database tests (one connection, rolled back), these commit to a throwaway schema and use a connection
pool, so each request runs in its own transaction on its own connection, exactly as in production. The schema is
dropped afterwards.

What guarantees issuance starts exactly once (all in PostgreSQL, none in memory):
1. app.db.public_bookings.record_result locks the PayFast attempt row (SELECT ... FOR UPDATE) and moves it
   'initiated' -> 'paid' in that transaction: only one result can see 'initiated', so only one returns "paid" and
   starts issuance; the other waits for the lock, then sees 'paid' and returns "already_paid".
2. app.db.issuance.claim locks the booking row and moves issuance 'not_issued' -> 'processing'; a second claim sees
   'processing' and is refused before any supplier call.
3. app.db.flight_recovery.claim_recovery locks the booking (SKIP LOCKED) and writes the one recovery entry.
"""

import asyncio
import hashlib
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.db import agencies, public_bookings, supplier_bookings
from app.main import app
from app.services import flight_payments, issuance, public_booking
from tests.database.test_booking_people import CUSTOMER, TRAVELERS
from tests.database.test_issuance import ISSUED, FakeSouthair
from tests.southair.fixtures import pnr_info

pytestmark = pytest.mark.real_notifications
MERCHANT, KEY = "14833", "test-secured-key"
TOTAL = 46_700


@pytest.fixture
def pooled(monkeypatch):
    url = os.environ.get("TRAVELIAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TRAVELIAN_TEST_DATABASE_URL to a disposable PostgreSQL database")
    admin = sa.create_engine(url)
    schema = "test_conc_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    engine = sa.create_engine(url, pool_size=10, connect_args={"options": f"-csearch_path={schema}"})
    monkeypatch.setattr(agencies, "application_engine", lambda: engine)
    try:
        with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)), \
             patch.object(settings, "southair_ticketing_enabled", True):
            yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def website_hold(engine) -> str:
    reference = supplier_bookings.save_hold(
        provider="southair", pnr="63B55C", supplier_ref="REF", hold_expires_at=datetime.now(timezone.utc) + timedelta(hours=3),
        currency="PKR", selling_total_minor=TOTAL, supplier_total_minor=45_770, supplier_response={}, actor_user_id=None,
        audit={"pnr": "63B55C"}, customer=CUSTOMER, travelers=TRAVELERS)
    public_bookings.mark_website_reference(reference, public_bookings.token_hash("guest-token-123"))
    return reference


def signed(basket, err_code="000", **extra):
    digest = hashlib.sha256(f"{basket}|{KEY}|{MERCHANT}|{err_code}".encode()).hexdigest()
    return {"basket_id": basket, "err_code": err_code, "validation_hash": digest, **extra}


class SlowSouthair(FakeSouthair):
    """Holds the ticketing call open long enough for a competing attempt to run into it. Thread-safe answers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.Lock()

    async def pnr_info(self, pnr):
        with self.lock:
            return await super().pnr_info(pnr)

    async def ticketing(self, *, ticket_ref, cash_amount):
        await asyncio.sleep(0.3)
        return await super().ticketing(ticket_ref=ticket_ref, cash_amount=cash_amount)


def scalar(engine, sql, **params):
    with engine.begin() as connection:
        return connection.scalar(sa.text(sql), params)


def test_notification_and_return_at_the_same_moment_record_one_payment_and_issue_once(pooled):
    reference = website_hold(pooled)
    public_bookings.create_attempt(reference, "TRV-F-RACE", TOTAL, "PKR")
    fake = SlowSouthair([pnr_info(), ISSUED, ISSUED])
    issuers: list[threading.Thread] = []

    def start(ref):  # what the route schedules, run for real in its own thread with the fake supplier
        thread = threading.Thread(target=lambda: asyncio.run(flight_payments.after_verified_card_payment(ref, client_factory=lambda: fake)))
        issuers.append(thread)
        thread.start()

    params = signed("TRV-F-RACE", transaction_id="T1", transaction_amount="467.00")
    barrier = threading.Barrier(2)
    answers = {}

    def send(route):
        client = TestClient(app)
        barrier.wait()
        if route == "notify":
            answers[route] = client.post("/v1/public/payments/payfast/notify", data=params).json()  # PayFast's server, form post
        else:
            answers[route] = client.post("/v1/public/payments/payfast/return", json=params).json()  # the customer's browser

    public_booking.PAY_LIMIT._hits.clear()
    with patch.object(flight_payments, "start_after_verified_card_payment", start):
        senders = [threading.Thread(target=send, args=(route,)) for route in ("notify", "return")]
        for thread in senders:
            thread.start()
        for thread in senders:
            thread.join(30)
        for thread in issuers:
            thread.join(30)

    assert answers["notify"] == {"status": "ok"}
    assert answers["return"]["result"] in ("paid", "already_paid")
    assert len(issuers) == 1  # only the result that moved the attempt to 'paid' started issuance
    assert sum(1 for call in fake.calls if call[0] == "ticketing") == 1  # the supplier got exactly one issue request
    assert scalar(pooled, "SELECT count(*) FROM booking_payments") == 1
    assert scalar(pooled, "SELECT issuance_status FROM bookings WHERE reference = :r", r=reference) == "issued"


def test_two_issuance_attempts_at_once_send_one_ticketing_call(pooled):
    """Even if issuance were started twice (e.g. a recovery pass next to the live attempt), the booking claim lets one through."""
    reference = website_hold(pooled)
    public_bookings.create_attempt(reference, "TRV-F-TWICE", TOTAL, "PKR")
    asyncio.run(public_booking.payfast_result(signed("TRV-F-TWICE", transaction_amount="467.00"), "notification"))  # starts nothing here
    flight_payments._running.clear()
    fake = SlowSouthair([pnr_info(), ISSUED])  # the loser is refused at the claim, before any supplier call
    barrier = threading.Barrier(2)
    results = []

    def attempt():
        barrier.wait()
        results.append(asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)))

    with patch.object(flight_payments, "_alert", lambda *args, **kwargs: asyncio.sleep(0)):
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    assert sorted(results) == ["failed", "issued"]  # the second was refused: "Issuing is already in progress"
    assert sum(1 for call in fake.calls if call[0] == "ticketing") == 1
    assert scalar(pooled, "SELECT issuance_status FROM bookings WHERE reference = :r", r=reference) == "issued"


def test_two_recovery_passes_at_once_retry_once(pooled):
    reference = website_hold(pooled)
    public_bookings.create_attempt(reference, "TRV-F-PASS", TOTAL, "PKR")
    with patch.object(flight_payments, "start_after_verified_card_payment", lambda ref: None):
        asyncio.run(public_booking.payfast_result(signed("TRV-F-PASS", transaction_amount="467.00"), "notification"))
    fake = SlowSouthair([pnr_info(), ISSUED, pnr_info(), ISSUED])
    later = datetime.now(timezone.utc) + timedelta(minutes=11)
    barrier = threading.Barrier(2)
    outcomes = []

    def run_pass():
        barrier.wait()
        outcomes.append(asyncio.run(flight_payments.recover(now=later, client_factory=lambda: fake)))

    threads = [threading.Thread(target=run_pass) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(outcomes, key=len) == [{}, {reference: "issued"}]
    assert sum(1 for call in fake.calls if call[0] == "ticketing") == 1
    assert scalar(pooled, "SELECT count(*) FROM booking_events WHERE action = 'auto_issue_recovery'") == 1
