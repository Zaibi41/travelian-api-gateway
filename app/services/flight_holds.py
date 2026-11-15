"""Southair hold (booking without ticketing or payment), behind SOUTHAIR_BOOKING_ENABLED.

Order: fresh reprice -> refuse unless it equals the confirmed total -> booking -> the supplier's
booking record (pnr_info) must show the same total -> save PNR, ticket_ref (server-only), time_limit
and that total in `bookings` as UAT test data, with an audit entry. The total, the per-passenger costs
and the below-cost check all use that one cost. An unverified total saves nothing (total_unverified).
Never retried automatically. There is no cancel call: the hold lapses at time_limit.
"""

from __future__ import annotations

from dataclasses import replace

import logging
from datetime import date, datetime, timezone

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.field_visibility import BACK_OFFICE_ROLES, BackOfficeUser
from app.core.pakistan_airports import is_domestic
from app.db import saved_people, supplier_bookings
from app.schemas.customer_card import ContactError
from app.schemas.live_flights import IDENTITY_FIELDS, FlightMoney, HoldContact, HoldPassenger, HoldRequest, HoldView
from app.schemas.saved_people import SaveOutcomeView
from app.services import live_flights
from app.services.live_flights import RepriceError, get_quote, price_selection, sell
from app.vendors.flights.southair import normalize
from app.vendors.flights.southair.client import NAME, SouthairError, configured_client

logger = logging.getLogger(__name__)
REMARKS = "Travelian UAT test booking"


class HoldError(Exception):
    """code: stable reason for clients; extra: facts the page needs (pnr, booking_id) when a hold
    exists at the supplier although the call failed. message: the sentence to show."""

    def __init__(self, status: int, message: str, code: str = "invalid_passengers",
                 field_errors: list[tuple[list, str]] | None = None, **extra: str):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.extra = extra
        self.field_errors = field_errors or []  # [(path e.g. ["passengers", 0, "date_of_birth"], message)]


def _age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)


def passport_warnings(request: HoldRequest, search) -> list[str]:
    """Expiry before the last travel date: error (422). Under 6 months' validity on an
    international route: a warning only (the destination's own rules decide)."""
    last_travel = search.return_date or search.departure_date
    international = not is_domestic(search.origin, search.destination)
    warnings = []
    for passenger in request.passengers:
        expiry = passenger.document_expiry
        if passenger.id_type != "PSPT" or expiry is None:
            continue
        if expiry < last_travel:
            raise HoldError(422, f"{passenger.first_name}'s passport expires before the last travel date.")
        if international and expiry < _add_months(last_travel, 6):
            warnings.append(f"{passenger.first_name}'s passport is valid for less than 6 months after travel. "
                            "Check the destination's entry rules.")
    return warnings


def infant_adults(request: HoldRequest) -> list[int]:
    """For each infant in order, the index (among adults) of the adult it travels with."""
    adults = sum(p.type == "ADT" for p in request.passengers)
    chosen = [p.travels_with if p.travels_with is not None else index
              for index, p in enumerate(p for p in request.passengers if p.type == "INF")]
    if any(choice >= adults for choice in chosen):
        raise HoldError(422, "An infant is set to travel with an adult who isn't on this booking.")
    if len(set(chosen)) != len(chosen):
        raise HoldError(422, "Each infant needs its own adult.")
    return chosen


def _supplier_passengers(request: HoldRequest, travel: date) -> list[dict[str, str]]:
    adults = 0
    infants = 0
    links = infant_adults(request)
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
            # The chosen adult (default: infant n with adult n), numbered from 1 (UAT, 9 Nov 2026:
            # "0" is refused with "The adult selected for Infant 1 does not exist in this booking").
            row["infant_link_id"] = str(links[infants] + 1)
            infants += 1
        result.append(row)
    if infants > adults:
        raise HoldError(422, "Each infant needs an adult.")
    return result


def _people_error(error: saved_people.PeopleError, path: list) -> HoldError:
    return HoldError(error.status if error.status != 404 else 422, error.message, code=error.code, field_errors=[(path, error.message)])


