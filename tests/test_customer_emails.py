"""Customer emails: who gets what, the SES sandbox refusal is recorded (admin shows "Email not sent"), once per kind."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import customer_emails as ce

REAL_DELIVER = ce.deliver  # the suite stubs deliver for every other test (tests/conftest.py)

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
FACTS = {"id": "b1", "reference": "TRV-1", "pnr": "OFZOJG", "hold_expires_at": NOW + timedelta(hours=20), "currency": "PKR", "total_minor": 2_407_000,
         "itinerary": [{"origin": "LHE", "destination": "KHI", "airline_name": "Airblue", "flight_number": "PA401", "departure_local": "2026-12-10T11:15"}],
         "product": "flight", "paid_minor": 0, "c_first": "Ali", "c_last": "Raza", "c_email": "ali@example.invalid",
         "travelers": [("MR Ali Raza", None)]}


def test_link_expires_at_the_later_of_30_days_and_two_days_after_travel():
    assert ce.link_expiry(NOW, FACTS["itinerary"]) == NOW + timedelta(days=30)
    late = [{"departure_local": "2027-03-01T09:00"}]
    assert ce.link_expiry(NOW, late) == datetime(2027, 3, 3, tzinfo=timezone.utc)
    assert ce.link_expiry(NOW, None) == NOW + timedelta(days=30)


@pytest.fixture
def wired(monkeypatch):
    recorded, sent = [], []
    monkeypatch.setattr(ce, "deliver", REAL_DELIVER)
    monkeypatch.setattr(ce, "_facts", lambda reference: dict(FACTS))
    monkeypatch.setattr(ce, "_already_sent", lambda booking_id, kind: any(r[1] == kind and r[2] for r in recorded))
    monkeypatch.setattr(ce, "create_link", lambda booking_id, itinerary, purpose: "tok123")
    monkeypatch.setattr(ce, "_record", lambda booking_id, kind, ok, why, to: recorded.append((booking_id, kind, ok, why)))
    return recorded, sent


def test_sent_once_with_the_signed_link(wired, monkeypatch):
    recorded, sent = wired
    monkeypatch.setattr(ce, "ses_send", lambda to, email: (sent.append((to, email)), (True, "sent"))[1])
    assert ce.deliver("TRV-1", "booking_held") == (True, "sent")
    to, email = sent[0]
    assert to == "ali@example.invalid" and "/my-bookings/view/tok123" in email.html and "Airblue PA401" in email.html
    assert "Southair" not in email.html and "SooperFare" not in email.html
    assert ce.deliver("TRV-1", "booking_held") == (False, "already sent") and len(sent) == 1


def test_ses_sandbox_refusal_is_recorded_for_the_admin(wired, monkeypatch):
    recorded, _ = wired

    class Rejected(Exception):
        response = {"Error": {"Code": "MessageRejected", "Message": "Email address is not verified. The following identities failed the check"}}

    class FakeSes:
        def send_email(self, **kwargs):
            raise Rejected()

    import boto3

    monkeypatch.setattr(ce.settings, "notify_email_enabled", True)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeSes())
    ok, why = ce.deliver("TRV-1", "payment_received")
    assert not ok and "isn't verified (SES sandbox" in why
    assert recorded[-1][:3] == ("b1", "payment_received", False)
    event = lambda action, kind, reason=None, at=NOW: SimpleNamespace(action=action, details={"email": kind, "reason": reason}, created_at=at)
    problems = ce.email_problems([event("customer_email_not_sent", "payment_received", why), event("customer_email_not_sent", "booking_held", "x"),
                                  event("customer_email_sent", "booking_held", at=NOW + timedelta(minutes=1))])
    assert problems == [{"email": "payment_received", "at": NOW, "reason": why}]       # a later success clears it


def test_no_customer_email_is_recorded_too(wired, monkeypatch):
    recorded, _ = wired
    monkeypatch.setattr(ce, "_facts", lambda reference: {**FACTS, "c_email": None})
    assert ce.deliver("TRV-1", "ticket_issued") == (False, "no customer email on the booking")
    assert recorded[-1][1:3] == ("ticket_issued", False)


def test_switch_off_skips_and_says_so(monkeypatch):
    monkeypatch.setattr(ce.settings, "notify_email_enabled", False)
    assert ce.ses_send("a@example.invalid", None) == (False, "skipped: NOTIFY_EMAIL_ENABLED is off")


def test_booking_link_route_opens_one_booking_read_only(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import public_booking

    monkeypatch.setattr(ce, "booking_for_link", lambda token: "TRV-1" if token == "good-token-123" else None)
    rows = {"reference": "TRV-1", "product": "flight", "booking_status": "held", "payment_status": "unpaid", "issuance_status": "not_issued",
            "hold_expires_at": NOW + timedelta(days=1), "currency": "PKR", "total_minor": 2_407_000, "pnr": "OFZOJG", "itinerary": FACTS["itinerary"],
            "final_pnr": None, "paid_minor": 0, "travelers": [{"title": "MR", "first_name": "Ali", "last_name": "Raza", "ticket_number": None}]}
    seen = []
    monkeypatch.setattr(public_booking.public_bookings, "lookup", lambda reference, contact, trusted=False: seen.append(trusted) or rows)
    client = TestClient(app)
    expired = client.get("/v1/public/booking-links/unknown-token-1")
    assert expired.status_code == 404 and "expired" in expired.json()["detail"]
    good = client.get("/v1/public/booking-links/good-token-123")
    assert good.status_code == 200 and good.json()["reference"] == "TRV-1" and good.json()["travelers"] == ["MR Ali Raza"]
    assert seen == [True] and "can_pay" not in good.json()                # read-only lookup shape, opened by the link
