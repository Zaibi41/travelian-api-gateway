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
RecordedPaymentMethod = Literal["bank_transfer", "jazzcash", "1link", "payfast", "alfalah"]
Card = Literal["confirmed", "on_hold", "cancelled", "unpaid", "expiring_soon", "awaiting_issuance", "hold_deadlines", "payment_pending",
               "paid_not_issued"]
DateType = Literal["created"]  # travel date is not stored yet
DetailSection = Literal["itinerary", "passengers", "payments", "tickets", "servicing", "audit"]

DataLabel = Literal["Imported"]

SOURCE = "gateway_bookings"
SAMPLE_LABEL = "Imported"  # issue #79: no "sample", "test" or "UAT" wording on staff screens


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
    sample: bool = Field(description="True for rows from a one-time import.")
    data_label: DataLabel | None = Field(None, description="\"Imported\" for rows from a one-time import.")
    airline_code: str | None = Field(None, description="First flight's airline (IATA), for the logo (#75).")
    airline_name: str | None = Field(None, description="First flight's airline name (the airline flown, not the supplier).")
    flight_number: str | None = Field(None, description="Flight number(s) in travel order, e.g. 'PA401 / PA402'.")


class BookingListFilterOptions(BaseModel):
    agencies: list[FilterOption]
    providers: list[FilterOption] | None = Field(None, description="Omitted without the supplier names permission.")


class BookingList(BaseModel):
    source: Literal["gateway_bookings"] = SOURCE
    contains_sample_data: bool
    sample_label: Literal["Imported"] = SAMPLE_LABEL
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
    profit: "ProfitView | None" = Field(None, description="travelian#70: selling + fees charged - supplier actual charge - payment fees - "
                                                          "refunds paid. Cost and margin permission only.")
    cost_unknown: bool | None = Field(None, description="True when no real supplier cost exists (none recorded, or an imported booking whose cost "
                                                        "was filled in as its total): show cost and profit as n/a. Only with the cost and margin permission.")


class RefundCreditOption(BaseModel):
    """An open Supplier Refund held as credit by this booking's supplier, usable to settle it."""

    id: str
    number: int
    remaining: BookingMoney
    reason: str
    booking_reference: str | None = None


class SupplierPayment(BaseModel):
    """The Supplier payment tab. Only sent with the cost and margin permission."""

    amount_due: BookingMoney | None = None
    hold_deadline: datetime | None = None
    billable_amount: BookingMoney | None = Field(None, description="Issue #202: what the supplier actually bills (editable until settled). "
                                                                   "The Fare tab and Issue the ticket read this figure.")
    supplier_id: str | None = None
    supplier_name: str | None = None
    settlement_method: Literal["fresh", "refund_credit"] | None = None
    refund_number: int | None = None
    credit_used: BookingMoney | None = None
    settled_at: datetime | None = None
    note: str | None = None
    refund_credits: list[RefundCreditOption] = Field(default_factory=list, description="Open refund credits with this supplier.")
    suppliers: list[dict] = Field(default_factory=list, description="Suppliers to choose from: [{id, name}].")
    can_edit: bool = Field(False, description="This viewer may set the billable amount and settle (Finance, or record payments).")
    available: bool = Field(True, description="False before the finance update (migration 0037) is applied.")


class PaymentShortfall(BaseModel):
    """D57: the booking accepted as paid with less received than its total, with the reason."""

    amount: BookingMoney
    reason_kind: Literal["discount", "goodwill", "channel_rate"]
    reason: str
    note: str
    recorded_at: datetime


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
    card_fee: BookingMoney | None = Field(None, description="Card fee the customer paid on top of this payment (travelian#74).")


class BookingSlip(BaseModel):
    """A customer's payment slip (gateway#56). The file opens through /v1/admin/finance/slips/{id}/link."""

    id: str
    method: Literal["bank_transfer", "jazzcash"]
    account_label: str | None = None
    amount: BookingMoney
    payer_reference: str | None = None
    content_type: str
    status: Literal["awaiting_check", "confirmed", "rejected"]
    submitted_at: datetime
    rejection_reason: str | None = None


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
    logo_asset_key: str | None = Field(None, description="S3 object key of the bank's logo image (travelian#76).")
    logo_url: str | None = Field(None, description="Public URL of the bank's logo (S3). None: checkout shows the bank's initials.")


