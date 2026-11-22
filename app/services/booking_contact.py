"""Change the customer's contact details (mobile, email) on a booking before payment (travelian#56, part 1).

Contact details are Travelian's own record: changing them never touches the airline hold, so there is no re-hold and the
price stays the same. Names, dates of birth and passports are not changed here (they need the supplier cancel and
re-hold, which waits for gateway#44 / #48).

The website may change them only while the booking still waits for payment; staff may change them at any time. Every
change is logged on the booking (masked old and new values), like the staff email fix (#59).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_CODE = re.compile(r"^\+\d{1,3}$")
PHONE = re.compile(r"^\d{6,15}$")
# The website can change contact details only before payment and while the booking is still open.
CLOSED = {"cancelled", "expired", "released", "ticket_issued"}


class ContactRefused(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass(frozen=True)
class Contact:
    email: str
    phone_code: str
    phone: str


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


def _masked_email(value: str | None) -> str | None:
    return None if not value else (value[0] + "***" + value[value.index("@"):] if "@" in value else "***")


def _masked_phone(code: str | None, phone: str | None) -> str | None:
    return None if not phone else f"{code or ''} ***{phone[-3:]}"


def change(reference: str, *, email: str | None, phone_code: str, phone: str, actor_user_id: str | None, website: bool) -> Contact:
    """Save the new contact details on the booking `reference` and log it. `website`: the customer, refused once the
    booking is paid, issued or closed. Unchanged details are not logged again."""
    email, phone_code, phone = clean(email, phone_code, phone)
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT b.id, b.booking_status, b.payment_status, b.issuance_status, c.email, c.phone_code, c.phone"
            " FROM bookings b JOIN booking_customers c ON c.booking_id = b.id WHERE b.reference = :r FOR UPDATE OF c"),
            {"r": reference.strip().upper()}).mappings().one_or_none()
        if row is None:
            raise ContactRefused(404, "Booking not found.", "not_found")
        if website and (row["payment_status"] == "paid" or row["booking_status"] in CLOSED or row["issuance_status"] == "issued"):
            raise ContactRefused(409, "This booking can't be changed online any more. Contact Travelian to update your details.", "closed")
        new = Contact(email=email if email is not None else (row["email"] or ""), phone_code=phone_code, phone=phone)
        old = Contact(email=row["email"] or "", phone_code=row["phone_code"] or "", phone=row["phone"] or "")
        if new == old:
            return new
        connection.execute(text("UPDATE booking_customers SET email = :e, phone_code = :pc, phone = :p WHERE booking_id = :b"),
                           {"e": new.email, "pc": new.phone_code, "p": new.phone, "b": row["id"]})
        details: dict[str, object] = {"via": "website" if website else "back_office"}
        if new.email.lower() != old.email.lower():
            details["email"] = {"from": _masked_email(old.email), "to": _masked_email(new.email)}
        if (new.phone_code, new.phone) != (old.phone_code, old.phone):
            details["phone"] = {"from": _masked_phone(old.phone_code, old.phone), "to": _masked_phone(new.phone_code, new.phone)}
        _event(connection, row["id"], actor_user_id, "customer_contact_changed", details)
    return new
