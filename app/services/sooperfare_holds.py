"""SooperFare hold (reservation), the same steps as the Southair hold (app.services.flight_holds):

1. Fresh price check (search + quote): the selling total must still be what the user confirmed and cover the cost.
2. Reservation, never retried (a timeout may still have held seats: staff check SooperFare before trying again).
3. The held total is verified from one source, SooperFare's own booking record (booking/list -> booking.billable_price,
   reservation_status 'active'): it must equal the priced supplier total (fares + SooperFare's platform fee). No verified
   total: nothing is saved (the hold lapses at SooperFare's expiry; cancel is not built yet).
4. Saved held, unpaid, not issued, with the customer, travelers and itinerary.

Answers as read live on 18 November 2026 (one approved Airblue test hold; tests/sooperfare/fixtures/reservation_PA.json):
data.uuid (SooperFare's booking id, kept as supplier_ref), data.order_ref_id, data.providers[].pnr, data.billable_price,
data.reservation_status, data.expiry_at. Ticket issuing (booking/confirm) is in app.services.issuance (gateway#45).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BackOfficeUser
from app.db import supplier_bookings
from app.schemas.live_flights import FlightMoney, HoldRequest, HoldView
from app.services import flight_holds, live_flights, notifications
from app.vendors.flights.sooperfare import client as sooperfare

logger = logging.getLogger(__name__)

ALPHA2: dict[str, str] = json.loads((Path(__file__).resolve().parents[1] / "data" / "country_alpha2.json").read_text())
TITLES = {"MR": ("mr", "male"), "MASTER": ("master", "male"), "MRS": ("mrs", "female"), "MISS": ("miss", "female")}
PASSPORT_FIELDS = ("passport_number", "passport_country", "passport_expiry")


def _alpha2(code: str | None) -> str | None:
    return ALPHA2.get((code or "").upper())


def required_documents(slots: list[dict]) -> list[dict]:
    """The document each traveler must give, from SooperFare's quote slots (by type, in order): "nic" -> cnic; any
    passport field -> passport. Travelers with no requirement are left out."""
    out, seen = [], {}
    for slot in slots:
        kind = slot.get("type")
        if kind not in ("ADT", "CHD", "INF"):
            continue
        seen[kind] = seen.get(kind, 0) + 1
        required = set(slot.get("required_fields") or [])
        document = "cnic" if "nic" in required else "passport" if required.intersection(PASSPORT_FIELDS) else None
        if document:
            out.append({"type": kind, "number": seen[kind], "document": document})
    return out


def reservation_passengers(request: HoldRequest, slots: list[dict]) -> tuple[list[dict], list[tuple[list, str]]]:
    """Fill SooperFare's passenger slots (from the quote, by type and order) with our passengers.
    Returns (passengers, field errors). Only the fields SooperFare asks for are sent."""
    errors: list[tuple[list, str]] = []
    out: list[dict] = []
    by_type: dict[str, list[dict]] = {}
    for slot in slots:
        by_type.setdefault(slot["type"], []).append(slot)
    used: dict[str, int] = {}
    for index, passenger in enumerate(request.passengers):
        options = by_type.get(passenger.type, [])
        position = used.get(passenger.type, 0)
        used[passenger.type] = position + 1
        if position >= len(options):
            errors.append((["passengers", index], "The fare doesn't have a seat for this passenger. Revalidate."))
            continue
        slot = options[position]
        required = set(slot.get("required_fields") or [])
        title, gender = TITLES.get((passenger.title or "").upper(), (None, None))
        row: dict = {"ref_id": slot["ref_id"], "type": {"ADT": "adult", "CHD": "child", "INF": "infant"}[passenger.type],
                     "title": title, "first_name": passenger.first_name, "last_name": passenger.last_name, "gender": gender,
                     "dob": passenger.date_of_birth.isoformat() if passenger.date_of_birth else None,
                     "nationality": _alpha2(passenger.nationality)}
        if title is None:
            errors.append((["passengers", index, "title"], "Choose Mr, Mrs, Ms, Mstr or Miss."))
        if row["nationality"] is None:
            errors.append((["passengers", index, "nationality"], "Choose the nationality."))
        if passenger.id_type == "NATI" and passenger.document_number:
            row["nic"] = passenger.document_number
        elif "nic" in required:
            errors.append((["passengers", index, "document_number"], "This airline needs the traveler's CNIC for this flight: choose CNIC as the ID type and enter the 13 digits."))
        if passenger.id_type == "PSPT" and passenger.document_number:
            row.update({"passport_number": passenger.document_number, "passport_country": _alpha2(passenger.document_country),
                        "passport_expiry": passenger.document_expiry.isoformat() if passenger.document_expiry else None})
        elif required.intersection(PASSPORT_FIELDS):
            errors.append((["passengers", index, "document_number"], "This flight needs the traveler's passport."))
        out.append({key: value for key, value in row.items() if value is not None})
    return out, errors


def _minor(value) -> int | None:
    try:
        return int(round(float(str(value)) * 100))
    except (TypeError, ValueError):
        return None


def _expiry(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def hold(request: HoldRequest, viewer: BackOfficeUser, quote: live_flights.Quote, *, card, owner, links, warnings, factory=None,
               into: str | None = None) -> HoldView:
    HoldError = flight_holds.HoldError
    context = quote.supplier_context or {}
    # 1. Fresh price check (the same rules as Southair: the confirmed selling price stays while it covers the new cost).
    try:
        fresh, currency, priced, new_context = await live_flights.sf_price_selection(quote.request, quote.refs, factory)
    except live_flights.RepriceError as error:
        raise HoldError(error.status, error.message, code=error.code) from None
    except sooperfare.SooperfareError:
        raise HoldError(502, "The flight supplier couldn't reprice this fare. Nothing was booked.", code="not_confirmed") from None
    fee = int(new_context.get("platform_fee_minor") or 0)

    def fresh_each(kind: str, number: int, cost: int) -> int | None:
        """Today's selling price per passenger: markup, plus the platform fee shared across the adults."""
        selling = live_flights.sell(cost)
        return selling + live_flights.fee_share(fee, number) if selling is not None and kind == "ADT" else selling

    confirmed_selling = {kind: selling for kind, _, _, selling in quote.passengers}
    selling_each = [(kind, number, cost, confirmed_selling.get(kind) or fresh_each(kind, number, cost)) for kind, number, cost in priced]
    if any(selling is None for *_, selling in selling_each):
        raise HoldError(409, "No selling price is configured for flights. Nothing was booked.", code="no_selling_price")
    supplier_total = sum(number * cost for _, number, cost, _ in selling_each) + fee
    selling_total = sum(number * selling for _, number, _, selling in selling_each)  # type: ignore[operator]
    if (currency != request.confirmed_total.currency or selling_total != request.confirmed_total.amount_minor
            or selling_total < supplier_total or any(selling < cost for _, _, cost, selling in selling_each)):  # type: ignore[operator]
        fresh_selling = sum(number * (fresh_each(kind, number, cost) or cost) for kind, number, cost, _ in selling_each)
        raise HoldError(409, f"The price changed to {live_flights.money_text(fresh_selling, currency)}. Revalidate and confirm the new total. "
                             "Nothing was booked.", code="price_changed")
    passengers, errors = reservation_passengers(request, new_context.get("passengers") or [])
    if errors:
        raise HoldError(422, "Check the travelers' details.", code="invalid_passengers", field_errors=errors)

    contact = request.contact
    phone = f"+{contact.phone_code}{contact.phone}" if contact.phone_code else str(contact.phone or "")
    lead = request.passengers[0]
    body = {
        "ref_id": new_context["search_ref_id"], "is_ancillary": False,
        "contact_info": {"first_name": contact.first_name or lead.first_name, "last_name": contact.last_name or lead.last_name,
                         "email_address": contact.email, "phone_number": phone,
                         "phone_number_country": "pk" if contact.phone_code == "92" else (_alpha2(lead.nationality) or "pk").lower()},
        "passengers": passengers,
    }
    client = (factory or sooperfare.SooperfareClient)()
    # 2. Reservation. Never retried.
    try:
        answer = await client.reservation(body)
    except sooperfare.SooperfareError as error:
        if error.status is not None and 400 <= error.status < 500:
            raise HoldError(409 if error.status != 422 else 422, f"The airline didn't hold the seats: {error.public_message} Nothing was booked.",
                            code="hold_refused") from None
        logger.warning("SooperFare reservation unclear", extra={"error_code": error.error_code})
        raise HoldError(502, "The supplier didn't confirm the hold. Check SooperFare before trying again.", code="not_confirmed_hold") from None
    data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
    booking_id = str(data.get("uuid") or "")
    pnr = str(((data.get("providers") or [{}])[0] or {}).get("pnr") or "").strip().upper()
    expires_at = _expiry(data.get("expiry_at"))
    if not booking_id or not pnr.isalnum() or data.get("reservation_status") != "active" or expires_at is None:
        logger.warning("SooperFare reservation not confirmed", extra={"status": data.get("reservation_status"), "pnr": pnr or None})
        raise HoldError(502, "The supplier didn't confirm the hold. Check SooperFare before trying again.", code="not_confirmed_hold",
                        **({"pnr": pnr} if pnr else {}))

    # 3. Verify the held total from SooperFare's own booking record.
    record_total, record_status = None, None
    try:
        listing = await client.booking_list()
        items = listing.get("data") if isinstance(listing.get("data"), list) else []
        record = next((item.get("booking") for item in items if isinstance(item, dict) and item.get("uuid") == booking_id), None)
        if isinstance(record, dict):
            record_status = record.get("reservation_status")
            record_total = _minor(record.get("billable_price")) if record_status == "active" else None
    except sooperfare.SooperfareError as error:
        logger.warning("SooperFare booking list after reservation failed", extra={"error_code": error.error_code, "pnr": pnr})
    total_check = "no_total" if record_total is None else "matches" if record_total == supplier_total else "differs"
    if total_check != "matches":
        logger.warning("SooperFare hold total not verified; booking not saved",
                       extra={"pnr": pnr, "total_check": total_check, "record_total": record_total, "expected": supplier_total})
        from app.services import sooperfare_bookings  # late: it imports the SooperFare client too

        outcome = await sooperfare_bookings.cancel_orphan(booking_id, record_total or supplier_total, currency, pnr, client)
        what = ("SooperFare's booking list didn't show it" if total_check == "no_total"
                else f"SooperFare's total ({live_flights.money_text(record_total, currency)}) differs from the price checked "
                     f"({live_flights.money_text(supplier_total, currency)})")
        raise HoldError(502, f"SooperFare created a hold (PNR {pnr}) but it couldn't be verified: {what}. It wasn't saved, and {outcome}. "
                             "Don't take payment for it. Search and hold again.",
                        code="total_unverified", pnr=pnr)

    # 4. Save.
    costs_each = {kind: cost for kind, _, cost, _ in selling_each}
    selling_by_kind = {kind: selling for kind, _, _, selling in selling_each}
    try:
        reference = await run_in_threadpool(lambda: flight_holds.save(
            into, provider=sooperfare.NAME, pnr=pnr, supplier_ref=booking_id, hold_expires_at=expires_at, currency=currency,
            selling_total_minor=selling_total, supplier_total_minor=record_total,
            supplier_response={"reservation": {"uuid": booking_id, "order_ref_id": data.get("order_ref_id"), "pnr": pnr,
                                               "airline": new_context.get("provider_id"), "expiry_at": data.get("expiry_at"),
                                               "billable_price": data.get("billable_price"), "platform_fee": data.get("platform_fee")},
                               "booking_list": {"status": record_status, "total_minor": record_total},
                               "search": quote.request.model_dump(mode="json")},
            actor_user_id=flight_holds.actor(viewer), customer=flight_holds._customer(request),
            travelers=[replace(t, selling_minor=selling_by_kind.get(t.passenger_type), cost_minor=costs_each.get(t.passenger_type))
                       for t in flight_holds._travelers(request, links)],
            itinerary=[ref.segment for ref in fresh if ref.segment], source=None,
            audit={"provider": sooperfare.NAME, "pnr": pnr, "time_limit": data.get("expiry_at"), "selling_total_minor": selling_total,
                   "currency": currency, "total_source": "booking_list", "fare_info": total_check},
        ))
    except Exception:
        logger.error("SooperFare hold created but not saved", extra={"pnr": pnr})
        raise HoldError(500, f"The hold was created at the supplier (PNR {pnr}) but couldn't be saved. Record this PNR.", code="not_saved", pnr=pnr) from None
    saved_outcomes = [] if into else await run_in_threadpool(
        flight_holds._save_people, request, reference, flight_holds.actor(viewer), owner, links,
        flight_holds.customer_card_document(card, request.passengers))
    if not into:
        await notifications.booking_event(
            "hold_created", reference=reference, passenger=flight_holds._lead_name(request), route=flight_holds._route(fresh),
            fare_minor=selling_total, currency=currency, pnr=pnr, hold_expires_at=expires_at)
    view = HoldView(booking_id=reference, pnr=pnr, time_limit=expires_at, label=None,
                    selling_total=FlightMoney(amount_minor=selling_total, currency=currency), warnings=warnings, saved_people=saved_outcomes)
    if viewer.visibility.cost_and_margin:
        view.supplier_cost = FlightMoney(amount_minor=record_total, currency=currency)  # type: ignore[arg-type]
        view.profit = FlightMoney(amount_minor=selling_total - record_total, currency=currency)  # type: ignore[operator]
    return view