class UpdateReceivingAccountRequest(BaseModel):
    bank_name: str | None = Field(None, max_length=1000)
    account_title: str | None = Field(None, max_length=1000)
    account_number: str | None = Field(None, max_length=60, description="The full account number, stored and shown in full (D40).")
    iban: str | None = Field(None, max_length=60, description="IBAN; spaces are removed.")
    qr_asset_key: str | None = Field(None, max_length=300, description="From POST /v1/admin/uploads (purpose payment_qr).")
    remove_qr: bool = Field(False, description="Remove the QR image (customers stop seeing it at checkout). Ignored with qr_asset_key.")
    logo_asset_key: str | None = Field(None, max_length=300, description="From POST /v1/admin/uploads (purpose bank_logo).")
    remove_logo: bool = Field(False, description="Remove the bank logo (checkout shows the bank's initials). Ignored with logo_asset_key.")


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
    logo_asset_key: str | None = Field(None, max_length=300, description="From POST /v1/admin/uploads (purpose bank_logo).")


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


class CustomerEmailProblem(BaseModel):
    """A customer email whose last attempt wasn't delivered to SES (e.g. the SES sandbox refuses unverified addresses)."""

    email: str = Field(description="Which email: booking_held, bank_transfer, payment_received, ticket_issued, ...")
    at: datetime
    to: str | None = Field(None, description="The address it went to, masked (a***@gmail.com).")
    reason: str | None = Field(None, description="Why, in plain words for staff.")
    can_resend: bool = Field(False, description="Resend email is offered (POST .../emails/{email}/resend).")
    fix_address: bool = Field(False, description="Bounced for good or marked as spam: contact the customer and fix the address "
                                                 "(PUT .../customer-email).")


class CustomerEmailChange(BaseModel):
    email: str = Field(min_length=3, max_length=254, description="The corrected customer email address.")


class CustomerEmailChanged(BaseModel):
    email: str
    message: str


class CustomerContactChange(BaseModel):
    """The customer's new mobile number and email (travelian#56). Names and documents are not changed here."""

    phone_code: str = Field(max_length=4, description='Country code, e.g. "+92".')
    phone: str = Field(max_length=20, description="Mobile number, digits only, without the country code.")
    email: str | None = Field(None, max_length=254, description="Left out or empty: the email on the booking stays.")


class CustomerContactChanged(BaseModel):
    phone: str = Field(description="With country code, e.g. +92 3001234567 (as on BookingCustomer).")
    email: str
    message: str


class ResendEmailResult(BaseModel):
    sent: bool
    message: str = Field(description="'Email sent to the customer.' or why not, in plain words.")


class SupplierBookingView(BaseModel):
    """The supplier's own booking (supplier-names permission only): its ids and, after a cancel request, what it says."""

    supplier: str
    booking_id: str = Field(description="The supplier's booking id (SooperFare: booking uuid).")
    order_ref: str | None = Field(None, description="The supplier's order reference (SooperFare: order_ref_id, e.g. spa-0rj).")
    cancel_state: Literal["none", "requested", "confirmed", "failed"] = "none"
    cancel_message: str | None = None
    cancel_at: datetime | None = None
    status_at_supplier: str | None = Field(None, description="The reservation status the supplier last showed.")
    can_cancel: bool = False
    can_rehold: bool = Field(False, description="Re-hold at SooperFare is offered: the hold is gone (expired, or paid but not "
                                                "ticketed), nothing is issued, and the viewer may issue tickets.")


class ReholdBody(BaseModel):
    """Re-hold at SooperFare."""

    accept_total_minor: int | None = Field(None, ge=1, description="The new selling total (minor units) staff saw and accepted, "
                                                                     "after a 409 price_changed. Leave out to hold only at the same total.")


class ReholdResult(BaseModel):
    message: str = Field(description="The new hold in plain words: new PNR and when it expires.")


class IssuanceAttempt(BaseModel):
    """The last issuing attempt or supplier check, with its real reason in plain words."""

    at: datetime
    outcome: Literal["issued", "not_issued", "unknown", "aborted", "checked"]
    reason: str = Field(description="What happened, in plain words (no supplier names).")
    supplier_answer: str | None = Field(None, description="What the supplier answered to the ticketing request, in plain words.")
    pnr_status: str | None = Field(None, description="The PNR status code the supplier showed afterwards (e.g. OK, BK).")


