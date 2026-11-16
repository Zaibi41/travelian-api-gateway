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

from app.schemas.customer_card import ContactError, CustomerCard, split_name, split_phone  # noqa: F401 - re-exported
from app.schemas.live_flights import HoldPassenger
from app.schemas.saved_people import SaveOutcomeView
from app.schemas.group_ticketing import GroupSource
from app.services.group_ticketing_view import GroupLegView, GroupMoney

MAX_ADULTS = 9


GroupHoldContact = CustomerCard  # the customer card, shared with the Flights hold (app.schemas.customer_card)


class GroupHoldPassenger(HoldPassenger):
    """A Flights passenger plus the traveler card's gender (checked against the title; not stored)."""

    gender: Literal["M", "F"] | None = Field(None, description="M or F; must agree with the title (Mr: M; Mrs or Miss: F).")


class GroupHoldRequest(BaseModel):
    ticket_id: UUID = Field(description="The group fare (GroupTicketView.id) from the search results.")
    adults: int = Field(ge=1, le=MAX_ADULTS, description="Adults only; must equal the number of passengers.")
    contact: GroupHoldContact
    passengers: list[GroupHoldPassenger] = Field(min_length=1, max_length=MAX_ADULTS, description="One adult (ADT) per seat.")
    fare_terms_acknowledged: bool = Field(False, description="Required (true): the customer has been told that this group fare is "
                                                              "non-refundable and non-changeable, and agrees. Stored with the booking "
                                                              "(who, when, the text shown). The hold is refused without it.")
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
    document_number: str | None = None
    date_of_birth: date
    nationality: str | None = None
    document_country: str | None = None
    document_expiry: date | None = None
    ticket_number: str | None = None


class GroupHoldPayment(BaseModel):
    id: str
    method: Literal["bank_transfer", "jazzcash", "1link"]
    received_into: str
    amount: GroupMoney
    reference: str
    received_at: datetime


GroupHoldStatus = Literal["held", "confirmed", "expired", "released", "processing", "issued", "unknown", "not_recorded"]
#: After payment: "Paid – awaiting final PNR" -> "Final PNR received" -> "Ticketed" (final PNR and every ticket number).
GroupTicketingStatus = Literal["awaiting_final_pnr", "final_pnr_received", "ticketed"]


class GroupHeldPriceChange(BaseModel):
    """The supplier held the seats at a different cost than shown before the hold. The selling price stays the staff's;
    payments wait until the change is confirmed, which is refused while the selling price is below the new cost."""

    selling_total: GroupMoney = Field(description="What the customer pays (unchanged by the supplier's change).")
    below_cost: bool = Field(description="The selling price is below the new supplier cost: set a higher selling price before confirming.")
    shown_cost: GroupMoney | None = Field(None, description="Supplier cost shown before the hold. Only with the cost and margin permission.")
    held_cost: GroupMoney | None = Field(None, description="Supplier cost held at. Only with the cost and margin permission.")
    difference: GroupMoney | None = Field(None, description="held_cost - shown_cost. Only with the cost and margin permission.")
    profit: GroupMoney | None = Field(None, description="selling_total - held_cost. Only with the cost and margin permission.")


class FareTermsView(BaseModel):
    """The stored acknowledgement that the fare is non-refundable and non-changeable."""

    text: str = Field(description="The exact text that was shown and ticked.")
    version: str = Field(description="The version of that text.")
    acknowledged_by: Literal["staff", "customer"] = Field(description="Who ticked it: a staff user for the customer, or the customer.")
    acknowledged_by_user_id: str | None = Field(None, description="The user who ticked it.")
    acknowledged_at: datetime


class GroupHoldView(BaseModel):
    booking_id: str = Field(description="Travelian reference, e.g. TRV-GRP-AB12CD.")
    pnr: str | None = Field(None, description="The supplier's PNR. Only with the supplier names permission.")
    status: GroupHoldStatus = Field(description="held; confirmed (paid, the final PNR is recorded); expired (time limit passed, can't be held again: search again); released; "
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
    travelers: list[GroupHoldTraveler] = Field(default_factory=list, description="Documents and dates of birth in full.")
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
    fare_terms: FareTermsView | None = Field(None, description="The non-refundable, non-changeable acknowledgement stored at the hold.")
    saved_people: list[SaveOutcomeView] | None = Field(None, description="Only in the hold answer: what happened to the saved customer and travelers.")
    final_pnr: str | None = Field(None, description="The airline's final PNR for this booking, recorded by staff after payment.")
    ticketing_status: GroupTicketingStatus | None = Field(None, description="After full payment: awaiting_final_pnr, final_pnr_received, ticketed.")
    can_edit_tickets: bool = Field(False, description="This viewer may record the final PNR and ticket numbers now (issue tickets permission, "
                                                      "paid in full; the endpoint still checks).")
    itinerary_available: bool = Field(True, description="The itinerary / e-ticket PDF and PNG can be downloaded: a group booking only once Ticketed.")


class GroupTicketNumber(BaseModel):
    position: int = Field(ge=0, description="The traveler's booking position.")
    ticket_number: str | None = Field(None, max_length=20, description="Empty clears it. Letters, digits and dashes, 6 to 20.")


class GroupTicketsRequest(BaseModel):
    """The final PNR and per-traveler ticket numbers, typed by staff from the airline / supplier."""

    final_pnr: str = Field(min_length=5, max_length=8, description="The airline's final PNR, 5 to 8 letters and digits.")
    tickets: list[GroupTicketNumber] = Field(default_factory=list, max_length=99)

    @field_validator("final_pnr")
    @classmethod
    def _pnr(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{5,8}", value):
            raise ValueError("Final PNR is 5 to 8 letters and digits.")
        return value

    @field_validator("tickets")
    @classmethod
    def _tickets(cls, tickets: list[GroupTicketNumber]) -> list[GroupTicketNumber]:
        numbers = []
        for ticket in tickets:
            value = (ticket.ticket_number or "").strip().upper().replace(" ", "")
            if value and not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{4,18}[A-Z0-9]", value):
                raise ValueError("A ticket number is 6 to 20 letters, digits and dashes.")
            ticket.ticket_number = value or None
            if value:
                numbers.append(value)
        if len(numbers) != len(set(numbers)):
            raise ValueError("Each traveler needs a different ticket number.")
        if len({ticket.position for ticket in tickets}) != len(tickets):
            raise ValueError("Each traveler appears once.")
        return tickets


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