def resolve_saved(request: HoldRequest, owner: str | None) -> tuple[HoldRequest, dict[int, tuple[str, str | None]]]:
    """Fill saved travelers and the saved customer from the database (server side), then run the
    same validation as typed-in details. Returns the full request and {position: (person, document)}."""
    links: dict[int, tuple[str, str | None]] = {}
    passengers = []
    for index, passenger in enumerate(request.passengers):
        if passenger.traveler_id is None:
            passengers.append(passenger)
            continue
        sent = [name for name in IDENTITY_FIELDS if getattr(passenger, name) is not None]
        if sent:
            raise HoldError(422, "A saved traveler's identity comes from the saved record; leave those fields out or edit the saved person.",
                            code="identity_with_traveler_id", field_errors=[(["passengers", index, name], "Comes from the saved traveler.") for name in sent])
        try:
            saved = saved_people.load_for_hold(owner, passenger.traveler_id, passenger.document_id)
        except saved_people.PeopleError as error:
            raise _people_error(error, ["passengers", index, "document_id" if error.code.startswith("document") else "traveler_id"]) from None
        for name in ("first_name", "last_name"):
            if getattr(passenger, name) and not saved_people._same_name(getattr(passenger, name), getattr(saved, name)):
                raise HoldError(422, "A saved traveler's name comes from the saved record.", code="identity_with_traveler_id",
                                field_errors=[(["passengers", index, name], "Comes from the saved traveler.")])
        if saved.date_of_birth is None:
            raise HoldError(422, "This saved traveler has no date of birth. Add it to the saved person first.", code="date_of_birth_missing",
                            field_errors=[(["passengers", index, "traveler_id"], "No date of birth saved.")])
        values = passenger.model_dump(exclude={"traveler_id"}) | {
            "title": passenger.title or saved.title, "first_name": saved.first_name, "last_name": saved.last_name,
            "date_of_birth": saved.date_of_birth, "id_type": saved.id_type, "document_number": (saved.document_number or "").replace(" ", "").replace("-", "") or None,
            "document_country": saved.document_country, "nationality": saved.nationality, "document_expiry": saved.document_expiry,
        }
        try:
            passengers.append(HoldPassenger.model_validate(values))
        except ValidationError as error:
            raise HoldError(422, "The saved traveler's details don't pass the checks for this booking.", code="invalid_saved_traveler",
                            field_errors=[(["passengers", index, *[p for p in e["loc"] if p != "__root__"]], e["msg"].removeprefix("Value error, "))
                                          for e in error.errors()]) from None
        links[index] = (saved.person_id, saved.document_id)
    contact = request.contact
    if contact.customer_id is not None:
        try:
            person = saved_people.load_contact(owner, contact.customer_id)
        except saved_people.PeopleError as error:
            raise _people_error(error, ["contact", "customer_id"]) from None
        values = {name: getattr(contact, name) or getattr(person, name) for name in ("first_name", "last_name", "email", "phone_code", "phone")}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise HoldError(422, "The saved customer has no phone or email. Add them for this booking.", code="contact_incomplete",
                            field_errors=[(["contact", name], "Required.") for name in missing])
        try:
            contact = HoldContact.model_validate(values | {"customer_id": contact.customer_id, "save_for_next_time": contact.save_for_next_time})
        except ValidationError as error:
            raise HoldError(422, "The saved customer's details don't pass the checks.", code="invalid_saved_customer",
                            field_errors=[(["contact", *e["loc"]], e["msg"]) for e in error.errors()]) from None
    return request.model_copy(update={"passengers": passengers, "contact": contact}), links


def customer_card_document(card, passengers) -> saved_people.DocumentInput | None:
    """The customer's own document from the shared customer card (Flights and Group Ticketing): a CNIC is Pakistani; a
    passport takes its issuing country from the traveler holding the same number (the customer travelling)."""
    if card is None or not card.document_number or not card.id_type:
        return None
    country = "PAK" if card.id_type == "NATI" else next(
        (p.document_country for p in passengers if p.document_number == card.document_number and p.document_country), None)
    return saved_people.DocumentInput(card.id_type, country, card.document_number) if country else None


