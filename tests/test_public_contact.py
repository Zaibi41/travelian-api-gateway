"""gateway#114: the Contact page's message endpoint: validation, a reply address required, rate limit, spam trap, stored
and emailed through the staff notifications, never logged."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import public as routes
from app.services import notifications, public_booking

GOOD = {"name": "Ayesha Khan", "email": "ayesha@example.invalid", "message": "Do you have group seats to Jeddah in March?"}


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    with patch.object(public_booking, "CONTACT_LIMIT", public_booking.RateLimiter(5, 3600)):
        yield TestClient(app)


def test_a_message_is_stored_and_emailed_and_never_logged(api, caplog):
    record = patch.object(notifications, "_record", return_value="n-1")
    send = patch.object(notifications, "_send_email", return_value=True)
    marked = patch.object(notifications, "_mark_emailed")
    with record as rec, send as mail, marked as mark, caplog.at_level(logging.DEBUG):
        answer = api.post("/v1/public/contact", json={**GOOD, "phone": "+92 300 1234567", "name": "  Ayesha\nKhan "})
    assert answer.status_code == 202 and "Thanks" in answer.json()["message"]
    kind, reference, title, details = rec.call_args.args
    assert (kind, reference, title) == ("contact_message", None, "Website message from Ayesha Khan")
    assert details == {"name": "Ayesha Khan", "email": GOOD["email"], "phone": "+92 300 1234567", "message": GOOD["message"]}
    subject, body, html = mail.call_args.args
    assert GOOD["message"] in body and html is None and "\n" not in subject
    mark.assert_called_once_with("n-1")
    assert "Jeddah" not in caplog.text and "ayesha@" not in caplog.text


@pytest.mark.parametrize("body", [
    {**GOOD, "email": None},                   # no email and no mobile: nothing to reply to
    {**GOOD, "email": "not-an-email"},
    {**GOOD, "message": "Hi"},                  # too short
    {**GOOD, "message": "x" * 2001},
    {**GOOD, "name": "A"},
    {**GOOD, "phone": "call me"},
])
def test_invalid_messages_are_refused_and_nothing_is_stored(api, body):
    with patch.object(notifications, "contact_message", new=AsyncMock()) as sent:
        assert api.post("/v1/public/contact", json=body).status_code == 422
    sent.assert_not_called()


def test_a_mobile_number_alone_is_enough(api):
    with patch.object(notifications, "contact_message", new=AsyncMock()) as sent:
        assert api.post("/v1/public/contact", json={**GOOD, "email": None, "phone": "03001234567"}).status_code == 202
    assert sent.await_args.kwargs["phone"] == "03001234567"


def test_five_per_hour_then_429(api):
    with patch.object(notifications, "contact_message", new=AsyncMock()):
        codes = [api.post("/v1/public/contact", json=GOOD).status_code for _ in range(6)]
    assert codes == [202] * 5 + [429]


def test_the_spam_trap_is_answered_the_same_and_nothing_is_kept(api):
    with patch.object(notifications, "contact_message", new=AsyncMock()) as sent:
        answer = api.post("/v1/public/contact", json={**GOOD, "website": "http://spam.example"})
    assert answer.status_code == 202 and "Thanks" in answer.json()["message"]
    sent.assert_not_called()


def test_when_it_cant_be_stored_the_sender_is_told_to_try_again(api):
    with patch.object(notifications, "_record", side_effect=RuntimeError("db down")):
        answer = api.post("/v1/public/contact", json=GOOD)
    assert answer.status_code == 503 and "WhatsApp" in answer.json()["detail"]
