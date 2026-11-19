"""A card payment that wasn't followed by a ticket (every supplier, and with ticketing off): staff must act.

`needs_staff` runs whenever a verified card payment on a website flight is not issued automatically (ticketing off, a
supplier Travelian can't issue for yet, or issuing failed):
1. Ask the supplier whether the hold is still active (read-only: Southair pnr_info, SooperFare booking/list). A hold past
   its time limit counts as lost without asking.
2. Lost: re-hold the same fare ONCE for this booking (same flights, same fare, same selling total, the saved travelers),
   recorded either way (rehold_created / rehold_failed). Success keeps the booking held under the new PNR. Failure: the
   booking becomes 'paid_not_ticketed' and staff get the "hold lost" red alert.
   Not re-held when the hold is only unclear (the supplier couldn't be read or answered in a way we can't read): a second
   reservation for the same people could double-book.
3. Otherwise staff get the red alert (once per booking) and the admin shows "Paid, issuance pending".
`thirty_minute_alerts` (recovery job) sends the second red alert when about 30 minutes of hold are left and it still isn't
issued. Nothing here issues a ticket, takes or refunds money.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from starlette.concurrency import run_in_threadpool

from app.db import flight_recovery
from app.db import issuance as store
from app.services import notifications

logger = logging.getLogger(__name__)

ACTION = "Booked and payment received. Confirm with supplier, issue the ticket, and share it with the customer."
LOST_ACTION = "Booked and payment received, but the airline hold is lost. Refund the customer or rebook a new fare, and contact them."
THIRTY_MINUTES = timedelta(minutes=30)
SUPPLIER_NAMES = {"southair": "Southair", "sooperfare": "SooperFare"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 1. is the hold still there? (read-only) ----------

async def hold_status(target: store.Target, now: datetime, client_factory=None, sooperfare_factory=None) -> tuple[str, str]:
    """(active | issued | lost | unclear, words for staff)."""
    if target.hold_expires_at is None or target.hold_expires_at <= now:
        return "lost", "the hold time has passed"
    provider = target.provider or "southair"
    try:
        if provider == "sooperfare":
            from app.vendors.flights.sooperfare.client import SooperfareClient

            listing = await (sooperfare_factory or SooperfareClient)().booking_list()
            items = listing.get("data") if isinstance(listing, dict) and isinstance(listing.get("data"), list) else []
            record = next((item.get("booking") for item in items if isinstance(item, dict) and item.get("uuid") == target.supplier_ref), None)
            if not isinstance(record, dict):
                return "unclear", "SooperFare's booking list doesn't show this booking"
            status = str(record.get("reservation_status") or "")
            return ("active", "SooperFare shows it active") if status == "active" else ("lost", f"SooperFare shows it '{status or 'unknown'}'")
        from app.vendors.flights.southair import normalize
        from app.vendors.flights.southair.client import configured_client

        state = normalize.pnr_state(await (client_factory or configured_client)().pnr_info(target.pnr), target.pnr)
        if state.kind == "not_issued":
            return "active", "Southair shows it booked (BK)"
        if state.kind == "issued":
            return "issued", "Southair shows tickets on this PNR: record them on the booking"
        return "unclear", f"Southair shows status '{state.status or 'unknown'}'"
    except Exception as error:  # noqa: BLE001 - reported to staff as unclear
        logger.warning("Hold status not read", extra={"reference": target.reference, "error_type": type(error).__name__})
        return "unclear", "the supplier couldn't be reached to check it"


# ---------- 2. re-hold the same fare once ----------

def _search_for(row: dict, people) -> "object":
    from app.schemas.flights import FlightSearchRequest

    saved = (row.get("supplier_response") or {}).get("search")
    if isinstance(saved, dict):
        return FlightSearchRequest.model_validate(saved)
    legs = [leg for leg in row.get("itinerary") or [] if isinstance(leg, dict)]
    out = [leg for leg in legs if leg.get("direction", "outbound") == "outbound"]
    back = [leg for leg in legs if leg.get("direction") == "return"]
    kinds = [t.passenger_type for t in people.travelers]
    return FlightSearchRequest(origin=out[0]["origin"], destination=out[-1]["destination"],
                               departure_date=date.fromisoformat(str(out[0]["departure_local"])[:10]),
                               return_date=date.fromisoformat(str(back[0]["departure_local"])[:10]) if back else None,
                               adults=kinds.count("ADT"), children=kinds.count("CHD"), infants=kinds.count("INF"))


def _same(saved: dict, offered: dict | None) -> bool:
    return isinstance(offered, dict) and all(saved.get(key) == offered.get(key) for key in ("direction", "flight_number", "departure_local", "fare_name"))


def _booking_row(reference: str) -> dict:
    from sqlalchemy import text

    from app.db import agencies

    with agencies.application_engine().begin() as connection:
        return dict(connection.execute(text("SELECT provider_id, total_minor, currency, itinerary, supplier_response FROM bookings"
                                            " WHERE reference = :r"), {"r": reference}).mappings().one())


async def rehold(reference: str, client_factory=None, sooperfare_factory=None) -> tuple[bool, str]:
    """Hold the same fare again for this booking. (held?, what happened, for staff)."""
    from app.db import booking_people
    from app.schemas.live_flights import FlightMoney, HoldContact, HoldPassenger, HoldRequest
    from app.services import flight_holds, live_flights
    from app.services.public_booking import WEBSITE_SEARCH_VIEWER

    row = await run_in_threadpool(_booking_row, reference)
    people = await run_in_threadpool(booking_people.read, reference)
    provider = row["provider_id"] or "southair"
    saved = [leg for leg in row["itinerary"] or [] if isinstance(leg, dict)]
    try:
        view = await live_flights.search(_search_for(row, people), WEBSITE_SEARCH_VIEWER, client_factory=client_factory,
                                         sooperfare_factory=sooperfare_factory)
        session = live_flights.get_session(view.search_id, WEBSITE_SEARCH_VIEWER.user_id)
        fares = list(session.fares.items()) if session else []
        fare_ids = [next((fare_id for fare_id, ref in fares if ref.provider == provider and _same(leg, ref.segment)), None) for leg in saved]
        if not saved or None in fare_ids:
            return False, "the same flights and fare aren't offered any more"
        priced = await live_flights.reprice(view.search_id, fare_ids, WEBSITE_SEARCH_VIEWER, client_factory=client_factory,  # type: ignore[arg-type]
                                            sooperfare_factory=sooperfare_factory)
        if priced.selling_total.amount_minor != row["total_minor"] or priced.selling_total.currency != row["currency"]:
            return False, (f"the fare now costs {live_flights.money_text(priced.selling_total.amount_minor, priced.selling_total.currency)} "
                           f"instead of the {live_flights.money_text(row['total_minor'], row['currency'])} paid")
        adults = [t.position for t in people.travelers if t.passenger_type == "ADT"]
        passengers = [HoldPassenger(
            type=t.passenger_type, title=t.title, first_name=t.first_name, last_name=t.last_name, date_of_birth=t.date_of_birth,
            id_type=t.id_type, document_number=t.document_number, document_country=t.document_country, document_expiry=t.document_expiry,
            nationality=t.nationality, save_for_next_time=False,
            travels_with=adults.index(t.infant_of_position) if t.passenger_type == "INF" and t.infant_of_position in adults else None,
        ) for t in people.travelers]
        customer = people.customer
        request = HoldRequest(quote_id=priced.quote_id, confirmed_total=FlightMoney(amount_minor=row["total_minor"], currency=row["currency"]),
                              contact=HoldContact(first_name=customer.first_name, last_name=customer.last_name, email=customer.email,
                                                  phone_code=customer.phone_code, phone=customer.phone, save_for_next_time=False),
                              passengers=passengers, accept_price_change=True)
        held = await flight_holds.hold(request, WEBSITE_SEARCH_VIEWER, client_factory=client_factory, sooperfare_factory=sooperfare_factory,
                                       into=reference)
    except flight_holds.HoldError as error:
        return False, f"the supplier didn't hold it again ({error.code})"
    except live_flights.RepriceError as error:
        return False, f"the fare couldn't be priced again ({error.code})"
    except Exception as error:  # noqa: BLE001 - recorded; staff take over
        logger.warning("Re-hold failed", extra={"reference": reference, "error_type": type(error).__name__})
        return False, "the re-hold couldn't be completed"
    return True, f"held again: new PNR {held.pnr}, expires {held.time_limit:%d %b %Y %H:%M} UTC"


# ---------- 3. red alerts ----------

def _when(value: datetime | None) -> str:
    return f"{value:%d %b %Y %H:%M} UTC" if value else "—"


async def send_red_alert(kind: str, reference: str, *, reason: str, hold: str, action: str = ACTION) -> None:
    facts = await run_in_threadpool(flight_recovery.facts, reference)
    if facts is None:
        return
    legs = [leg for leg in facts["itinerary"] or [] if isinstance(leg, dict)]
    route = " → ".join([legs[0].get("origin") or "?"] + [leg.get("destination") or "?" for leg in legs]) if legs else "—"
    travel_date = str(legs[0].get("departure_local") or "")[:16].replace("T", " ") if legs else "—"
    amount = notifications._money(facts["paid_minor"], facts["currency"])
    supplier = SUPPLIER_NAMES.get(facts["provider_id"] or "southair", facts["provider_id"] or "—")
    subject = f"URGENT: Payment received, ticket NOT issued — {reference}"
    if kind == "paid_not_issued_30m":
        subject += " (about 30 minutes of hold left)"
    lines = [f"Booking reference: {reference}", f"Amount received: {amount}",
             f"PayFast transaction ID: {facts['payfast_transaction_id'] or '—'}", f"Supplier: {supplier}", f"PNR: {facts['pnr'] or '—'}",
             f"Hold status: {hold}", f"Hold expires: {_when(facts['hold_expires_at'])}",
             f"Passengers: {', '.join(facts['passengers']) or '—'}", f"Route: {route}", f"Travel date: {travel_date}",
             f"Reason: {reason}", "", action]
    details = {"reference": reference, "amount": amount, "payfast_transaction_id": facts["payfast_transaction_id"], "supplier": supplier,
               "pnr": facts["pnr"], "hold_status": hold, "hold_expires_at": facts["hold_expires_at"].isoformat() if facts["hold_expires_at"] else None,
               "passenger": ", ".join(facts["passengers"]), "route": route, "travel_date": travel_date, "fare": amount,
               "reason": reason, "note": f"{reason}. {action}"}
    await notifications.red_alert(kind, reference=reference, subject=subject, lines=lines, details=details)
    # The customer: reassured once while staff issue, or told the choice (refund or rebook) when the hold is lost.
    customer_kind = {"paid_not_issued": "issuance_pending", "paid_hold_lost": "paid_not_ticketed"}.get(kind)
    if customer_kind:
        from app.services import customer_emails

        await customer_emails.send(reference, customer_kind)


async def needs_staff(reference: str, reason: str, *, now: datetime | None = None, client_factory=None, sooperfare_factory=None) -> str:
    """Returns: needs_staff (hold active or unclear, or re-held), paid_not_ticketed (hold lost, re-hold failed)."""
    now = now or _now()
    target = await run_in_threadpool(store.load, reference)
    status, words = await hold_status(target, now, client_factory, sooperfare_factory)
    if status == "lost" and target.issuance_status == "not_issued":
        if await run_in_threadpool(flight_recovery.mark_once, reference, "rehold_attempt", {"hold": words}):
            held, what = await rehold(reference, client_factory, sooperfare_factory)
            if not held:
                await run_in_threadpool(flight_recovery.mark_once, reference, "rehold_failed", {"result": what})
            if held:
                status, words = "active", f"the original hold was lost ({words}); {what}"
        if status == "lost":
            await run_in_threadpool(flight_recovery.mark_paid_not_ticketed, reference, "hold_lost", now, {"hold": words})
            if await run_in_threadpool(flight_recovery.mark_once, reference, "red_alert_hold_lost"):
                await send_red_alert("paid_hold_lost", reference, reason=f"{reason}; the airline hold is lost and re-holding didn't work",
                                     hold=f"lost ({words})", action=LOST_ACTION)
            return "paid_not_ticketed"
    if await run_in_threadpool(flight_recovery.mark_once, reference, "red_alert"):
        await send_red_alert("paid_not_issued", reference, reason=reason, hold=f"{status} ({words})")
    return "needs_staff"


async def hold_lost(reference: str, reason: str) -> None:
    """A paid booking whose hold ended unissued (expiry job): the 'hold lost' red alert, once."""
    if await run_in_threadpool(flight_recovery.mark_once, reference, "red_alert_hold_lost"):
        await send_red_alert("paid_hold_lost", reference, reason=reason, hold="lost (the hold time has passed)", action=LOST_ACTION)


async def thirty_minute_alerts(now: datetime | None = None) -> list[str]:
    """The second red alert: about 30 minutes of hold left and still not issued (card-paid flights). Once per booking."""
    now = now or _now()
    sent = []
    for reference in await run_in_threadpool(flight_recovery.expiring_unissued, now, now + THIRTY_MINUTES):
        if await run_in_threadpool(flight_recovery.mark_once, reference, "red_alert_30m"):
            await send_red_alert("paid_not_issued_30m", reference, reason="about 30 minutes of hold are left and the ticket still isn't issued",
                                 hold="active, ending soon")
            sent.append(reference)
    return sent