def _save_people(request: HoldRequest, reference: str, actor: str, owner: str | None,
                 links: dict[int, tuple[str, str | None]], customer_document: saved_people.DocumentInput | None = None) -> list[SaveOutcomeView]:
    """Create or link saved people after the hold. Any failure is reported, never raised.
    customer_document: the customer's own document (Group Ticketing), saved with a new saved customer."""
    contact = request.contact
    customer = saved_people.HoldCustomer(contact.first_name, contact.last_name, contact.phone_code, contact.phone, contact.email,
                                         person_id=contact.customer_id, save=contact.save_for_next_time, document=customer_document)
    travelers = []
    for index, passenger in enumerate(request.passengers):
        document = None
        if passenger.document_number and (passenger.document_country or passenger.nationality):
            document = saved_people.DocumentInput(passenger.id_type, passenger.document_country or passenger.nationality,
                                                  passenger.document_number, passenger.document_expiry)
        person_id, document_id = links.get(index, (None, None))
        travelers.append(saved_people.HoldTraveler(index, passenger.first_name, passenger.last_name, passenger.title, passenger.date_of_birth,
                                                   passenger.nationality, document, person_id, document_id, passenger.save_for_next_time))
    try:
        outcomes = saved_people.save_from_hold(owner, actor, reference, customer, travelers)
    except Exception:  # noqa: BLE001 - saving people never fails a hold
        logger.warning("Saving people after a hold failed")
        return [SaveOutcomeView(role="customer", status="not_saved", code="save_failed")]
    from app.api.routes.saved_people import summary
    return [SaveOutcomeView(role=o.role, position=o.position, status=o.status, person_id=o.person_id, document_id=o.document_id,
                            possible_duplicate=summary(o.duplicate) if o.duplicate else None, reason=o.reason, code=o.code) for o in outcomes]


def _customer(request: HoldRequest) -> supplier_bookings.CustomerRecord:
    contact = request.contact
    return supplier_bookings.CustomerRecord(first_name=contact.first_name.strip(), last_name=contact.last_name.strip(),
                                            phone_code=contact.phone_code, phone=contact.phone, email=contact.email.strip())


