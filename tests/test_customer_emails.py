"""Customer emails: who gets what, an SES refusal is recorded (admin shows "Email not sent"), once per kind."""

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
    monkeypatch.setattr(ce, "_already_sent", lambda booking_id, kind: any(r[1] in ((kind,) if isinstance(kind, str) else kind) and r[2] for r in recorded))
    monkeypatch.setattr(ce, "create_link", lambda booking_id, itinerary, purpose: "tok123")
    monkeypatch.setattr(ce, "_address_blocked", lambda booking_id: False)
    monkeypatch.setattr(ce, "_record", lambda booking_id, kind, ok, why, to, method=None, actor=None: recorded.append((booking_id, kind, ok, why, actor)))
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
    assert not ok and "isn't approved yet" in why and "sandbox" not in why.lower()
    assert recorded[-1][:3] == ("b1", "payment_received", False)
    event = lambda action, kind, reason=None, at=NOW: SimpleNamespace(action=action, details={"email": kind, "reason": reason}, created_at=at)
    problems = ce.email_problems([event("customer_email_not_sent", "payment_received", why), event("customer_email_not_sent", "booking_held", "x"),
                                  event("customer_email_sent", "booking_held", at=NOW + timedelta(minutes=1))])
    assert problems == [{"email": "payment_received", "at": NOW, "to": None, "reason": ce.plain_reason(why), "can_resend": True, "fix_address": False}]  # a later success clears it
    assert problems[0]["reason"].startswith("Email blocked: AWS hasn't approved")


def test_ses_access_denied_is_explained_without_ses_detail(wired, monkeypatch):
    class Denied(Exception):
        response = {"Error": {"Code": "AccessDenied", "Message": "User arn:aws:iam::123:user/x is not authorized to perform ses:SendEmail on "
                                                                 "resource arn:aws:ses:ap-southeast-1:123:identity/ali@example.invalid"}}

    class FakeSes:
        def send_email(self, **kwargs):
            raise Denied()

    import boto3

    monkeypatch.setattr(ce.settings, "notify_email_enabled", True)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeSes())
    ok, why = ce.deliver("TRV-1", "payment_received")
    assert not ok and "AccessDenied" in why and "approves full sending" in why and "sandbox" not in why.lower()
    assert "ali@example.invalid" not in why and "arn:" not in why


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


# ---------- gateway#52: hold ended, booking cancelled ----------

def test_hold_ended_email_only_after_a_hold_email(wired, monkeypatch):
    recorded, sent = wired
    monkeypatch.setattr(ce, "ses_send", lambda to, email: (sent.append((to, email)), (True, "sent"))[1])
    # Never told about the hold (e.g. a staff hold): nothing is sent or recorded.
    assert ce.deliver("TRV-1", "hold_expired") == (False, "not needed") and sent == [] and recorded == []
    assert ce.deliver("TRV-1", "booking_held")[0] is True
    assert ce.deliver("TRV-1", "hold_expired") == (True, "sent")
    email = sent[-1][1]
    assert email.subject == "Booking TRV-1: your hold has ended"
    assert "Nothing was charged" in email.html and "/flights-search" in email.html and "wa.me/" in email.html
    assert "Nothing was charged" in email.text and "Southair" not in email.html and "SooperFare" not in email.html
    assert ce.deliver("TRV-1", "hold_expired") == (False, "already sent")


def test_hold_ended_email_not_sent_when_money_was_paid(wired, monkeypatch):
    recorded, sent = wired
    monkeypatch.setattr(ce, "_facts", lambda reference: {**FACTS, "paid_minor": 1_000_000})
    monkeypatch.setattr(ce, "ses_send", lambda to, email: (sent.append((to, email)), (True, "sent"))[1])
    ce.deliver("TRV-1", "booking_held")
    assert ce.deliver("TRV-1", "hold_expired") == (False, "not needed") and len(sent) == 1


