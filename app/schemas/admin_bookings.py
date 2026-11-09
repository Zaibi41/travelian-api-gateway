"""Admin Bookings list and Booking detail responses.

Field visibility (spec v3.0 D23): `provider` (supplier names), `supplier_cost`,
`margin` and `supplier_payment` are LEFT OUT of the response (not null) for
callers without the matching permission. Routes serialize with
response_model_exclude_unset, so only fields that were set are sent.
Money is integer minor units with its currency code (bookings can be in PKR or
other currencies).
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.admin_dashboard import FilterOption


Product = Literal["flight", "group", "hotel", "umrah"]
BookingStatus = Literal["held", "pending_payment", "payment_received", "confirmed", "ticket_issued", "cancelled", "expired"]
PaymentStatus = Literal["paid", "unpaid", "part_paid"]
PaymentMethod = Literal["bank_transfer", "jazzcash", "1link"]
IssuanceStatus = Literal["not_issued", "requested", "processing", "issued", "unknown"]
Card = Literal["confirmed", "on_hold", "cancelled", "unpaid", "expiring_soon", "awaiting_issuance", "hold_deadlines"]
DateType = Literal["created"]  # travel date is not stored yet
DetailSection = Literal["itinerary", "passengers", "payments", "tickets", "servicing", "audit"]

DataLabel = Literal["Sample data", "UAT test data"]

SOURCE = "gateway_bookings"
SAMPLE_LABEL = "Sample data"


class BookingMoney(BaseModel):
    amount_minor: int = Field(description="Amount in minor units (paisa for PKR).")
    currency: str = Field(min_length=3, max_length=3)


class BookingStatuses(BaseModel):
    booking: BookingStatus
    payment: PaymentStatus
    issuance: IssuanceStatus


class BookingListCards(BaseModel):
    """Counts over the rows in the current filters (all but `card`). A card is omitted when its data doesn't exist yet."""

    all_bookings: int
    confirmed: int
    on_hold: int
    cancelled: int
    unpaid: int
    expiring_soon: int
    expiring_window_hours: int
    awaiting_issuance: int | None = Field(None, description="Flights tab only.")
    hold_deadlines: int | None = Field(None, description="Flights tab only: open holds not yet expired.")


class BookingListItem(BaseModel):
    booking_id: str
    pnr: str | None = None
    product: Product
    customer_or_agency: str | None = Field(None, description="Agency name for agency bookings, otherwise the customer's name when saved.")
    provider: str | None = Field(None, description="Supplier/source. Omitted without the supplier names permission.")
    statuses: BookingStatuses
    amount: BookingMoney
    created_at: datetime | None = None
    expires_at: datetime | None = None
    sample: bool = Field(description="True for test rows (one-time import or supplier UAT).")
    data_label: DataLabel | None = Field(None, description="Label for test rows.")


class BookingListFilterOptions(BaseModel):
    agencies: list[FilterOption]
    providers: list[FilterOption] | None = Field(None, description="Omitted without the supplier names permission.")


class BookingList(BaseModel):
    source: Literal["gateway_bookings"] = SOURCE
    contains_sample_data: bool
    sample_label: Literal["Sample data"] = SAMPLE_LABEL
    cards: BookingListCards
    filter_options: BookingListFilterOptions
    total: int
    limit: int
    offset: int
    items: list[BookingListItem]


class BookingFare(BaseModel):
    billable_price: BookingMoney
    supplier_cost: BookingMoney | None = Field(None, description="Omitted without the cost and margin permission, or when unknown.")
    margin: BookingMoney | None = Field(None, description="Omitted without the cost and margin permission, or when unknown.")


class SupplierPayment(BaseModel):
    """The Supplier payment tab. Only sent with the cost and margin permission."""

    amount_due: BookingMoney | None = None
    hold_deadline: datetime | None = None


class BookingCustomer(BaseModel):
    """Who pays and is contacted (spec 8)."""

    first_name: str
    last_name: str
    phone: str = Field(description="With country code, e.g. +92 3001234567.")
    email: str


class BookingPassenger(BaseModel):
    """A traveler. Date of birth and document number are masked; the full values need a logged reveal."""

    position: int = Field(description="Order in the booking; used by the reveal endpoint.")
    type: Literal["ADT", "CHD", "INF"]
    title: str
    first_name: str
    last_name: str
    date_of_birth_masked: str = Field(description="Year only, e.g. 1990-••-••.")
    id_type: Literal["PSPT", "NATI"]
    document_number_masked: str | None = Field(None, description="Last 3 characters only.")
    document_country: str | None = None
    nationality: str | None = None
    document_expiry_masked: str | None = Field(None, description="Year only, e.g. 2031-••-••. The full date needs a logged reveal.")
    document_validity: Literal["valid", "expiring", "expired"] | None = Field(
        None, description="From the expiry date today: expired, expiring within 6 months, or valid. Omitted without an expiry.")
    infant_of_position: int | None = Field(None, description="For an infant: the position of the adult it travels with.")
    ticket_number: str | None = Field(None, description="Set when the ticket is issued.")


