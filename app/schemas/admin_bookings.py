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
from app.schemas.booking_common import BookingMoney, BookingStatus, BookingStatuses, IssuanceStatus, PaymentStatus  # noqa: F401 (re-exported)
from app.schemas.group_holds import FareTermsView


Product = Literal["flight", "group", "hotel", "umrah"]
PaymentMethod = Literal["bank_transfer", "jazzcash", "1link"]
#: A payment as shown: also PayFast (online, recorded only by the gateway from a verified PayFast result).
RecordedPaymentMethod = Literal["bank_transfer", "jazzcash", "1link", "payfast"]
Card = Literal["confirmed", "on_hold", "cancelled", "unpaid", "expiring_soon", "awaiting_issuance", "hold_deadlines", "payment_pending",
               "paid_not_issued"]
DateType = Literal["created"]  # travel date is not stored yet
DetailSection = Literal["itinerary", "passengers", "payments", "tickets", "servicing", "audit"]

DataLabel = Literal["Sample data", "UAT test data"]

SOURCE = "gateway_bookings"
SAMPLE_LABEL = "Sample data"


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
    paid_not_issued: int | None = Field(None, description="Flights tab only: paid and no ticket, needing staff (Paid, issuance pending "
                                                          "or Paid, not ticketed).")
    hold_deadlines: int | None = Field(None, description="Flights and Group Ticketing tabs: open holds not yet expired.")
    payment_pending: int | None = Field(
        None, description="Flights tab only: open holds not fully paid and not past their deadline (the dashboard's Payment pending).",
    )


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


class FarePerPassenger(BaseModel):
    passenger_type: Literal["ADT", "CHD", "INF"]
    count: int
    selling_each: BookingMoney = Field(description="What the customer pays per traveler of this type.")
    supplier_cost_each: BookingMoney | None = Field(None, description="Only with the cost and margin permission.")


class BookingFare(BaseModel):
    billable_price: BookingMoney
    per_passenger: list[FarePerPassenger] | None = Field(None, description="Selling price (and cost, with access) per passenger type. Omitted when no travelers were saved.")
    supplier_cost: BookingMoney | None = Field(None, description="Omitted without the cost and margin permission, or when unknown.")
    margin: BookingMoney | None = Field(None, description="Omitted without the cost and margin permission, or when unknown.")
    cost_unknown: bool | None = Field(None, description="True when no real supplier cost exists (none recorded, or an imported booking whose cost "
                                                        "was filled in as its total): show cost and profit as n/a. Only with the cost and margin permission.")


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
    """A traveler, with the full identity values as stored."""

    position: int = Field(description="Order in the booking.")
    type: Literal["ADT", "CHD", "INF"]
    title: str
    first_name: str
    last_name: str
    date_of_birth: date
    id_type: Literal["PSPT", "NATI"]
    document_number: str | None = None
    document_country: str | None = None
    nationality: str | None = None
    document_expiry: date | None = None
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
    method: RecordedPaymentMethod
    received_into: str = Field(description="The Travelian account the money was received into.")
    reference: str = Field(description="The payer's transaction reference.")
    received_at: datetime
    recorded_by_user_id: str | None = None
    recorded_by_role: str | None = None
    recorded_at: datetime


class PaymentAccount(BaseModel):
    """One of Travelian's receiving accounts (where customer payments arrive)."""

    id: str
    method: PaymentMethod
    label: str


class PaymentAccountList(BaseModel):
    accounts: list[PaymentAccount] = Field(description="Active accounts, by method then name. Empty until an administrator adds them.")


class ReceivingAccount(BaseModel):
    """A receiving account as Finance > Accounts & Ledgers manages it (active and inactive)."""

    id: str
    method: PaymentMethod
    label: str
    active: bool
    bank_name: str | None = None
    account_title: str | None = None
    account_number_masked: str | None = Field(None, description="Short form for lists (last 4 digits).")
    account_number: str | None = Field(None, description="The full account number customers pay into (D40: never masked).")
    iban: str | None = Field(None, description="IBAN, upper case, no spaces.")
    qr_asset_key: str | None = Field(None, description="S3 object key of the QR image.")
    qr_url: str | None = Field(None, description="Public URL of the QR image (S3).")


class UpdateReceivingAccountRequest(BaseModel):
    bank_name: str | None = Field(None, max_length=1000)
    account_title: str | None = Field(None, max_length=1000)
    account_number: str | None = Field(None, max_length=60, description="The full account number, stored and shown in full (D40).")
    iban: str | None = Field(None, max_length=60, description="IBAN; spaces are removed.")
    qr_asset_key: str | None = Field(None, max_length=300, description="From POST /v1/admin/uploads (purpose payment_qr).")
    remove_qr: bool = Field(False, description="Remove the QR image (customers stop seeing it at checkout). Ignored with qr_asset_key.")


class ReceivingAccountList(BaseModel):
    accounts: list[ReceivingAccount] = Field(description="Every receiving account, active and inactive, by method then name.")