@pytest.mark.parametrize("paid,words", [(0, "Nothing was charged."), (2_407_000, "Our team will contact you about the refund.")])
def test_cancelled_email_says_what_happens_to_the_money(paid, words):
    booking = ce.booking_from({**FACTS, "paid_minor": paid}, "https://travelian.pk/my-bookings/view/tok")
    email = ce.render("booking_cancelled", booking)
    assert email.subject == "Booking TRV-1 is cancelled" and words in email.html and words in email.text
    assert "wa.me/" in email.html


def test_every_kind_renders():
    booking = ce.booking_from(dict(FACTS), "https://travelian.pk/my-bookings/view/tok")
    monkey_accounts = ce._accounts
    try:
        ce._accounts = lambda kind: []
        for kind in sorted(ce.KINDS):
            email = ce.render(kind, booking)
            assert email.subject and "TRV-1" in email.html and email.text
    finally:
        ce._accounts = monkey_accounts


# ---------- gateway#53: plain reasons, Resend email ----------

@pytest.mark.parametrize("stored,plain", [
    ("refused by SES (AccessDenied): Travelian's AWS sending permission doesn't cover this address. SES is still in sandbox mode", "Email blocked:"),
    ("refused by SES: the customer's address isn't verified (SES sandbox: only verified addresses receive email)", "Email blocked:"),
    ("no customer email on the booking", "No email address on the booking."),
    ("failed (Throttling)", "The email service didn't answer (Throttling). Try Resend email."),
])
def test_old_and_new_reasons_read_in_plain_words(stored, plain):
    assert ce.plain_reason(stored).startswith(plain) and "sandbox" not in ce.plain_reason(stored).lower()


def test_problems_show_who_it_went_to_and_old_events_without_a_kind():
    events = [SimpleNamespace(action="customer_email_not_sent", details={"to": "a***@gmail.com", "reason": "failed (AccessDenied)"}, created_at=NOW),
              SimpleNamespace(action="customer_email_not_sent", details={"email": "booking_held", "to": "a***@gmail.com", "reason": "failed (AccessDenied)"}, created_at=NOW)]
    problems = ce.email_problems(events)
    assert [(p["email"], p["to"], p["can_resend"]) for p in problems] == [("unknown", "a***@gmail.com", False), ("booking_held", "a***@gmail.com", True)]
    assert problems[1]["reason"].startswith("Email blocked")


def test_resend_sends_again_and_records_who(wired, monkeypatch):
    recorded, sent = wired
    monkeypatch.setattr(ce, "ses_send", lambda to, email: (sent.append((to, email)), (True, "sent"))[1])
    monkeypatch.setattr(ce, "_attempts", lambda booking_id, kind: [{"email": kind}] if kind == "booking_held" else [])
    assert ce.deliver("TRV-1", "booking_held")[0] is True
    assert ce.resend("TRV-1", "booking_held", "u-staff") == (True, "Email sent to the customer.")
    assert len(sent) == 2 and recorded[-1][-1] == "u-staff"


def test_resend_refuses_an_email_the_booking_never_had(wired):
    with pytest.raises(ce.ResendRefused) as error:
        ce.resend("TRV-1", "nonsense", "u-staff")
    assert error.value.status == 422
    import types
    ce_attempts = ce._attempts
    try:
        ce._attempts = lambda booking_id, kind: []
        with pytest.raises(ce.ResendRefused) as error:
            ce.resend("TRV-1", "ticket_issued", "u-staff")
        assert error.value.status == 409 and "never sent" in error.value.message
    finally:
        ce._attempts = ce_attempts


def test_resend_failure_says_why_in_plain_words(wired, monkeypatch):
    monkeypatch.setattr(ce, "_attempts", lambda booking_id, kind: [{"email": kind, "method": "bank transfer"}])
    monkeypatch.setattr(ce, "ses_send", lambda to, email: (False, "refused by the email service (AccessDenied): ..."))
    sent, message = ce.resend("TRV-1", "payment_received", "u-staff")
    assert not sent and message.startswith("Email blocked")