class BookingAuditEntry(BaseModel):
    action: str
    actor_user_id: str | None = None
    actor_role: str | None = Field(None, description="The actor's role. User names aren't stored in the gateway yet.")
    at: datetime
    details: dict[str, Any] = Field(description="Whitelisted, no personal data.")


class BookingPayment(BaseModel):
    """A customer payment (Payments tab)."""

    id: str
    amount: BookingMoney
    method: PaymentMethod
    received_into: str = Field(description="The Travelian account the money was received into.")
    reference: str = Field(description="The payer's transaction reference.")
    received_at: datetime
    recorded_by_user_id: str | None = None
    recorded_by_role: str | None = None
    recorded_at: datetime


class RecordPaymentRequest(BaseModel):
    amount: BookingMoney
    method: PaymentMethod
    received_into: str = Field(min_length=2, max_length=120)
    reference: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ./_-]*$")
    received_at: datetime = Field(description="When the money was received. Not in the future.")


class RecordPaymentResponse(BaseModel):
    payment: BookingPayment
    payment_status: PaymentStatus


class IssuanceChecklist(BaseModel):
    """What stands between this booking and a ticket. Supplier fields only with the cost and margin permission."""

    issuance_status: IssuanceStatus
    blockers: list[str] = Field(description="Why issuing (or requesting) isn't possible now; empty when it is.")
    full_payment_recorded: bool
    amount_paid: BookingMoney
    amount_due: BookingMoney
    hold_expires_at: datetime | None = None
    ticketing_enabled: bool
    can_request: bool
    can_issue: bool = Field(description="This viewer may issue now: permission, ticketing on, no blockers.")
    supplier_amount: BookingMoney | None = Field(None, description="Only with the cost and margin permission.")
    supplier_balance: BookingMoney | None = Field(None, description="Only with the cost and margin permission; omitted if the supplier didn't answer.")
    margin: BookingMoney | None = Field(None, description="Only with the cost and margin permission.")


class IssuanceRequest(BaseModel):
    note: str | None = Field(None, max_length=500)


class TicketNumber(BaseModel):
    position: int = Field(ge=0, le=8)
    ticket_number: str = Field(pattern=r"^[A-Za-z0-9-]{6,32}$")


class IssuanceResolveRequest(BaseModel):
    outcome: Literal["issued", "not_issued"]
    reason: str = Field(min_length=5, max_length=500)
    ticket_numbers: list[TicketNumber] = Field(default_factory=list, description="Required for every traveler when outcome is issued.")


class IssuanceResult(BaseModel):
    result: Literal["issued", "not_issued", "unknown", "requested"]
    issuance_status: IssuanceStatus
    message: str
    tickets: list[TicketNumber] = []


class RevealRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=500, description="Why the full values are needed. Logged with the reveal.")


class RevealResponse(BaseModel):
    date_of_birth: date
    document_number: str | None = None
    document_expiry: date | None = None


class BookingDetail(BaseModel):
    source: Literal["gateway_bookings"] = SOURCE
    sample: bool
    sample_label: DataLabel | None = None
    booking_id: str
    pnr: str | None = None
    product: Product
    customer_or_agency: str | None = None
    provider: str | None = Field(None, description="Omitted without the supplier names permission.")
    statuses: BookingStatuses
    amount: BookingMoney
    deadline: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fare: BookingFare
    supplier_payment: SupplierPayment | None = Field(None, description="Omitted without the cost and margin permission.")
    customer: BookingCustomer | None = Field(None, description="Omitted when no customer was saved.")
    passengers: list[BookingPassenger] | None = Field(None, description="The Passengers tab. Omitted when no travelers were saved.")
    audit: list[BookingAuditEntry] | None = Field(None, description="The Audit tab. Omitted when there are no events.")
    can_reveal_identity: bool = Field(False, description="Whether this viewer may call the reveal endpoint (the endpoint still checks).")
    payments: list[BookingPayment] | None = Field(None, description="The Payments tab. Omitted when no payment is recorded.")
    amount_paid: BookingMoney | None = Field(None, description="Sum of recorded payments.")
    amount_due: BookingMoney | None = Field(None, description="Total less recorded payments.")
    can_record_payments: bool = Field(False, description="Whether this viewer may record a payment (the endpoint still checks).")
    can_issue_tickets: bool = Field(False, description="Whether this viewer may issue, verify and resolve (the endpoints still check).")
    sections_not_available: list[DetailSection] = Field(
        description="Tabs whose data the gateway doesn't record yet; the page shows them as not available."
    )
