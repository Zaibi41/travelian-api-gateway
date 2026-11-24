"""Saved people (customers and travelers): requests and responses. Back-office reads carry the stored values in
full (document numbers, dates of birth, phones and emails); the document expiry also comes with a validity status."""

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.booking_common import BookingMoney, BookingStatuses

_NAME = r"^[A-Za-z][A-Za-z '\-]{0,24}$"
Title = Literal["MR", "MRS", "MISS", "MASTER"]
IdType = Literal["PSPT", "NATI"]


class SavedDocument(BaseModel):
    id: str
    id_type: IdType
    document_country: str
    document_number: str
    document_expiry: date | None = None
    document_issue_date: date | None = Field(None, description="Optional; documents saved before it existed have none.")
    document_validity: Literal["valid", "expiring", "expired"] | None = None


class SavedPersonSummary(BaseModel):
    id: str
    title: Title | None = None
    first_name: str
    last_name: str
    date_of_birth: date | None = None
    nationality: str | None = None
    phone: str | None = Field(None, description="With country code, e.g. +92 3001234567.")
    email: str | None = None
    documents: list[SavedDocument] = []
    last_booking_at: datetime | None = None
    match: Literal["document", "contact", "name"] | None = Field(None, description="Search only: what matched.")


class SavedPersonBooking(BaseModel):
    """One booking of a saved person's history. Cost and profit only with the cost and margin permission."""

    booking_id: str
    created_at: datetime
    role: Literal["customer", "traveler"]
    pnr: str | None = Field(None, description="Airline PNR; a group booking's only with the supplier names permission.")
    product: str | None = Field(None, description="flight or group (also hotel / umrah).")
    route: str | None = Field(None, description='Airports in travel order, e.g. "LHE → DXB → LHE". Omitted when the flights were not recorded.')
    departure_local: str | None = Field(None, description="First flight's local departure, e.g. 2026-11-19T00:25.")
    airline_code: str | None = Field(None, description="First flight's airline (IATA), for the logo (#75).")
    airline_name: str | None = Field(None, description="First flight's airline name (the airline flown, not the supplier).")
    flight_number: str | None = Field(None, description="Flight number(s) in travel order, e.g. 'PA401 / PA402'.")
    statuses: BookingStatuses | None = None
    ticket_numbers: list[str] = []
    selling_price: BookingMoney | None = None
    supplier_cost: BookingMoney | None = Field(None, description="Cost and margin permission only.")
    profit: BookingMoney | None = Field(None, description="Selling price minus supplier cost. Cost and margin permission only.")
    cost_unknown: bool | None = Field(None, description="Cost and margin permission only: the supplier cost was not recorded.")


class SavedPersonDetail(SavedPersonSummary):
    merged_into_id: str | None = None
    bookings: list[SavedPersonBooking] = []
    can_manage: bool = Field(False, description="May create, edit and change documents (manage_people).")
    can_merge: bool = Field(False, description="May merge people (merge_people).")


class SavedPersonSearch(BaseModel):
    results: list[SavedPersonSummary]
    can_manage: bool = Field(False, description="The caller may create, edit and change documents (manage_people).")
    can_merge: bool = Field(False, description="The caller may merge people (merge_people).")


class DocumentInput(BaseModel):
    id_type: IdType
    document_country: str = Field(pattern=r"^[A-Z]{3}$")
    document_number: str = Field(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9 \-]+$")
    document_expiry: date | None = None
    document_issue_date: date | None = Field(None, description="Optional issue date.")

    @model_validator(mode="after")
    def rules(self):
        if self.id_type == "PSPT" and self.document_expiry is None:
            raise ValueError("A passport needs its expiry date.")
        if self.document_issue_date and self.document_issue_date > date.today():
            raise ValueError("The issue date can't be in the future.")
        if self.document_issue_date and self.document_expiry and self.document_issue_date >= self.document_expiry:
            raise ValueError("The issue date must be before the expiry date.")
        if self.id_type == "NATI" and self.document_country == "PAK" and not re.fullmatch(r"\d{13}", re.sub(r"\D", "", self.document_number)):
            raise ValueError("A Pakistani national ID (CNIC or B-form) is 13 digits.")
        return self


BookingPersonRole = Literal["customer", "traveler"]


class DuplicateChoice(BaseModel):
    choice: Literal["add_to_existing", "keep_separate"]
    person_id: str | None = Field(None, min_length=8, max_length=36,
                                  description="The possible duplicate the choice is about (required for add_to_existing).")


class SavedPersonCreate(BaseModel):
    title: Title | None = None
    first_name: str = Field(pattern=_NAME)
    last_name: str = Field(pattern=_NAME)
    date_of_birth: date | None = None
    nationality: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    phone_code: str | None = Field(None, pattern=r"^[1-9]\d{0,3}$")
    phone: str | None = Field(None, pattern=r"^\d{5,10}$")
    email: str | None = Field(None, max_length=50, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    document: DocumentInput | None = None
    duplicate_choice: DuplicateChoice | None = Field(None, description="Send after a possible_duplicate answer.")


class SavedPersonUpdate(BaseModel):
    title: Title | None = None
    first_name: str | None = Field(None, pattern=_NAME)
    last_name: str | None = Field(None, pattern=_NAME)
    date_of_birth: date | None = None
    nationality: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    phone_code: str | None = Field(None, pattern=r"^[1-9]\d{0,3}$")
    phone: str | None = Field(None, pattern=r"^\d{5,10}$")
    email: str | None = Field(None, max_length=50, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SavedPersonCreated(BaseModel):
    person: SavedPersonDetail
    outcome: Literal["created", "existing", "added_to_existing"]


class MergeRequest(BaseModel):
    merge_into_person_id: str = Field(min_length=8, max_length=36)
    reason: str = Field(min_length=5, max_length=500)


class SavedPersonEvent(BaseModel):
    action: str
    actor_user_id: str | None = None
    at: datetime
    details: dict = Field(description="Ids and field names only.")


class SaveOutcomeView(BaseModel):
    """What a hold did with saved people. Saving never fails the hold."""

    role: Literal["customer", "traveler"]
    position: int | None = Field(None, description="Traveler position in the request.")
    status: Literal["saved", "linked", "updated", "needs_choice", "conflict", "not_saved", "skipped"]
    person_id: str | None = None
    document_id: str | None = None
    possible_duplicate: SavedPersonSummary | None = Field(None, description="needs_choice: the saved person this may be.")
    reason: str | None = None
    code: str | None = None
