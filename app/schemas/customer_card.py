"""The customer card used by both admin hold forms (Flights and Group Ticketing): given name and surname (or, from
older clients, one full name), one phone field with its country code, email, and the customer's document. The gateway turns it into the Flights contact shape (first and last
name, phone code and number), so the browser never splits names or phone numbers."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ContactError(ValueError):
    """A customer field that can't be read: `field` is the request field to point at."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def split_name(full_name: str) -> tuple[str, str]:
    """'Ali Raza' -> ('Ali', 'Raza'); the last word is the surname."""
    words = full_name.split()
    if len(words) < 2:
        raise ContactError("full_name", "Enter the customer's full name: given name and surname.")
    return " ".join(words[:-1]), words[-1]


def split_phone(text: str) -> tuple[str, str]:
    """'+92 300 1234567', '0092 300 1234567' or '0300 1234567' (Pakistan) -> ('92', '3001234567');
    '+971 50 123 4567' -> ('971', '501234567'): the country code is what follows '+' up to the first space."""
    raw = text.strip()
    compact = re.sub(r"[\s\-().]", "", raw)
    if compact.startswith("00"):
        compact, raw = "+" + compact[2:], "+" + raw.lstrip()[2:].lstrip()
    if compact.startswith("+92"):
        return "92", compact[3:].lstrip("0")
    if re.fullmatch(r"0\d{9,10}", compact):
        return "92", compact[1:]
    if compact.startswith("+") and " " in raw:
        code, rest = raw.lstrip("+").split(None, 1)
        return code, re.sub(r"\D", "", rest)
    raise ContactError("phone", "Enter the phone number with its country code, e.g. +92 300 1234567.")


class CustomerCard(BaseModel):
    """The customer card, typed per booking (or a saved customer by customer_id)."""

    first_name: str | None = Field(None, max_length=60, description="Given name(s), as on the document.")
    last_name: str | None = Field(None, max_length=60, description="Surname, as on the document. Airlines need both parts.")
    full_name: str | None = Field(None, max_length=60, description="Older clients: given name(s) and surname; the last word is the surname.")
    phone: str | None = Field(None, max_length=24, description="With country code, e.g. +92 300 1234567. A Pakistani number may start with 0.")
    email: str | None = Field(None, max_length=50)
    id_type: Literal["PSPT", "NATI"] | None = Field(None, description="The customer's document: passport or CNIC.")
    document_number: str | None = Field(None, max_length=20)
    customer_id: str | None = Field(None, min_length=8, max_length=36, description="A saved customer; typed values override for this booking.")
    save_for_next_time: bool = Field(True, description="Save or update the customer after the hold. Never blocks the hold.")

    @field_validator("document_number")
    @classmethod
    def compact_document(cls, value: str | None) -> str | None:
        value = re.sub(r"[\s-]", "", value or "").upper()
        return value or None

    def to_hold_contact(self) -> HoldContact:
        """The Flights contact shape (first/last name, phone code and number). Raises ContactError."""
        values: dict[str, object] = {"customer_id": self.customer_id, "save_for_next_time": self.save_for_next_time,
                                     "email": (self.email or "").strip() or None}
        first, last = (self.first_name or "").strip(), (self.last_name or "").strip()
        if first or last:
            if self.customer_id is None and not first:
                raise ContactError("first_name", "Enter the customer's given name.")
            if self.customer_id is None and not last:
                raise ContactError("last_name", "Enter the customer's surname.")
            values["first_name"], values["last_name"] = first or None, last or None
        elif self.full_name and self.full_name.strip():
            values["first_name"], values["last_name"] = split_name(self.full_name)
        elif self.customer_id is None:
            raise ContactError("first_name", "Enter the customer's given name and surname.")
        if self.phone and self.phone.strip():
            values["phone_code"], values["phone"] = split_phone(self.phone)
        elif self.customer_id is None:
            raise ContactError("phone", "Enter the customer's phone number.")
        if self.customer_id is None and not values["email"]:
            raise ContactError("email", "Enter the customer's email.")
        from app.schemas.live_flights import HoldContact  # late: live_flights imports this module

        return HoldContact.model_validate(values)
