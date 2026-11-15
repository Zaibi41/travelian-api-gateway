"""Group Ticketing holds (admin): hold seats, read the hold, re-check the price, release, issue.

Adults only. The price is not known before the hold: the hold answers with it, and it is checked
again before issuing. Supplier names are only in `supplier`, for viewers with the supplier names
permission; `stock` ("supplier" or "owned") is for everyone.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.live_flights import HoldContact, HoldPassenger
from app.schemas.saved_people import SaveOutcomeView
from app.schemas.group_ticketing import GroupSource
from app.services.group_ticketing_view import GroupLegView, GroupMoney

MAX_ADULTS = 9


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


class GroupHoldContact(BaseModel):
    """The customer card, typed per booking (or a saved customer by customer_id)."""

    full_name: str | None = Field(None, max_length=60, description="Given name(s) and surname; the last word is the surname.")
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
        if self.full_name and self.full_name.strip():
            values["first_name"], values["last_name"] = split_name(self.full_name)
        elif self.customer_id is None:
            raise ContactError("full_name", "Enter the customer's full name.")
        if self.phone and self.phone.strip():
            values["phone_code"], values["phone"] = split_phone(self.phone)
        elif self.customer_id is None:
            raise ContactError("phone", "Enter the customer's phone number.")
        return HoldContact.model_validate(values)


class GroupHoldPassenger(HoldPassenger):
    """A Flights passenger plus the traveler card's gender (checked against the title; not stored)."""

    gender: Literal["M", "F"] | None = Field(None, description="M or F; must agree with the title (Mr: M; Mrs or Miss: F).")


class GroupHoldRequest(BaseModel):
    ticket_id: UUID = Field(description="The group fare (GroupTicketView.id) from the search results.")
    adults: int = Field(ge=1, le=MAX_ADULTS, description="Adults only; must equal the number of passengers.")
    contact: GroupHoldContact
    passengers: list[GroupHoldPassenger] = Field(min_length=1, max_length=MAX_ADULTS, description="One adult (ADT) per seat.")
    selling_price_per_adult: GroupMoney | None = Field(None, description="What the customer pays per adult, in whole rupees, not below the "
                                                                         "supplier cost. Default: supplier price + the group markup (0 when unset). "
                                                                         "Changing it needs the record payments permission.")


class GroupHoldFlight(BaseModel):
    """The first flight's summary, plus every flight as held (outbound, then return) in `legs`."""

    airline: str | None = None
    sector: str | None = None
    origin: str | None = None
    destination: str | None = None
    departure_date: str | None = None
    departure_time: str | None = None
    logo_url: str | None = None
    baggage: str | None = None
    legs: list[GroupLegView] = Field(default_factory=list, description="Every flight as held, outbound then return.")


class GroupHoldCustomer(BaseModel):
    name: str
    phone: str | None = None
    email: str | None = None


class GroupHoldTraveler(BaseModel):
    position: int
    title: str
    first_name: str
    last_name: str
    id_type: str
    document_number_masked: str | None = None
    date_of_birth_masked: str
    nationality: str | None = None
    document_country: str | None = None
    document_expiry_masked: str | None = None
    ticket_number: str | None = None


class GroupHoldPayment(BaseModel):
    id: str
    method: Literal["bank_transfer", "jazzcash", "1link"]
    received_into: str
    amount: GroupMoney
    reference: str
    received_at: datetime


GroupHoldStatus = Literal["held", "expired", "released", "processing", "issued", "unknown", "not_recorded"]


class GroupHeldPriceChange(BaseModel):
    """The supplier held the seats at a different cost than shown before the hold. The selling price stays the staff's;
    payments wait until the change is confirmed, which is refused while the selling price is below the new cost."""

    selling_total: GroupMoney = Field(description="What the customer pays (unchanged by the supplier's change).")
    below_cost: bool = Field(description="The selling price is below the new supplier cost: set a higher selling price before confirming.")
    shown_cost: GroupMoney | None = Field(None, description="Supplier cost shown before the hold. Only with the cost and margin permission.")
    held_cost: GroupMoney | None = Field(None, description="Supplier cost held at. Only with the cost and margin permission.")
    difference: GroupMoney | None = Field(None, description="held_cost - shown_cost. Only with the cost and margin permission.")
    profit: GroupMoney | None = Field(None, description="selling_total - held_cost. Only with the cost and margin permission.")


