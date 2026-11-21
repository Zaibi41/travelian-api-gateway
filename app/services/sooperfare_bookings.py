"""SooperFare bookings after the hold: what SooperFare shows for them, and cancelling at SooperFare.

Cancel (staff, after confirming in the admin): POST /v1/booking/cancel, then SooperFare's own record (booking/list) decides.
Live on 19 Nov 2026 the cancel call answered "Operation Successfull" with a cancel transaction while the booking stayed
`active` (additional.is_cancelled false), so a booking is marked Cancelled only when booking/list says so
(is_cancelled, or a reservation status that isn't active). Until then it shows "Cancel requested at SooperFare".
Every step is in the booking's audit trail with SooperFare's answer (no personal data). Nothing here issues or pays.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.db import agencies
from app.db.supplier_bookings import _event
from app.vendors.flights.sooperfare import client as sooperfare

logger = logging.getLogger(__name__)

#: Why SooperFare gave no clear answer, in words (SooperfareError.error_code).
NO_ANSWER = {"timeout": "took too long to answer", "unreachable": "couldn't be reached",
             "invalid_response": "answered in a way Travelian couldn't read", "login_failed": "refused Travelian's sign-in",
             "not_configured": "isn't set up on this server"}

CLOSED = {"cancelled", "canceled", "expired", "void", "voided"}


class SupplierCancelError(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass
class Record:
    id: str
    reference: str
    provider: str | None
    booking_status: str
    pnr: str | None
    supplier_ref: str | None
    supplier_cost_minor: int | None
    currency: str
    supplier_response: dict[str, Any]
    issuance_status: str = "not_issued"
    hold_over: bool = False  # the hold's time limit has passed


def _load(reference: str) -> Record | None:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT id, reference, provider_id, booking_status, pnr, supplier_ref, supplier_cost_minor, currency, supplier_response,"
            " issuance_status, COALESCE(hold_expires_at <= now(), true) AS hold_over FROM bookings WHERE reference = :r"), {"r": reference}).mappings().one_or_none()
    if row is None:
        return None
    response = row["supplier_response"] if isinstance(row["supplier_response"], dict) else json.loads(row["supplier_response"] or "{}")
    return Record(row["id"], row["reference"], row["provider_id"], row["booking_status"], row["pnr"], row["supplier_ref"],
                  row["supplier_cost_minor"], row["currency"], response or {}, row["issuance_status"], bool(row["hold_over"]))


def _save(record: Record, actor: str | None, action: str, details: dict[str, Any], *, cancel: dict[str, Any] | None = None,
          status: str | None = None) -> None:
    with agencies.application_engine().begin() as connection:
        if cancel is not None:
            connection.execute(text("UPDATE bookings SET supplier_response = COALESCE(supplier_response, '{}'::jsonb)"
                                    " || jsonb_build_object('supplier_cancel', CAST(:c AS jsonb)), updated_at = now(), revision = revision + 1"
                                    " WHERE id = :b"), {"c": json.dumps(cancel, default=str), "b": record.id})
        if status is not None:
            connection.execute(text("UPDATE bookings SET booking_status = :s, updated_at = now(), revision = revision + 1 WHERE id = :b"),
                               {"s": status, "b": record.id})
        _event(connection, record.id, actor, action, details)


TRAVELER_TYPE = {"adult": "ADT", "child": "CHD", "infant": "INF"}


def _tickets(booking: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Each passenger's ticket, in SooperFare's order: (ADT/CHD/INF, ticket number or None).
    Read from booking.providers[].legs[].passengers[] (ticket_number, passenger.traveler_type), as booking/list answered
    on 19 Nov 2026 (docs/SOOPERFARE_API_RESPONSES.md). A passenger listed on several legs (a return trip) is one traveler:
    the legs' different numbers are joined with "/" (one number when they are the same); a leg without a number leaves
    that traveler without a ticket, so nothing is recorded as issued from a partial answer."""
    travelers: list[list] = []  # [type, numbers, missing]
    for provider in booking.get("providers") or []:
        for leg in (provider or {}).get("legs") or []:
            for index, row in enumerate((leg or {}).get("passengers") or []):
                person = row.get("passenger") if isinstance(row, dict) and isinstance(row.get("passenger"), dict) else {}
                kind = TRAVELER_TYPE.get(str(person.get("traveler_type") or "").lower(), "?")
                number = str(row.get("ticket_number") or "").strip().upper() if isinstance(row, dict) else ""
                if index >= len(travelers):
                    travelers.append([kind, [], False])
                if number:
                    if number not in travelers[index][1]:
                        travelers[index][1].append(number)
                else:
                    travelers[index][2] = True
    out: list[tuple[str, str | None]] = []
    for kind, numbers, missing in travelers:
        joined = "/".join(numbers)
        out.append((kind, None if missing or not numbers or len(joined) > 32 else joined))
    return out