def _travelers(request: HoldRequest, links: dict[int, tuple[str, str | None]] | None = None) -> list[supplier_bookings.TravelerRecord]:
    """Travelers in request order; infant n travels with adult n (as sent to the supplier)."""
    adult_positions = [index for index, passenger in enumerate(request.passengers) if passenger.type == "ADT"]
    infant_links = infant_adults(request)
    infants = 0
    result = []
    saved = links or {}
    for position, passenger in enumerate(request.passengers):
        infant_of = None
        if passenger.type == "INF":
            infant_of = adult_positions[infant_links[infants]]
            infants += 1
        result.append(supplier_bookings.TravelerRecord(
            passenger_type=passenger.type, title=passenger.title, first_name=passenger.first_name.strip(),
            last_name=passenger.last_name.strip(), date_of_birth=passenger.date_of_birth, id_type=passenger.id_type,
            document_number=passenger.document_number, document_country=passenger.document_country,
            nationality=passenger.nationality, document_expiry=passenger.document_expiry, infant_of_position=infant_of,
            person_id=saved.get(position, (None, None))[0], document_id=saved.get(position, (None, None))[1],
        ))
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
        raise HoldError(409, "Booking is turned off. Nothing was booked.", code="booking_off")
    if viewer.role not in BACK_OFFICE_ROLES:
        raise HoldError(403, "Only back-office users can place holds.", code="not_allowed")
    quote = get_quote(request.quote_id, viewer.user_id)
    if quote is None:
        raise HoldError(404, "The revalidated fare has expired. Revalidate again.", code="quote_expired")
    if quote.price_changed and not request.accept_price_change:
        raise HoldError(409, f"The price changed to {live_flights.money_text(quote.selling_total_minor, quote.currency)}. "
                             "Accept the new total to continue. Nothing was booked.", code="price_not_accepted")
    if request.confirmed_total.currency != quote.currency or request.confirmed_total.amount_minor != quote.selling_total_minor:
        raise HoldError(409, f"Confirm the revalidated total of {live_flights.money_text(quote.selling_total_minor, quote.currency)}. Nothing was booked.",
                        code="confirm_total")
    card = request.customer
    if request.customer is not None:
        # The shared customer card: the gateway splits the name and phone (same rules as the Group Ticketing hold).
        try:
            request = request.model_copy(update={"contact": request.customer.to_hold_contact(), "customer": None})
        except ContactError as error:
            raise HoldError(422, error.message, code="invalid_customer", field_errors=[(["contact", error.field], error.message)]) from None
        except ValidationError as error:
            fields = {"first_name": "full_name", "last_name": "full_name", "phone_code": "phone"}
            paths = [(["contact", fields.get(str(e["loc"][0]), str(e["loc"][0])) if e["loc"] else "full_name"], e["msg"].removeprefix("Value error, "))
                     for e in error.errors()]
            raise HoldError(422, "Check the customer's details.", code="invalid_customer", field_errors=paths) from None
    owner = None  # back-office holds are Travelian's own (saved people are scoped by owner)
    request, links = await run_in_threadpool(resolve_saved, request, owner)
    counts = {"ADT": quote.request.adults, "CHD": quote.request.children, "INF": quote.request.infants}
    given = {kind: sum(p.type == kind for p in request.passengers) for kind in counts}
    if given != counts:
        raise HoldError(422, "The passengers must match the search (adults, children, infants).", code="passengers_mismatch")
    passengers = _supplier_passengers(request, quote.request.departure_date)
    warnings = passport_warnings(request, quote.request)

    client = (client_factory or configured_client)()
    # 1. Fresh reprice: the supplier total must still match what the user confirmed.
    try:
        nld, fresh, currency, priced = await price_selection(quote.request, quote.refs, client)
    except RepriceError as error:
        raise HoldError(error.status, error.message, code=error.code) from None
    except SouthairError:
        raise HoldError(502, "The flight supplier couldn't reprice this fare. Nothing was booked.", code="not_confirmed") from None
    # The selling price per passenger type stays what the user confirmed (app.services.pricing). If the airline's
    # cost moved, the hold goes ahead while every selling price still covers the new cost (the profit changes);
    # a selling price below the new cost is refused: revalidate and confirm a new total.
    confirmed_selling = {kind: selling for kind, _, _, selling in quote.passengers}
    selling_each = [(kind, number, cost, confirmed_selling.get(kind) or sell(cost)) for kind, number, cost in priced]
    confirmed_selling = {kind: selling for kind, _, _, selling in selling_each}
    if any(selling is None for *_, selling in selling_each):
        raise HoldError(409, "No selling price is configured for flights. Nothing was booked.", code="no_selling_price")
    supplier_total = sum(number * cost for _, number, cost, _ in selling_each)
    selling_total = sum(number * selling for _, number, _, selling in selling_each)  # type: ignore[operator]
    below_cost = any(selling < cost for _, _, cost, selling in selling_each)  # type: ignore[operator]
    if currency != request.confirmed_total.currency or selling_total != request.confirmed_total.amount_minor or below_cost:
        fresh_selling = sum(number * (sell(cost) or cost) for _, number, cost, _ in selling_each)
        raise HoldError(409, f"The price changed to {live_flights.money_text(fresh_selling, currency)}. Revalidate and confirm the new total. Nothing was booked.", code="price_changed")
    costs_each = {kind: cost for kind, _, cost, _ in selling_each}

    # 2. Booking (hold). Never retried.
    try:
        booking = await client.booking(
            nld=nld, acceptances=[ref.fare_hash for ref in fresh], remarks=REMARKS,
            contact={"email": request.contact.email, "code": request.contact.phone_code, "phone": request.contact.phone},
            passengers=passengers,
        )
    except SouthairError as error:
        logger.warning("Southair booking failed", extra={"error_code": error.error_code})
        raise HoldError(502, "The supplier didn't confirm the hold. Check the supplier before trying again.", code="not_confirmed_hold") from None
    pnr, ticket_ref = str(booking.get("pnrn") or "").strip().upper(), booking.get("ticket_ref")
    if str(booking.get("booking_status")) != "1" or not pnr.isalnum() or not isinstance(ticket_ref, str) or not ticket_ref:
        logger.warning("Southair booking not confirmed", extra={"booking_status": booking.get("booking_status")})
        raise HoldError(502, "The supplier didn't confirm the hold. Check the supplier before trying again.", code="not_confirmed_hold")
    try:
        pax_count = int(booking.get("pax_count"))
    except (TypeError, ValueError):
        pax_count = None

    # 3. Verify the hold's total from one source: the supplier's own booking record of this PNR (pnr_info:
    #    res.total_amount, status BK). It must equal the repriced total the per-passenger costs and the below-cost check
    #    used, so total, traveler costs and profit all agree. No verified total, or a different one: nothing is saved.
    pnr_summary: dict = {}
    record_total, check_errors = None, {}
    try:
        record = normalize.pnr_state(await client.pnr_info(pnr), pnr)
        pnr_summary = {"status": record.status, "total_minor": live_flights.to_minor(record.total.value) if record.total else None,
                       "currency": record.currency}
        if record.status == "BK" and record.total is not None and (record.currency in (None, currency)):
            record_total = live_flights.to_minor(record.total.value)
    except SouthairError as error:
        check_errors["pnr_info_error"] = error.error_code
        logger.warning("Southair pnr_info after booking failed", extra={"error_code": error.error_code})
    total_check = "no_total" if record_total is None else "matches" if record_total == supplier_total else "differs"
    if total_check != "matches":
        # The supplier holds the PNR, but its total isn't confirmed: the booking is not saved, so no payment can be
        # taken against it. The supplier hold lapses at its own time limit.
        logger.warning("Southair hold total not verified; booking not saved",
                       extra={"pnr": pnr, "total_check": total_check, "time_limit": booking.get("time_limit"), **check_errors})
        raise HoldError(502, f"The supplier created a hold (PNR {pnr}) but its total couldn't be verified, so it wasn't saved. "
                             "Don't take payment for it; it lapses at the supplier's time limit. Search and hold again.",
                        code="total_unverified", pnr=pnr)
    stored_cost = record_total

    # 4. Save as UAT test data with an audit entry.
    time_limit = _time_limit(booking.get("time_limit"))
    try:
        reference = supplier_bookings.save_hold(
            provider=NAME, pnr=pnr, supplier_ref=ticket_ref, hold_expires_at=time_limit, currency=currency,
            selling_total_minor=selling_total, supplier_total_minor=stored_cost,
            supplier_response={"booking": {k: booking.get(k) for k in ("booking_status", "pnrn", "time_limit", "pax_count")},
                               "pnr_info": pnr_summary, "repriced_supplier_total_minor": supplier_total},
            actor_user_id=viewer.user_id, customer=_customer(request),
            travelers=[replace(t, selling_minor=confirmed_selling.get(t.passenger_type), cost_minor=costs_each.get(t.passenger_type))
                       for t in _travelers(request, links)],
            itinerary=[ref.segment for ref in fresh if ref.segment],
            audit={"provider": NAME, "pnr": pnr, "time_limit": booking.get("time_limit"), "selling_total_minor": selling_total,
                   "currency": currency, "pax_count": pax_count, "fare_info": total_check,
                   "total_source": "pnr_info"},
        )
    except Exception:
        logger.error("Southair hold created but not saved", extra={"pnr": pnr})
        raise HoldError(500, f"The hold was created at the supplier (PNR {pnr}) but couldn't be saved. Record this PNR.", code="not_saved", pnr=pnr) from None
    saved_outcomes = await run_in_threadpool(_save_people, request, reference, viewer.user_id, owner, links,
                                             customer_card_document(card, request.passengers))
    view = HoldView(booking_id=reference, pnr=pnr, time_limit=time_limit,
                    selling_total=FlightMoney(amount_minor=selling_total, currency=currency), warnings=warnings, saved_people=saved_outcomes)
    if viewer.visibility.cost_and_margin:
        view.supplier_cost = FlightMoney(amount_minor=stored_cost, currency=currency)
        view.profit = FlightMoney(amount_minor=selling_total - stored_cost, currency=currency)
    return view