class IssuanceChecklist(BaseModel):
    """What stands between this booking and a ticket. Supplier fields only with the cost and margin permission."""

    issuance_status: IssuanceStatus
    blockers: list[str] = Field(description="Why issuing (or requesting) isn't possible now; empty when it is.")
    full_payment_recorded: bool
    amount_paid: BookingMoney
    amount_due: BookingMoney
    hold_expires_at: datetime | None = None
    ticketing_enabled: bool
    ticketing_note: str | None = Field(None, description="Why tickets can't be issued from Travelian for this booking: the supplier and the switch. "
                                                       "Left out while a flight's hold is gone (expired or not on hold): the hold blocker is then the reason.")
    can_request: bool
    can_issue: bool = Field(description="This viewer may issue now: permission, ticketing on, no blockers.")
    supplier_amount: BookingMoney | None = Field(None, description="Only with the cost and margin permission.")
    supplier_balance: BookingMoney | None = Field(None, description="Only with the cost and margin permission; omitted if the supplier didn't answer.")
    margin: BookingMoney | None = Field(None, description="Only with the cost and margin permission.")
    last_attempt: IssuanceAttempt | None = Field(None, description="The last issuing attempt or supplier check, with its reason.")
    can_check: bool = Field(False, description="Check with supplier is available (unknown or failed issuance; read-only).")
    balance_warning: str | None = Field(None, description="The supplier's known balance is low or below this ticket's cost (Finance > Wallets). "
                                                         "Amounts and supplier name only with cost and margin plus supplier names.")


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
    baggage: str | None = Field(None, description="Short, hand carry first: '7 kg', '7+20 kg', or 'Baggage: check with airline'.")
    baggage_detail: str | None = Field(None, description="In full words, e.g. '7 kg hand carry + 20 kg checked'.")
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
    chosen_account: str | None = Field(None, description="The receiving account the customer chose at checkout (travelian#76).")
    payment_slips: list[BookingSlip] | None = Field(None, description="Customer payment slips. Omitted when none.")
    amount_paid: BookingMoney | None = Field(None, description="Sum of recorded payments.")
    amount_due: BookingMoney | None = Field(None, description="Total less recorded payments (and less an accepted difference, D57).")
    payment_shortfall: PaymentShortfall | None = Field(None, description="D57: accepted as paid with less received, and why.")
    can_accept_shortfall: bool = Field(False, description="Part paid, nothing accepted yet, and this viewer may record payments.")
    sold_through: str | None = Field(None, description="The reseller or channel the booking was sold through (not the customer).")
    can_edit_sold_through: bool = False
    can_correct: bool = Field(False, description="Super Admin: may correct travelers and the customer name (travelian#225; the endpoints still check).")
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
    customer_email_problems: list[CustomerEmailProblem] | None = Field(None, description="Customer emails not sent: staff contact the customer.")
    supplier_booking: SupplierBookingView | None = Field(None, description="SooperFare bookings, supplier-names permission only.")
    itinerary_available: bool = Field(True, description="The itinerary PDF / PNG can be downloaded. Group bookings: only once Ticketed.")


_NAME = r"^[A-Za-z][A-Za-z '\-]{0,63}$"


class TravelerCorrection(BaseModel):
    """Super Admin: correct one traveler on a booking (travelian#225). Leave a field out to keep it. Logged with the reason
    (which fields, never the values)."""

    title: Literal["MR", "MRS", "MISS", "MASTER"] | None = None
    first_name: str | None = Field(None, pattern=_NAME)
    last_name: str | None = Field(None, pattern=_NAME)
    date_of_birth: date | None = None
    document_number: str | None = Field(None, pattern=r"^[A-Za-z0-9]{1,15}$")
    document_country: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    nationality: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    document_expiry: date | None = None
    reason: str = Field(min_length=5, max_length=500, description="Why this is corrected; shown in the booking's audit trail.")


class CustomerNameCorrection(BaseModel):
    """Super Admin: correct the customer's name on a booking (travelian#225). Contact details: customer-contact."""

    first_name: str | None = Field(None, pattern=_NAME)
    last_name: str | None = Field(None, pattern=_NAME)
    reason: str = Field(min_length=5, max_length=500)


class CorrectionResult(BaseModel):
    fields: list[str] = Field(description="The fields that changed.")
    sent_to_airline: bool = Field(description="The booking already has a PNR or ticket: correct it with the airline too.")
    message: str


from app.schemas.finance import ProfitView  # noqa: E402  (forward reference in BookingFare)

BookingFare.model_rebuild()