class GroupHoldView(BaseModel):
    booking_id: str = Field(description="Travelian reference, e.g. TRV-GRP-AB12CD.")
    pnr: str | None = Field(None, description="The supplier's PNR. Only with the supplier names permission.")
    status: GroupHoldStatus = Field(description="held; expired (time limit passed, can't be held again: search again); released; "
                                                "processing (issuing); issued; unknown (issuing outcome unclear); not_recorded (the supplier held "
                                                "but the booking couldn't be completed; only viewers with supplier access can open it).")
    hold_expires_at: datetime | None = Field(None, description="Travelian's own time limit: hold creation + GROUP_HOLD_MINUTES.")
    adults: int
    flight: GroupHoldFlight
    stock: Literal["supplier", "owned"]
    supplier: str | None = Field(None, description="Only with the supplier names permission.")
    group_source: GroupSource | None = Field(None, description="Provider, supplier PNR, group and ticket IDs, as copied from the listing "
                                                               "when the hold was created. Only with the supplier names permission.")
    price_per_adult: GroupMoney = Field(description="Selling price per adult, from the hold (or the accepted new price).")
    total: GroupMoney
    amount_paid: GroupMoney
    amount_due: GroupMoney
    payment_status: Literal["unpaid", "part_paid", "paid"]
    customer: GroupHoldCustomer | None = None
    travelers: list[GroupHoldTraveler] = Field(default_factory=list, description="Masked documents and dates of birth.")
    payments: list[GroupHoldPayment] = Field(default_factory=list)
    can_record_payments: bool = Field(False, description="This viewer may record a payment now (the endpoint still checks).")
    can_issue_tickets: bool = Field(False, description="This viewer may issue now: held, not expired, nothing due, provider supports it (the endpoint still checks).")
    issue_unavailable_reason: str | None = Field(None, description="Why Issue tickets is off for this booking's provider, when it is.")
    price_change: GroupHeldPriceChange | None = Field(None, description="Set while the held price differs from the price shown and the customer "
                                                                          "hasn't agreed yet (POST .../accept-held-price).")
    supplier_cost: GroupMoney | None = Field(None, description="Supplier cost total. Only with the cost and margin permission.")
    profit: GroupMoney | None = Field(None, description="Total minus supplier cost. Only with the cost and margin permission.")
    can_set_selling_price: bool = Field(False, description="This viewer may change the selling price now: on hold, nothing paid, "
                                                         "Super Admin or record payments (the endpoint still checks).")
    can_release: bool = Field(False, description="Release hold can be used now: held, and the provider has a release call (the endpoint still checks).")
    supplier_release_failed: bool | None = Field(None, description="The seats could not be released at the supplier after the hold ended "
                                                                   "(being retried, or given up). Only with the supplier names permission.")
    hold_time_note: str | None = Field(None, description="Under the countdown: 'Hold time estimated (45 min default)' or 'Hold time from "
                                                        "supplier'. Only with the supplier names permission.")
    supplier_notice: str | None = Field(None, description="What happens to the seats at the supplier, e.g. 'Cancelled at the supplier'. Only with the supplier names permission.")
    saved_people: list[SaveOutcomeView] | None = Field(None, description="Only in the hold answer: what happened to the saved customer and travelers.")


class GroupPriceCheck(BaseModel):
    """The supplier cost checked again (read-only). The selling price never changes here; only the cost can move."""

    changed: bool = Field(description="The supplier cost differs from the one on the booking.")
    selling_total: GroupMoney = Field(description="What the customer pays (unchanged).")
    below_cost: bool = Field(description="The new supplier cost is above the selling price.")
    accepted_supplier_cost: GroupMoney = Field(description="Send this back as accepted_supplier_cost to issue at the new cost.")
    previous_supplier_cost: GroupMoney | None = Field(None, description="Only with the cost and margin permission.")
    new_supplier_cost: GroupMoney | None = Field(None, description="Only with the cost and margin permission.")
    difference: GroupMoney | None = Field(None, description="new - previous supplier cost. Only with the cost and margin permission.")
    profit: GroupMoney | None = Field(None, description="selling_total - new supplier cost. Only with the cost and margin permission.")


class GroupIssueRequest(BaseModel):
    accepted_supplier_cost: GroupMoney | None = Field(
        None, description="After 'supplier_cost_changed': the new supplier cost the issuer confirmed. Refused if it moved again.")
    accept_below_cost: bool = Field(False, description="Required when the new supplier cost is above the selling price.")


class GroupIssueResult(BaseModel):
    result: Literal["issued", "not_issued", "unknown"]
    message: str
    hold: GroupHoldView


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))
