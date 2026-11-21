"""gateway#59: SES bounces and complaints through SNS. Signatures are checked with a certificate made here (the real
ones come from sns.<region>.amazonaws.com); the database is mocked, tests/test_customer_emails.py covers the admin view."""

import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.services import customer_emails as ce
from app.services import ses_feedback as sf

TOPIC = "arn:aws:sns:us-east-1:123456789012:ses-feedback"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc.pem"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
CERT = (x509.CertificateBuilder().subject_name(_name).issuer_name(_name).public_key(KEY.public_key()).serial_number(1)
        .not_valid_before(datetime(2026, 1, 1)).not_valid_after(datetime(2030, 1, 1)).sign(KEY, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM))
fetch = lambda url: CERT  # noqa: E731
REAL_DELIVER = ce.deliver  # the suite stubs deliver for every test (tests/conftest.py)


@pytest.fixture(autouse=True)
def topic(monkeypatch):
    monkeypatch.setattr(sf.settings, "ses_sns_topic_arns", TOPIC)


def signed(message: dict, version: str = "2", key=KEY) -> dict:
    message = {"TopicArn": TOPIC, "MessageId": "m-1", "Timestamp": "2026-11-21T08:00:00.000Z", "SignatureVersion": version,
               "SigningCertURL": CERT_URL, **message}
    digest = hashes.SHA1() if version == "1" else hashes.SHA256()
    message["Signature"] = base64.b64encode(key.sign(sf.string_to_sign(message), padding.PKCS1v15(), digest)).decode()
    return message


def bounce(kind="Permanent", message_id="ses-0001"):
    return json.dumps({"notificationType": "Bounce", "mail": {"messageId": message_id},
                       "bounce": {"bounceType": kind, "bounceSubType": "General", "timestamp": "2026-11-21T08:00:00Z",
                                  "bouncedRecipients": [{"emailAddress": "bounce@simulator.amazonses.com"}]}})


@pytest.mark.parametrize("version", ["1", "2"])
def test_a_signed_bounce_is_recorded(monkeypatch, version):
    saved = []
    monkeypatch.setattr(sf, "record", lambda items: saved.extend(items) or ["TRV-1"])
    body = json.dumps(signed({"Type": "Notification", "Message": bounce()}, version)).encode()
    assert sf.handle(body, fetch=fetch) == {"result": "recorded", "bookings": ["TRV-1"]}
    assert saved == [{"type": "bounce", "bounce_type": "Permanent", "bounce_sub_type": "General", "recipient": "bounce@simulator.amazonses.com",
                      "at": "2026-11-21T08:00:00Z", "message_id": "ses-0001"}]


@pytest.mark.parametrize("change,status", [
    (lambda m: {**m, "Message": bounce("Transient")}, 403),                          # changed after signing
    (lambda m: {**m, "Signature": ""}, 403),                                         # unsigned
    (lambda m: {k: v for k, v in m.items() if k != "Signature"}, 403),
    (lambda m: {**m, "SigningCertURL": "https://evil.example.com/cert.pem"}, 403),    # certificate not from SNS
    (lambda m: {**m, "SigningCertURL": "http://sns.us-east-1.amazonaws.com/x.pem"}, 403),
    (lambda m: {**m, "SignatureVersion": "3"}, 403),
])
def test_forged_or_unsigned_messages_are_refused(monkeypatch, change, status):
    monkeypatch.setattr(sf, "record", lambda items: pytest.fail("nothing may be recorded"))
    message = change(signed({"Type": "Notification", "Message": bounce()}))
    with pytest.raises(sf.Refused) as error:
        sf.handle(json.dumps(message).encode(), fetch=fetch)
    assert error.value.status == status


def test_a_message_signed_by_another_key_is_refused(monkeypatch):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(sf.Refused) as error:
        sf.handle(json.dumps(signed({"Type": "Notification", "Message": bounce()}, key=other)).encode(), fetch=fetch)
    assert error.value.status == 403


def test_unknown_topic_and_no_topic_configured_are_refused(monkeypatch):
    body = json.dumps(signed({"Type": "Notification", "Message": bounce(), "TopicArn": "arn:aws:sns:us-east-1:999:other"})).encode()
    with pytest.raises(sf.Refused, match="Unknown SNS topic"):
        sf.handle(body, fetch=fetch)
    monkeypatch.setattr(sf.settings, "ses_sns_topic_arns", "")
    with pytest.raises(sf.Refused, match="isn't set up"):
        sf.handle(json.dumps(signed({"Type": "Notification", "Message": bounce()})).encode(), fetch=fetch)


def test_subscription_is_confirmed_only_at_an_sns_url():
    visited = []
    good = signed({"Type": "SubscriptionConfirmation", "Message": "confirm", "Token": "t",
                   "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"})
    assert sf.handle(json.dumps(good).encode(), fetch=fetch, get=visited.append) == {"result": "subscribed"}
    assert visited == [good["SubscribeURL"]]
    bad = signed({"Type": "SubscriptionConfirmation", "Message": "confirm", "Token": "t", "SubscribeURL": "https://evil.example.com/"})
    with pytest.raises(sf.Refused):
        sf.handle(json.dumps(bad).encode(), fetch=fetch, get=visited.append)
    assert len(visited) == 1


