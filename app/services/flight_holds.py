"""Southair hold (booking without ticketing or payment), behind SOUTHAIR_BOOKING_ENABLED.

Order: fresh reprice -> refuse unless it equals the confirmed total -> booking ->
fare_info within the supplier's 10-minute window -> save PNR, ticket_ref (server-only),
time_limit and the fare total in `bookings` as UAT test data, with an audit entry.
Never retried automatically. There is no cancel call: the hold lapses at time_limit.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from app.core.config import settings
from app.core.field_visibility import BACK_OFFICE_ROLES, BackOfficeUser
from app.db import supplier_bookings
from app.schemas.live_flights import FlightMoney, HoldRequest, HoldView
from app.services import live_flights
from app.services.live_flights import RepriceError, get_quote, price_selection, sell
from app.vendors.flights.southair.client import NAME, SouthairError, configured_client

logger = logging.getLogger(__name__)
REMARKS = "Travelian UAT test booking"


class HoldError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def _supplier_passengers(request: HoldRequest, travel: date) -> list[dict[str, str]]:
    adults = 0
    infants = 0
    result = []
    for passenger in request.passengers:
        age = _age_on(passenger.date_of_birth, travel)
        if passenger.type == "ADT" and age < 12 or passenger.type == "CHD" and not 2 <= age < 12 or passenger.type == "INF" and age >= 2:
            raise HoldError(422, f"{passenger.first_name}'s date of birth doesn't match the passenger type.")
        if passenger.type == "INF" and passenger.title not in ("MASTER", "MISS"):
            raise HoldError(422, "Infants take the title MASTER or MISS.")
        if passenger.type == "ADT" and passenger.title == "MASTER":
            raise HoldError(422, "Adults take the title MR, MRS or MISS.")
        row = {
            "type": passenger.type, "first_name": passenger.first_name.strip(), "last_name": passenger.last_name.strip(),
            "date_of_birth": passenger.date_of_birth.isoformat(), "name_title": passenger.title, "id_type": passenger.id_type,
            "passport_no": passenger.document_number, "passport_issue": passenger.document_country,
            "passport_expiry": passenger.document_expiry.isoformat() if passenger.document_expiry else None,
            "origin_country": passenger.nationality,
        }
        if passenger.type == "ADT":
            adults += 1
        if passenger.type == "INF":
            row["infant_link_id"] = str(infants)  # infant n travels with adult n
            infants += 1
        result.append(row)
    if infants > adults:
        raise HoldError(422, "Each infant needs an adult.")
    return result


def _time_limit(value: object) -> datetime | None:
    text = str(value or "").replace("UTC", "").strip()
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def hold(request: HoldRequest, viewer: BackOfficeUser, client_factory=None) -> HoldView:
    if not settings.southair_booking_enabled:
        raise HoldError(409, "Booking is turned off on this gateway (SOUTHAIR_BOOKING_ENABLED).")
    if viewer.role not in BACK_OFFICE_ROLES:
        raise HoldError(403, "Only back-office users can place holds.")
    quote = get_quote(request.quote_id, viewer.user_id)
    if quote is None:
        raise HoldError(404, "The revalidated fare has expired. Revalidate again.")
    counts = {"ADT": quote.request.adults, "CHD": quote.request.children, "INF": quote.request.infants}
    given = {kind: sum(p.type == kind for p in request.passengers) for kind in counts}
    if given != counts:
        raise HoldError(422, "The passengers must match the search (adults, children, infants).")
    passengers = _supplier_passengers(request, quote.request.departure_date)

    client = (client_factory or configured_client)()
    # 1. Fresh reprice: the supplier total must still match what the user confirmed.
    try:
        nld, fresh, currency, priced = await price_selection(quote.request, quote.refs, client)
    except RepriceError as error:
        raise HoldError(error.status, error.message) from None
    except SouthairError:
        raise HoldError(502, "The flight supplier couldn't reprice this fare. Nothing was booked.") from None
    selling_each = [(kind, number, cost, sell(cost)) for kind, number, cost in priced]
    if any(selling is None for *_, selling in selling_each):
        raise HoldError(409, "No selling price is configured for flights. Nothing was booked.")
    supplier_total = sum(number * cost for _, number, cost, _ in selling_each)
    selling_total = sum(number * selling for _, number, _, selling in selling_each)  # type: ignore[operator]
    if currency != request.confirmed_total.currency or selling_total != request.confirmed_total.amount_minor:
        raise HoldError(409, f"The price changed to {currency} {selling_total / 100:,.0f}. Revalidate and confirm the new total. Nothing was booked.")

    # 2. Booking (hold). Never retried.
    try:
        booking = await client.booking(
            nld=nld, acceptances=[ref.fare_hash for ref in fresh], remarks=REMARKS,
            contact={"email": request.contact.email, "code": request.contact.phone_code, "phone": request.contact.phone},
            passengers=passengers,
        )
    except SouthairError as error:
        logger.warning("Southair booking failed", extra={"error_code": error.error_code})
        raise HoldError(502, "The supplier didn't confirm the hold. Check the supplier before trying again.") from None
    pnr, ticket_ref = str(booking.get("pnrn") or "").strip().upper(), booking.get("ticket_ref")
    if str(booking.get("booking_status")) != "1" or not pnr.isalnum() or not isinstance(ticket_ref, str) or not ticket_ref:
        logger.warning("Southair booking not confirmed", extra={"booking_status": booking.get("booking_status")})
        raise HoldError(502, "The supplier didn't confirm the hold. Check the supplier before trying again.")
    try:
        pax_count = int(booking.get("pax_count"))
    except (TypeError, ValueError):
        pax_count = None

    # 3. fare_info within the supplier's 10-minute window: the fare total of the hold.
    fare_info: dict = {}
    fare_info_status = "failed"
    stored_cost = supplier_total
    try:
        fare_info = await client.fare_info(nld=nld, selections=[(ref.route_key, ref.fare_hash) for ref in fresh])
        info_total = live_flights.verified_total_minor(fare_info, counts)
        if info_total is not None:
            fare_info_status = "matches" if info_total == supplier_total else "differs"
            stored_cost = info_total
        else:
            fare_info_status = "no_total"
    except SouthairError as error:
        logger.warning("Southair fare_info after booking failed", extra={"error_code": error.error_code})

    # 4. Save as UAT test data with an audit entry.
    time_limit = _time_limit(booking.get("time_limit"))
    try:
        reference = supplier_bookings.save_hold(
            provider=NAME, pnr=pnr, supplier_ref=ticket_ref, hold_expires_at=time_limit, currency=currency,
            selling_total_minor=selling_total, supplier_total_minor=stored_cost,
            supplier_response={"booking": {k: booking.get(k) for k in ("booking_status", "pnrn", "time_limit", "pax_count")},
                               "fare_info": fare_info, "repriced_supplier_total_minor": supplier_total},
            actor_user_id=viewer.user_id,
            audit={"provider": NAME, "pnr": pnr, "time_limit": booking.get("time_limit"), "selling_total_minor": selling_total,
                   "currency": currency, "pax_count": pax_count, "fare_info": fare_info_status},
        )
    except Exception:
        logger.error("Southair hold created but not saved", extra={"pnr": pnr})
        raise HoldError(500, f"The hold was created at the supplier (PNR {pnr}) but couldn't be saved. Record this PNR.") from None
    if fare_info_status != "matches":
        # The hold exists at the supplier and is saved, but its total isn't verified: never
        # report it as confirmed. Staff must check the total with the supplier first.
        logger.warning("Southair hold total not verified", extra={"pnr": pnr, "fare_info": fare_info_status})
        raise HoldError(502, f"The hold was created at the supplier (PNR {pnr}, {reference}) but its total couldn't be verified. "
                             "Check the total with the supplier before taking any payment.")
    return HoldView(booking_id=reference, pnr=pnr, time_limit=time_limit,
                    selling_total=FlightMoney(amount_minor=selling_total, currency=currency))