def _sooperfare_record(listing: dict[str, Any], booking_ref: str) -> dict[str, Any] | None:
    for item in listing.get("data") or []:
        if isinstance(item, dict) and item.get("uuid") == booking_ref:
            booking = item.get("booking") if isinstance(item.get("booking"), dict) else {}
            flags = item.get("additional") if isinstance(item.get("additional"), dict) else {}
            return {"reservation_status": booking.get("reservation_status"), "is_cancelled": bool(flags.get("is_cancelled")),
                    "is_paid": bool(flags.get("is_paid")), "is_issued": bool(flags.get("is_issued")), "expiry_at": booking.get("expiry_at"),
                    "order_ref_id": booking.get("order_ref_id"), "pnr": next((str(p.get("pnr")) for p in booking.get("providers") or []
                                                                             if isinstance(p, dict) and p.get("pnr")), None),
                    "tickets": _tickets(booking), "billable_minor": _minor(booking.get("billable_price"))}
    return None


def _minor(value) -> int | None:
    try:
        return int(round(float(str(value)) * 100))
    except (TypeError, ValueError):
        return None


def is_cancelled(state: dict[str, Any] | None) -> bool:
    return bool(state) and (state.get("is_cancelled") or str(state.get("reservation_status") or "").lower() in CLOSED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def cancel(reference: str, actor: str | None, factory=None) -> dict[str, Any]:
    """Ask SooperFare to cancel, then read SooperFare's record. Returns the cancel state shown in the admin."""
    record = await run_in_threadpool(_load, reference)
    if record is None:
        raise SupplierCancelError(404, "Booking not found.", "not_found")
    if record.provider != sooperfare.NAME or not record.supplier_ref:
        raise SupplierCancelError(409, "Only a SooperFare booking with a SooperFare reservation can be cancelled here.", "not_sooperfare")
    if record.booking_status != "held":
        raise SupplierCancelError(409, f"Only a held booking can be cancelled (this one is {record.booking_status.replace('_', ' ')}).", "not_held")
    amount = round((record.supplier_cost_minor or 0) / 100, 2)
    client = (factory or sooperfare.SooperfareClient)()
    try:
        answer = await client.cancel(record.supplier_ref, record.reference, amount, record.currency,
                                     f"Travelian booking {record.reference}: cancel this unpaid reservation")
    except sooperfare.SooperfareError as error:
        if error.error_code == "refused":
            message, after = f"SooperFare refused the cancel: {error.public_message}", " Nothing was changed."
        else:  # no clear answer: the cancel may or may not have reached SooperFare
            message = f"SooperFare {NO_ANSWER.get(error.error_code, NO_ANSWER['unreachable'])}, so the cancel may not have gone through."
            after = " The booking is still held here. Press Check with SooperFare before trying again."
        await run_in_threadpool(_save, record, actor, "supplier_cancel_failed",
                                {"supplier": "sooperfare", "error": error.error_code, "http_status": error.status, "message": error.public_message},
                                cancel={"state": "failed", "at": _now(), "message": message})
        raise SupplierCancelError(502, message + after, "cancel_failed") from None
    transaction = ((answer.get("data") or {}).get("transaction") or {}) if isinstance(answer.get("data"), dict) else {}
    answered = {"message": answer.get("message"), "transaction_id": transaction.get("uuid"), "txn_ref_id": transaction.get("txn_ref_id"),
                "transaction_status": transaction.get("status"), "description": transaction.get("description")}
    state = await _read_state(client, record)
    cancelled = is_cancelled(state)
    cancel_state = {"state": "confirmed" if cancelled else "requested", "at": _now(), "answer": answered, "sooperfare": state}
    await run_in_threadpool(_save, record, actor, "supplier_cancel_confirmed" if cancelled else "supplier_cancel_requested",
                            {"supplier": "sooperfare", **answered, "sooperfare_status": state}, cancel=cancel_state,
                            status="cancelled" if cancelled else None)
    return cancel_state


async def _read_state(client, record: Record) -> dict[str, Any] | None:
    try:
        return _sooperfare_record(await client.booking_list(), record.supplier_ref or "")
    except sooperfare.SooperfareError as error:
        logger.warning("SooperFare booking list failed", extra={"error_code": error.error_code})
        return None


async def check(reference: str, actor: str | None, factory=None) -> dict[str, Any]:
    """Read-only check with SooperFare after a cancel request: marks the booking Cancelled once SooperFare's record says so."""
    record = await run_in_threadpool(_load, reference)
    if record is None or record.provider != sooperfare.NAME or not record.supplier_ref:
        raise SupplierCancelError(404, "No SooperFare reservation on this booking.", "not_sooperfare")
    state = await _read_state((factory or sooperfare.SooperfareClient)(), record)
    if state is None:
        raise SupplierCancelError(502, "SooperFare's booking list couldn't be read, or doesn't list this booking. Nothing was changed.", "not_listed")
    previous = record.supplier_response.get("supplier_cancel") or {}
    if is_cancelled(state) and record.booking_status == "held":
        cancel_state = {**previous, "state": "confirmed", "confirmed_at": _now(), "sooperfare": state}
        await run_in_threadpool(_save, record, actor, "supplier_cancel_confirmed", {"supplier": "sooperfare", "sooperfare_status": state},
                                cancel=cancel_state, status="cancelled")
        return cancel_state
    cancel_state = {**previous, "checked_at": _now(), "sooperfare": state} if previous else {"state": "none", "sooperfare": state}
    await run_in_threadpool(_save, record, actor, "supplier_checked", {"supplier": "sooperfare", "sooperfare_status": state},
                            cancel=cancel_state if previous else None)
    return cancel_state


async def cancel_orphan(booking_ref: str, amount_minor: int | None, currency: str, pnr: str, client) -> str:
    """A reservation SooperFare made that Travelian couldn't verify or save: ask SooperFare to cancel it at once, so it
    isn't left open. Returns what happened, in words, for the error shown to staff and the log."""
    try:
        answer = await client.cancel(
            booking_ref, f"UNSAVED-{pnr}", round((amount_minor or 0) / 100, 2), currency, f"Travelian couldn't verify hold {pnr}: cancel it")
        logger.warning("Unverified SooperFare hold: cancel requested", extra={"pnr": pnr, "supplier_message": answer.get("message")})
        return "Travelian asked SooperFare to cancel it"
    except Exception as error:  # noqa: BLE001 - never hides the hold error it is reported in
        logger.error("Unverified SooperFare hold: cancel failed", extra={"pnr": pnr, "error_code": getattr(error, "error_code", type(error).__name__)})
        return "Travelian's cancel request failed, so cancel it in SooperFare's portal"


def view(reference: str, supplier_names: bool) -> dict[str, Any] | None:
    """The admin's SooperFare block (supplier-names permission only): SooperFare's ids and the cancel state."""
    if not supplier_names:
        return None
    record = _load(reference)
    if record is None or record.provider != sooperfare.NAME or not record.supplier_ref:
        return None
    reservation = record.supplier_response.get("reservation") or {}
    cancel_state = record.supplier_response.get("supplier_cancel") or {}
    return {"supplier": "SooperFare", "booking_id": record.supplier_ref, "order_ref": reservation.get("order_ref_id"),
            "cancel_state": cancel_state.get("state", "none"), "cancel_message": cancel_state.get("message"),
            "cancel_at": cancel_state.get("at"), "status_at_supplier": (cancel_state.get("sooperfare") or {}).get("reservation_status"),
            "can_cancel": record.booking_status == "held",
            "can_rehold": record.issuance_status == "not_issued" and (record.booking_status in ("expired", "paid_not_ticketed")
                                                                    or (record.booking_status == "held" and record.hold_over))}


# ---------- confirm (issue) ----------

def confirm_request(reference: str) -> dict[str, Any]:
    """The exact POST /v1/booking/confirm body for a held SooperFare booking (SooperFare's collection: booking_ref_id,
    txn_ref_id, amount, currency, remarks). txn_ref_id: Travelian's booking reference (the collection shows "TEST"; to be
    confirmed with SooperFare). amount: SooperFare's billable price for the booking, in rupees."""
    record = _load(reference)
    if record is None or record.provider != sooperfare.NAME or not record.supplier_ref:
        raise SupplierCancelError(404, "No SooperFare reservation on this booking.", "not_sooperfare")
    if record.booking_status != "held":
        raise SupplierCancelError(409, f"Only a held booking can be confirmed (this one is {record.booking_status.replace('_', ' ')}).", "not_held")
    return {"method": "POST", "path": "/v1/booking/confirm",
            "body": {"booking_ref_id": record.supplier_ref, "txn_ref_id": record.reference,
                     "amount": round((record.supplier_cost_minor or 0) / 100, 2), "currency": record.currency,
                     "remarks": f"Travelian booking {record.reference}, PNR {record.pnr}"}}


async def confirm(reference: str, actor: str | None, factory=None) -> dict[str, Any]:
    """Issue at SooperFare. Refused unless SOOPERFARE_CONFIRM_ENABLED is on (the owner approves the first real call)."""
    from app.core.config import settings

    request = await run_in_threadpool(confirm_request, reference)
    if not settings.sooperfare_confirm_enabled:
        raise SupplierCancelError(409, "Issuing at SooperFare is off until the owner approves it. Nothing was sent.", "confirm_off")
    body = request["body"]
    record = await run_in_threadpool(_load, reference)
    try:
        answer = await (factory or sooperfare.SooperfareClient)().confirm(body["booking_ref_id"], body["txn_ref_id"], body["amount"],
                                                                         body["currency"], body["remarks"])
    except sooperfare.SooperfareError as error:
        await run_in_threadpool(_save, record, actor, "supplier_confirm_failed", {"supplier": "sooperfare", "error": error.error_code,
                                                                                  "http_status": error.status, "message": error.public_message})
        raise SupplierCancelError(502, f"SooperFare didn't confirm: {error.public_message} Check SooperFare before trying again.", "confirm_failed") from None
    await run_in_threadpool(_save, record, actor, "supplier_confirm_answered", {"supplier": "sooperfare", "message": answer.get("message")})
    return answer