def test_complaints_are_read_too():
    items = sf.feedback({"notificationType": "Complaint", "mail": {"messageId": "ses-9"},
                         "complaint": {"complaintFeedbackType": "abuse", "timestamp": "2026-11-21T09:00:00Z",
                                       "complainedRecipients": [{"emailAddress": "complaint@simulator.amazonses.com"}]}})
    assert items == [{"type": "complaint", "bounce_type": "abuse", "bounce_sub_type": None, "recipient": "complaint@simulator.amazonses.com",
                      "at": "2026-11-21T09:00:00Z", "message_id": "ses-9"}]
    assert sf.feedback({"notificationType": "Delivery"}) == []


# ---------- what staff see ----------

NOW = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
ev = lambda action, at, **details: SimpleNamespace(action=action, created_at=NOW + timedelta(minutes=at), details=details)  # noqa: E731


def test_a_permanent_bounce_asks_staff_to_fix_the_address():
    problems = ce.email_problems([ev("customer_email_sent", 0, email="booking_held", to="b***@simulator.amazonses.com"),
                                  ev("customer_email_bounced", 1, email="booking_held", type="bounce", bounce_type="Permanent", to="b***@simulator.amazonses.com")])
    assert problems == [{"email": "booking_held", "at": NOW + timedelta(minutes=1), "to": "b***@simulator.amazonses.com",
                         "reason": "Bounced: the address doesn't accept email. Contact the customer and fix the email address.",
                         "can_resend": True, "fix_address": True}]


def test_a_complaint_flags_too_and_a_temporary_bounce_does_not():
    complaint = ce.email_problems([ev("customer_email_bounced", 1, email="ticket_issued", type="complaint", bounce_type="abuse")])[0]
    assert complaint["fix_address"] is True and complaint["reason"].startswith("The customer marked this email as spam.")
    soft = ce.email_problems([ev("customer_email_bounced", 1, email="ticket_issued", type="bounce", bounce_type="Transient")])[0]
    assert soft["fix_address"] is False and soft["reason"].startswith("Bounced for now")


def test_changing_the_address_clears_the_flag_and_a_resend_clears_the_problem():
    events = [ev("customer_email_sent", 0, email="booking_held"), ev("customer_email_bounced", 1, email="booking_held", type="bounce", bounce_type="Permanent"),
              ev("customer_email_changed", 2)]
    assert ce.email_problems(events) == [{"email": "booking_held", "at": NOW + timedelta(minutes=1), "to": None,
                                          "reason": "The email address has been changed since. Resend this email to the new address.",
                                          "can_resend": True, "fix_address": False}]
    assert ce.email_problems([*events, ev("customer_email_sent", 3, email="booking_held")]) == []


def test_no_email_goes_to_a_bounced_address(monkeypatch):
    recorded, sent = [], []
    monkeypatch.setattr(ce, "_facts", lambda reference: {"id": "b1", "reference": "TRV-1", "pnr": None, "hold_expires_at": None, "currency": "PKR",
                                                         "total_minor": 100, "itinerary": [], "product": "flight", "paid_minor": 0,
                                                         "c_first": "A", "c_last": "B", "c_email": "bounce@simulator.amazonses.com", "travelers": []})
    monkeypatch.setattr(ce, "_already_sent", lambda booking_id, kind: False)
    monkeypatch.setattr(ce, "create_link", lambda *a: "tok")
    monkeypatch.setattr(ce, "_address_blocked", lambda booking_id: True)
    monkeypatch.setattr(ce, "_record", lambda booking_id, kind, ok, why, to, method=None, actor=None: recorded.append((kind, ok, why)))
    monkeypatch.setattr(ce, "ses_send", lambda to, email: sent.append(to) or (True, "sent"))
    assert REAL_DELIVER("TRV-1", "ticket_issued") == (False, "address bounced or complained")
    assert sent == [] and recorded == [("ticket_issued", False, "address bounced or complained")]
    assert ce.plain_reason("address bounced or complained").startswith("Not sent: this address bounced")


def test_the_ses_message_id_is_kept_with_the_sent_email(monkeypatch):
    events = []

    class Engine:
        def begin(self):
            import contextlib
            return contextlib.nullcontext("conn")

    monkeypatch.setattr(ce.agencies, "application_engine", lambda: Engine())
    monkeypatch.setattr(ce, "_event", lambda conn, booking_id, actor, action, details: events.append((action, details)))
    ce._record("b1", "booking_held", True, "sent id=0100018abc", "ali@example.invalid")
    assert events == [("customer_email_sent", {"email": "booking_held", "to": "a***@example.invalid", "message_id": "0100018abc"})]


@pytest.mark.parametrize("email,status", [("not-an-email", 422), ("same@example.invalid", 409)])
def test_address_change_is_checked(monkeypatch, email, status):
    monkeypatch.setattr(ce, "_facts", lambda reference: {"id": "b1", "c_email": "same@example.invalid"})
    with pytest.raises(ce.ResendRefused) as error:
        ce.change_address("TRV-1", email, "u-staff")
    assert error.value.status == status


def test_webhook_route_refuses_without_setup_and_records_when_signed(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import ses_webhook

    app = FastAPI()
    app.include_router(ses_webhook.router, prefix="/v1")
    client = TestClient(app)
    monkeypatch.setattr(sf, "_certificate", fetch)
    monkeypatch.setattr(sf, "record", lambda items: ["TRV-1"])
    body = json.dumps(signed({"Type": "Notification", "Message": bounce()}))
    assert client.post("/v1/webhooks/ses", content=body, headers={"content-type": "text/plain"}).json() == {"result": "recorded", "bookings": ["TRV-1"]}
    assert client.post("/v1/webhooks/ses", content="nonsense").status_code == 400
    monkeypatch.setattr(sf.settings, "ses_sns_topic_arns", "")
    assert client.post("/v1/webhooks/ses", content=body).status_code == 403