class AddReceivingAccountRequest(BaseModel):
    method: PaymentMethod
    # Length is checked after spaces are collapsed (2 to 120), with the same message as the command.
    label: str = Field(max_length=1000, description="The account's name staff see: 2 to 120 characters after extra spaces are removed. "
                                                     "Not the full account number.")
    bank_name: str | None = Field(None, max_length=1000)
    account_title: str | None = Field(None, max_length=1000)
    account_number: str | None = Field(None, max_length=60, description="The full account number, stored and shown in full (D40).")
    iban: str | None = Field(None, max_length=60, description="IBAN; spaces are removed.")
    qr_asset_key: str | None = Field(None, max_length=300, description="From POST /v1/admin/uploads (purpose payment_qr).")


class SellingPriceItem(BaseModel):
    passenger_type: Literal["ADT", "CHD", "INF"]
    selling_each: BookingMoney = Field(description="What the customer pays per traveler of this type: whole rupees, not below that traveler's supplier cost.")


class SetSellingPriceRequest(BaseModel):
    """The selling price (Group Ticketing and Flights): before any payment, while the booking is on hold and not issued."""

    prices: list[SellingPriceItem] = Field(min_length=1, max_length=3)


class SellingPriceResult(BaseModel):
    total: BookingMoney = Field(description="The new selling total: what payments are capped at.")
    supplier_cost: BookingMoney | None = Field(None, description="Only with the cost and margin permission.")
    profit: BookingMoney | None = Field(None, description="total - supplier_cost. Only with the cost and margin permission.")


class RecordPaymentRequest(BaseModel):
    amount: BookingMoney
    method: PaymentMethod
    account_id: str = Field(min_length=8, max_length=36, description="The receiving account (GET /v1/admin/payment-accounts); active and of this method.")
    reference: str | None = Field(None, min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ./_-]*$",
                                  description="The payer's transaction reference. Left out (Group Ticketing payments): the gateway records a reference of its own.")
    received_at: datetime | None = Field(None, description="When the money was received. Not in the future. Left out: now.")


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
    last_attempt: IssuanceAttempt | None = Field(None, description="The last issuing attempt or supplier check, with its reason.")
    can_check: bool = Field(False, description="Check with supplier is available (unknown or failed issuance; read-only).")


class IssuanceAttempt(BaseModel):
    """The last issuing attempt or supplier check, with its real reason in plain words."""

    at: datetime
    outcome: Literal["issued", "not_issued", "unknown", "aborted", "checked"]
    reason: str = Field(description="What happened, in plain words (no supplier names).")
    supplier_answer: str | None = Field(None, description="What the supplier answered to the ticketing request, in plain words.")
    pnr_status: str | None = Field(None, description="The PNR status code the supplier showed afterwards (e.g. OK, BK).")


class IssueTicketRequest(BaseModel):
    accepted_supplier_cost: BookingMoney | None = Field(None, description="After 409 supplier_cost_changed: the airline's new cost, confirmed.")


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


class BookingSegment(BaseModel):
    """One flight of the Itinerary tab, as the supplier described it when the booking was held."""

    direction: Literal["outbound", "return"]
    origin: str
    destination: str
    airline_code: str | None = None
    airline_name: str | None = None
    flight_number: str | None = None
    departure_local: str | None = Field(None, description="Local time at the origin, e.g. 2026-11-19T00:25.")
    arrival_local: str | None = Field(None, description="Local time at the destination.")
    duration_minutes: int | None = None
    stops: int | None = None
    stop_cities: list[str] = Field(default_factory=list)
    fare_name: str | None = None
    baggage: str | None = None
    meal: str | None = None


class BookingDetail(BaseModel):
    fare_terms: FareTermsView | None = Field(None, description="Group bookings: the stored non-refundable, non-changeable acknowledgement.")
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
    itinerary: list[BookingSegment] | None = Field(None, description="The Itinerary tab: the flights as held, in travel order. Omitted when not recorded (bookings held before 14 Nov 2026).")
    audit: list[BookingAuditEntry] | None = Field(None, description="The Audit tab. Omitted when there are no events.")
    payments: list[BookingPayment] | None = Field(None, description="The Payments tab. Omitted when no payment is recorded.")
    amount_paid: BookingMoney | None = Field(None, description="Sum of recorded payments.")
    amount_due: BookingMoney | None = Field(None, description="Total less recorded payments.")
    can_record_payments: bool = Field(False, description="Whether this viewer may record a payment (the endpoint still checks).")
    can_issue_tickets: bool = Field(False, description="Whether this viewer may issue, verify and resolve (the endpoints still check).")
    can_set_selling_price: bool = Field(False, description="Whether this viewer may change the selling price now: on hold, not issued, nothing paid, "
                                                         "Super Admin or record payments (the endpoint still checks).")
    sections_not_available: list[DetailSection] = Field(
        description="Tabs whose data the gateway doesn't record yet; the page shows them as not available."
    )
    final_pnr: str | None = Field(None, description="Group bookings: the airline's final PNR, recorded by staff after payment.")
    group_ticketing_status: Literal["awaiting_final_pnr", "final_pnr_received", "ticketed"] | None = Field(
        None, description="Group bookings after full payment: Paid – awaiting final PNR, Final PNR received, Ticketed.")
    can_edit_group_tickets: bool = Field(False, description="Group bookings: this viewer may record the final PNR and ticket numbers on the Tickets tab "
                                                            "(issue tickets permission, paid in full; the endpoint still checks).")
    itinerary_available: bool = Field(True, description="The itinerary PDF / PNG can be downloaded. Group bookings: only once Ticketed.")
