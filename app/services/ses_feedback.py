"""SES bounces and complaints through SNS (gateway#59).

SES publishes each bounce or complaint to an SNS topic; the topic posts it to POST /v1/webhooks/ses. Before anything is
read, the SNS message must be signed by AWS (SignatureVersion 1 = SHA1, 2 = SHA256, with the certificate at an
https://sns.<region>.amazonaws.com/... URL) and come from a topic listed in SES_SNS_TOPIC_ARNS. With that setting empty
(the default) every message is refused, so nothing happens until the topic is set up.

A notification names the SES message id; customer emails record theirs when sent, so the bounce lands on the booking
and the email it belongs to. Staff see "Contact the customer and fix the email address" on the booking.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.db import agencies
from app.db.supplier_bookings import _event

logger = logging.getLogger(__name__)

SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_CERTS: dict[str, bytes] = {}

# The fields SNS signs, in this order (AWS: "Verifying the signatures of Amazon SNS messages").
_SIGNED = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
    "UnsubscribeConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
}


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def allowed_topics() -> set[str]:
    return {arn.strip() for arn in (settings.ses_sns_topic_arns or "").split(",") if arn.strip()}


def aws_url(url: str | None) -> bool:
    """An https URL on an SNS host (certificates and subscribe links are only fetched from there)."""
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(SNS_HOST.match(parsed.hostname or "")) and not parsed.username and parsed.port in (None, 443)


def string_to_sign(message: dict[str, Any]) -> bytes:
    fields = _SIGNED.get(message.get("Type", ""))
    if fields is None:
        raise Refused(400, "Unknown SNS message type.")
    lines = []
    for name in fields:
        if name == "Subject" and "Subject" not in message:
            continue
        if message.get(name) is None:
            raise Refused(400, f"SNS message without {name}.")
        lines += [name, str(message[name])]
    return ("\n".join(lines) + "\n").encode()


def _certificate(url: str) -> bytes:
    if url not in _CERTS:
        response = httpx.get(url, timeout=5.0, follow_redirects=False)
        response.raise_for_status()
        if len(_CERTS) > 20:
            _CERTS.clear()
        _CERTS[url] = response.content
    return _CERTS[url]


def verify(message: dict[str, Any], fetch=None) -> None:
    """Refuse anything not signed by SNS for one of our topics. `fetch(url) -> PEM bytes` is for tests."""
    topics = allowed_topics()
    if not topics:
        raise Refused(403, "SES feedback isn't set up (SES_SNS_TOPIC_ARNS is empty).")
    if message.get("TopicArn") not in topics:
        raise Refused(403, "Unknown SNS topic.")
    url = message.get("SigningCertURL") or message.get("SigningCertUrl")
    if not aws_url(url) or not str(urlparse(url).path).endswith(".pem"):
        raise Refused(403, "The signing certificate isn't from Amazon SNS.")
    version = str(message.get("SignatureVersion"))
    if version not in ("1", "2"):
        raise Refused(403, "Unsupported SNS signature version.")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        certificate = x509.load_pem_x509_certificate((fetch or _certificate)(url))
        certificate.public_key().verify(base64.b64decode(message.get("Signature") or ""), string_to_sign(message), padding.PKCS1v15(),
                                        hashes.SHA1() if version == "1" else hashes.SHA256())  # noqa: S303 - SNS v1 is SHA1
    except Refused:
        raise
    except Exception:  # noqa: BLE001 - any failure is a refusal
        raise Refused(403, "The SNS signature doesn't match.") from None


def confirm_subscription(message: dict[str, Any], get=None) -> None:
    """A verified SubscriptionConfirmation for one of our topics: visit its SubscribeURL (an SNS URL) once."""
    url = message.get("SubscribeURL")
    if not aws_url(url):
        raise Refused(403, "The subscribe link isn't from Amazon SNS.")
    (get or (lambda link: httpx.get(link, timeout=5.0, follow_redirects=False).raise_for_status()))(url)
    logger.info("SES feedback topic subscribed", extra={"topic": message.get("TopicArn")})


def _masked(address: str | None) -> str | None:
    if not address:
        return None
    return address[0] + "***" + address[address.index("@"):] if "@" in address else "***"


def feedback(ses: dict[str, Any]) -> list[dict[str, Any]]:
    """The bounce or complaint records in one SES notification: [{type, bounce_type, recipient, at, message_id}]."""
    kind = (ses.get("notificationType") or ses.get("eventType") or "").lower()
    message_id = (ses.get("mail") or {}).get("messageId")
    if kind == "bounce":
        bounce = ses.get("bounce") or {}
        return [{"type": "bounce", "bounce_type": bounce.get("bounceType"), "bounce_sub_type": bounce.get("bounceSubType"),
                 "recipient": r.get("emailAddress"), "at": bounce.get("timestamp"), "message_id": message_id}
                for r in bounce.get("bouncedRecipients") or [{}]]
    if kind == "complaint":
        complaint = ses.get("complaint") or {}
        return [{"type": "complaint", "bounce_type": complaint.get("complaintFeedbackType"), "bounce_sub_type": None,
                 "recipient": r.get("emailAddress"), "at": complaint.get("timestamp"), "message_id": message_id}
                for r in complaint.get("complainedRecipients") or [{}]]
    return []


def record(items: list[dict[str, Any]]) -> list[str]:
    """Save each bounce or complaint on the booking whose customer email had that SES message id. Returns references."""
    saved = []
    with agencies.application_engine().begin() as connection:
        for item in items:
            if not item.get("message_id"):
                continue
            row = connection.execute(text(
                "SELECT e.booking_id, e.details ->> 'email' AS kind, b.reference FROM booking_events e JOIN bookings b ON b.id = e.booking_id"
                " WHERE e.action = 'customer_email_sent' AND e.details ->> 'message_id' = :m LIMIT 1"), {"m": item["message_id"]}).one_or_none()
            if row is None:
                logger.info("SES feedback for an email that isn't a booking email", extra={"feedback_type": item["type"]})
                continue
            _event(connection, row.booking_id, None, "customer_email_bounced", {
                "email": row.kind, "type": item["type"], "bounce_type": item.get("bounce_type"), "bounce_sub_type": item.get("bounce_sub_type"),
                "to": _masked(item.get("recipient")), "at": item.get("at") or datetime.now(timezone.utc).isoformat()})
            saved.append(row.reference)
            logger.warning("Customer email bounced", extra={"reference": row.reference, "feedback_type": item["type"],
                                                            "bounce_type": item.get("bounce_type")})
    return saved


def handle(body: bytes, fetch=None, get=None) -> dict[str, Any]:
    """One SNS POST: verify, then confirm a subscription or record the SES feedback."""
    try:
        message = json.loads(body)
    except ValueError:
        raise Refused(400, "Not JSON.") from None
    if not isinstance(message, dict):
        raise Refused(400, "Not an SNS message.")
    verify(message, fetch)
    if message["Type"] == "SubscriptionConfirmation":
        confirm_subscription(message, get)
        return {"result": "subscribed"}
    if message["Type"] != "Notification":
        return {"result": "ignored"}
    try:
        ses = json.loads(message.get("Message") or "{}")
    except ValueError:
        raise Refused(400, "The SES notification isn't JSON.") from None
    return {"result": "recorded", "bookings": record(feedback(ses))}
