"""Change the customer's contact details (mobile, email) on a booking before payment (travelian#56, part 1).

Contact details are Travelian's own record: changing them never touches the airline hold, so there is no re-hold and the
price stays the same. Names, dates of birth and passports are not changed here (they need the supplier cancel and
re-hold, which waits for gateway#44 / #48).

Website (the guest, proven by the booking's private access token, never by reference + contact): only on a website
booking, and only while it is unpaid and in an explicitly open state (fail closed: any other or unknown state is
refused). The old email is told about the change. Staff may change it at any time. Every change is logged on the
booking (who, when, masked old -> new).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event

logger = logging.getLogger(__name__)

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_CODE = re.compile(r"^\+\d{1,3}$")
PHONE = re.compile(r"^\d{6,15}$")
# Fail closed: the website may change contact details only in these booking states, and only while nothing is paid.
WEBSITE_OPEN_STATES = frozenset({"held", "hold_pending", "requested"})
WEBSITE_PAYMENT_STATES = frozenset({"unpaid"})


class ContactRefused(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass(frozen=True)
class Contact:
    email: str
    phone_code: str
    phone: str


@dataclass(frozen=True)
class Changed:
    """The saved contact, what changed ("email", "phone"), and the old email (to tell it about the change)."""
    contact: Contact
    fields: tuple[str, ...] = field(default_factory=tuple)
    old_email: str = ""
    booking_id: str = ""


def clean(email: str | None, phone_code: str, phone: str) -> tuple[str | None, str, str]:
    """Validated values (email None: keep the one on the booking). Raises ContactRefused (422)."""
    email = (email or "").strip() or None
    phone_code = (phone_code or "").strip()
    phone = "".join(ch for ch in (phone or "") if not ch.isspace())
    if email is not None and (len(email) > 254 or not EMAIL.match(email)):
        raise ContactRefused(422, "Enter a valid email address, e.g. name@example.com.", "invalid_email")
    if not PHONE_CODE.match(phone_code):
        raise ContactRefused(422, "Choose the country code, e.g. +92.", "invalid_phone_code")
    if not PHONE.match(phone):
        raise ContactRefused(422, "Enter the mobile number in digits (6 to 15), e.g. 3001234567.", "invalid_phone")
    return email, phone_code, phone


def website_may_change(booking_status: str | None, payment_status: str | None, channel: str | None) -> bool:
    """Allow-list only: a website booking, unpaid, in an open state. Anything else (part paid, paid, cancelled, expired,
    released, issued, unknown or missing) is refused."""
    return channel == "website" and payment_status in WEBSITE_PAYMENT_STATES and booking_status in WEBSITE_OPEN_STATES


def _masked_email(value: str | None) -> str | None:
    return None if not value else (value[0] + "***" + value[value.index("@"):] if "@" in value else "***")


def _masked_phone(code: str | None, phone: str | None) -> str | None:
    return None if not phone else f"{code or ''} ***{phone[-3:]}"


def change(reference: str, *, email: str | None, phone_code: str, phone: str, actor_user_id: str | None, website: bool) -> Changed:
    """Save the new contact details on the booking `reference` and log it. `website`: the guest (already proven by the
    booking's access token); refused unless website_may_change. Unchanged details are not logged again."""
    email, phone_code, phone = clean(email, phone_code, phone)
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT b.id, b.channel, b.booking_status, b.payment_status, c.email, c.phone_code, c.phone"
            " FROM bookings b JOIN booking_customers c ON c.booking_id = b.id WHERE b.reference = :r FOR UPDATE OF b, c"),
            {"r": reference.strip().upper()}).mappings().one_or_none()
        if row is None:
            raise ContactRefused(404, "Booking not found.", "not_found")
        if website and not website_may_change(row["booking_status"], row["payment_status"], row["channel"]):
            raise ContactRefused(409, "This booking can't be changed online any more. Contact Travelian to update your details.", "closed")
        new = Contact(email=email if email is not None else (row["email"] or ""), phone_code=phone_code, phone=phone)
        old = Contact(email=row["email"] or "", phone_code=row["phone_code"] or "", phone=row["phone"] or "")
        changed: list[str] = []
        if new.email.lower() != old.email.lower():
            changed.append("email")
        if (new.phone_code, new.phone) != (old.phone_code, old.phone):
            changed.append("phone")
        if not changed:
            return Changed(contact=new, booking_id=row["id"], old_email=old.email)
        connection.execute(text("UPDATE booking_customers SET email = :e, phone_code = :pc, phone = :p WHERE booking_id = :b"),
                           {"e": new.email, "pc": new.phone_code, "p": new.phone, "b": row["id"]})
        details: dict[str, object] = {"via": "website" if website else "back_office",
                                      "by": "customer (booking access token)" if website else "staff"}
        if "email" in changed:
            details["email"] = {"from": _masked_email(old.email), "to": _masked_email(new.email)}
        if "phone" in changed:
            details["phone"] = {"from": _masked_phone(old.phone_code, old.phone), "to": _masked_phone(new.phone_code, new.phone)}
        _event(connection, row["id"], actor_user_id, "customer_contact_changed", details)
    return Changed(contact=new, fields=tuple(changed), old_email=old.email, booking_id=row["id"])


def notify_old_contact(reference: str, changed: Changed) -> tuple[bool, str]:
    """Tell the OLD email that the contact details changed (a security notice; the new values are not shown). The
    outcome (sent, or why not, e.g. email sending switched off) is recorded on the booking. There is no SMS channel,
    so the old phone is not messaged. Never raises."""
    from app.services import customer_emails
    from app.services import email_templates as t

    if not changed.fields:
        return False, "nothing changed"
    try:
        to = (changed.old_email or "").strip()
        if not to:
            customer_emails._record(changed.booking_id, "contact_changed", False, "no customer email on the booking", None)
            return False, "no customer email on the booking"
        email = t.contact_changed(reference.strip().upper(), list(changed.fields))
        sent, why = customer_emails.ses_send(to, email)
        customer_emails._record(changed.booking_id, "contact_changed", sent, why, to)
        return sent, "sent" if sent else why
    except Exception as error:  # noqa: BLE001 - the change is saved; a failed notice is logged, never raised
        logger.error("Contact change notice failed", extra={"reference": reference, "error_type": type(error).__name__})
        return False, f"failed ({type(error).__name__})"
