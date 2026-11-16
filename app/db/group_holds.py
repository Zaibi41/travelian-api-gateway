"""Group Ticketing holds in `bookings` (product 'group'), with audit entries in `booking_events`.

A group hold is created by supplier_bookings.save_hold(product="group"). Its state changes here
lock the booking row and check the current state in the same transaction:
- expire: a held booking whose hold time has passed becomes 'expired' (it is never held again).
- release: a held booking becomes 'cancelled' after the supplier confirmed the release.
- accept_cost: the issuer confirmed the re-checked supplier cost; only the cost changes (booking and travelers).
- release state (supplier_response -> 'release'): whether the seats were given back at the supplier
  (pending / released / failed, attempts, last error). The expiry job retries pending releases.
supplier_ref and supplier_response never leave the gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event

RELEASE_PENDING, RELEASE_DONE, RELEASE_FAILED = "pending", "released", "failed"


class GroupHoldRefused(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class GroupBooking:
    id: str
    reference: str
    pnr: str | None
    provider: str | None
    booking_status: str
    issuance_status: str
    payment_status: str
    hold_expires_at: datetime | None
    currency: str
    total_minor: int
    supplier_cost_minor: int | None
    supplier_ref: str | None
    itinerary: list[dict[str, Any]]
    adults: int
    paid_minor: int
    release: dict[str, Any]  # {} until the seats need releasing; then status, attempts, last_error, updated_at
    #: Group identity copied from the listing when the hold was created (supplier_response -> 'source'):
    #: provider, supplier_pnr, supplier_group_id, supplier_ticket_id. {} for holds made before it was kept.
    source: dict[str, Any] = field(default_factory=dict)
    #: Set when the supplier held but the booking couldn't be completed: supplier_pnr, error, at.
    unrecorded: dict[str, Any] = field(default_factory=dict)
    #: The supplier held at a different cost than shown: shown_cost_total_minor, held_cost_total_minor, accepted.
    price_change: dict[str, Any] = field(default_factory=dict)
    #: The supplier's own booking amounts and states (Dhillu & Sahi: total_amount, cancel_status, ...), as last read.
    supplier_amounts: dict[str, Any] = field(default_factory=dict)
    #: Our ticket id (GroupTicketView.id) the hold was made on, to search again.
    ticket_id: str | None = None
    #: Where the hold time came from: {"source": "default", "minutes": 45, "from": ...}; {} for GROUP_HOLD_MINUTES holds.
    hold_time: dict[str, Any] = field(default_factory=dict)

    @property
    def price_change_pending(self) -> bool:
        return bool(self.price_change) and not self.price_change.get("accepted")

    @property
    def supplier_cancelled(self) -> bool:
        return str(self.supplier_amounts.get("cancel_status", "")).strip() == "1"

    def __repr__(self) -> str:  # never print the supplier reference
        return f"GroupBooking({self.reference}, {self.booking_status})"

    @property
    def release_failed(self) -> bool:
        """The supplier still holds the seats after a failed release (retrying or given up)."""
        return self.release.get("status") in (RELEASE_PENDING, RELEASE_FAILED) and int(self.release.get("attempts") or 0) > 0


def _load(connection, reference: str, lock: bool) -> GroupBooking:
    row = connection.execute(
        text("SELECT id, reference, pnr, provider_id, booking_status, issuance_status, payment_status, hold_expires_at, currency,"
             " total_minor, supplier_cost_minor, supplier_ref, itinerary, supplier_response -> 'adults' AS adults,"
             " supplier_response -> 'release' AS release, supplier_response -> 'source' AS source,"
             " supplier_response -> 'unrecorded' AS unrecorded, supplier_response -> 'price_change' AS price_change,"
             " supplier_response -> 'supplier_amounts' AS supplier_amounts, supplier_response -> 'ticket' ->> 'id' AS ticket_id,"
             " supplier_response -> 'hold_time' AS hold_time"
             " FROM bookings WHERE reference = :r AND product = 'group'" + (" FOR UPDATE" if lock else "")),
        {"r": reference},
    ).mappings().one_or_none()
    if row is None or row["booking_status"] in ("hold_pending", "hold_failed"):
        raise GroupHoldRefused(404, "Group booking not found.", "not_found")
    paid = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments WHERE booking_id = :b"), {"b": row["id"]})
    adults = row["adults"] if isinstance(row["adults"], int) else connection.scalar(
        text("SELECT count(*) FROM booking_travelers WHERE booking_id = :b"), {"b": row["id"]})
    return GroupBooking(row["id"], row["reference"], row["pnr"], row["provider_id"], row["booking_status"], row["issuance_status"],
                        row["payment_status"], row["hold_expires_at"], row["currency"], row["total_minor"], row["supplier_cost_minor"],
                        row["supplier_ref"], row["itinerary"] if isinstance(row["itinerary"], list) else [], int(adults or 0), int(paid),
                        row["release"] if isinstance(row["release"], dict) else {},
                        row["source"] if isinstance(row["source"], dict) else {},
                        row["unrecorded"] if isinstance(row["unrecorded"], dict) else {},
                        row["price_change"] if isinstance(row["price_change"], dict) else {},
                        row["supplier_amounts"] if isinstance(row["supplier_amounts"], dict) else {}, row["ticket_id"],
                        row["hold_time"] if isinstance(row["hold_time"], dict) else {})


def load(reference: str) -> GroupBooking:
    with agencies.application_engine().begin() as connection:
        return _load(connection, reference, lock=False)


def is_over(booking: GroupBooking, now: datetime) -> bool:
    return booking.hold_expires_at is not None and booking.hold_expires_at <= now


def _set_release(connection, booking_id: str, state: dict[str, Any]) -> None:
    connection.execute(
        text("UPDATE bookings SET supplier_response = COALESCE(supplier_response, '{}'::jsonb) || jsonb_build_object('release', CAST(:s AS jsonb)),"
             " updated_at = now(), revision = revision + 1 WHERE id = :b"),
        {"s": json.dumps(state, default=str), "b": booking_id},
    )


RELEASE_NOT_AVAILABLE = "not_available"  # the source has no release call: the hold ends at the supplier by itself


def _expire(connection, booking: GroupBooking, now: datetime, no_release: frozenset[str] = frozenset()) -> None:
    """held -> expired; seats to be released at the supplier by the expiry job, unless the source has no release call."""
    connection.execute(text("UPDATE bookings SET booking_status = 'expired', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                       {"b": booking.id})
    status = RELEASE_NOT_AVAILABLE if booking.provider in no_release else RELEASE_PENDING
    _set_release(connection, booking.id, {"status": status, "attempts": 0, "updated_at": now.isoformat()})
    _event(connection, booking.id, None, "hold_expired", {"pnr": booking.pnr, "time_limit": booking.hold_expires_at.isoformat() if booking.hold_expires_at else None})


def _expirable(booking: GroupBooking, now: datetime) -> bool:
    return booking.booking_status == "held" and is_over(booking, now) and booking.issuance_status in ("not_issued", "requested")


def expire_if_over(reference: str, now: datetime, no_release: frozenset[str] = frozenset()) -> GroupBooking:
    """Mark a held booking whose time limit has passed as expired (once, with an audit entry).
    Leaves a booking that is being issued, or whose issuing is unclear, alone."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if _expirable(booking, now):
            _expire(connection, booking, now, no_release)
            booking = _load(connection, reference, lock=False)
        return booking


def expire_due(now: datetime, no_release: frozenset[str] = frozenset()) -> list[str]:
    """Every held group booking past its time limit becomes expired. Returns their references."""
    with agencies.application_engine().begin() as connection:
        references = connection.execute(
            text("SELECT reference FROM bookings WHERE product = 'group' AND booking_status = 'held' AND hold_expires_at <= :now"
                 " AND issuance_status IN ('not_issued', 'requested') ORDER BY hold_expires_at"), {"now": now},
        ).scalars().all()
    expired = []
    for reference in references:
        with agencies.application_engine().begin() as connection:
            booking = _load(connection, reference, lock=True)
            if _expirable(booking, now):
                _expire(connection, booking, now, no_release)
                expired.append(reference)
    return expired


def releases_due(max_attempts: int) -> list[GroupBooking]:
    """Bookings whose seats still need releasing at the supplier (pending, under the attempt limit)."""
    with agencies.application_engine().begin() as connection:
        references = connection.execute(
            text("SELECT reference FROM bookings WHERE product = 'group' AND supplier_response -> 'release' ->> 'status' = :pending"
                 " AND COALESCE((supplier_response -> 'release' ->> 'attempts')::int, 0) < :max ORDER BY updated_at"),
            {"pending": RELEASE_PENDING, "max": max_attempts},
        ).scalars().all()
        return [_load(connection, reference, lock=False) for reference in references]


def mark_release_attempt(reference: str, actor: str | None, ok: bool, error: str | None, now: datetime, max_attempts: int) -> GroupBooking:
    """Record one supplier release call: released, or another failed attempt (failed once the limit is reached)."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        attempts = int(booking.release.get("attempts") or 0) + 1
        if ok:
            _set_release(connection, booking.id, {"status": RELEASE_DONE, "attempts": attempts, "updated_at": now.isoformat()})
            _event(connection, booking.id, actor, "supplier_released", {"pnr": booking.pnr})
        else:
            status = RELEASE_FAILED if attempts >= max_attempts else RELEASE_PENDING
            _set_release(connection, booking.id, {"status": status, "attempts": attempts, "last_error": error, "updated_at": now.isoformat()})
            _event(connection, booking.id, actor, "supplier_release_failed", {"pnr": booking.pnr, "supplier_error": error, "reason_code": status})
        return _load(connection, reference, lock=False)


def mark_released(reference: str, actor: str, now: datetime) -> None:
    """Staff released the hold and the supplier confirmed: the booking is cancelled (released)."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if booking.booking_status != "held" or booking.issuance_status not in ("not_issued", "requested"):
            raise GroupHoldRefused(409, "The booking changed meanwhile. Nothing was released.", "changed")
        connection.execute(text("UPDATE bookings SET booking_status = 'cancelled', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                           {"b": booking.id})
        _set_release(connection, booking.id, {"status": RELEASE_DONE, "attempts": 1, "updated_at": now.isoformat()})
        _event(connection, booking.id, actor, "hold_released", {"pnr": booking.pnr})


def mark_release_failed_on_request(reference: str, actor: str, error: str | None, now: datetime) -> None:
    """Staff released the hold but the supplier didn't confirm: the booking reads as expired and the
    expiry job keeps trying to release the seats."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if booking.booking_status != "held":
            raise GroupHoldRefused(409, "The booking changed meanwhile.", "changed")
        connection.execute(text("UPDATE bookings SET booking_status = 'expired', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                           {"b": booking.id})
        _set_release(connection, booking.id, {"status": RELEASE_PENDING, "attempts": 1, "last_error": error, "updated_at": now.isoformat()})
        _event(connection, booking.id, actor, "hold_released", {"pnr": booking.pnr, "supplier_error": error, "reason_code": "supplier_release_failed"})


def accept_cost(reference: str, actor: str, expected_cost_minor: int | None, supplier_cost_minor: int) -> GroupBooking:
    """The issuer confirmed the re-checked supplier cost. Only the cost changes (booking and each traveler); the selling
    price, the travelers' selling prices, payments and Due stay as they are (app.services.pricing)."""
    from app.services.pricing import split

    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if booking.supplier_cost_minor != expected_cost_minor or booking.booking_status != "held":
            raise GroupHoldRefused(409, "The booking changed meanwhile. Check the price again.", "changed")
        connection.execute(text("UPDATE bookings SET supplier_cost_minor = :c, updated_at = now(), revision = revision + 1 WHERE id = :b"),
                           {"c": supplier_cost_minor, "b": booking.id})
        travelers = connection.execute(text("SELECT id FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
                                       {"b": booking.id}).scalars().all()
        for traveler_id, cost in zip(travelers, split(supplier_cost_minor, len(travelers))):
            connection.execute(text("UPDATE booking_travelers SET cost_minor = :c WHERE id = :t"), {"c": cost, "t": traveler_id})
        _event(connection, booking.id, actor, "supplier_cost_accepted",
               {"amount_minor": supplier_cost_minor, "previous_amount_minor": expected_cost_minor, "currency": booking.currency,
                "below_cost": supplier_cost_minor > booking.total_minor})
        return _load(connection, reference, lock=False)


# ---------- the hold record, written before the supplier is called ----------

HOLD_PENDING, HOLD_FAILED, HOLD_UNRECORDED = "hold_pending", "hold_failed", "hold_unrecorded"


def create_pending(*, provider: str, currency: str, selling_total_minor: int, supplier_total_minor: int | None,
                   supplier_response: dict[str, Any], actor_user_id: str, customer, travelers: list, itinerary: list[dict[str, Any]],
                   reference_prefix: str, fare_terms: dict[str, Any] | None = None) -> tuple[str, str]:
    """Our record before the supplier is called: 'hold_pending', no PNR yet, with its customer and travelers.
    Returns (booking id, reference)."""
    from uuid import uuid4

    from app.db.supplier_bookings import _save_people, new_reference

    with agencies.application_engine().begin() as connection:
        booking_id, reference = str(uuid4()), new_reference(reference_prefix)
        connection.execute(
            text("INSERT INTO bookings (id, reference, product, provider_id, created_by_user_id, booking_status, payment_status,"
                 " issuance_status, currency, total_minor, supplier_cost_minor, supplier_response, itinerary)"
                 " VALUES (:id, :ref, 'group', :provider, :actor, :status, 'unpaid', 'not_issued', :currency, :total, :cost,"
                 " CAST(:response AS jsonb), CAST(:itinerary AS jsonb))"),
            {"id": booking_id, "ref": reference, "provider": provider, "actor": actor_user_id, "status": HOLD_PENDING, "currency": currency,
             "total": selling_total_minor, "cost": supplier_total_minor, "response": json.dumps(supplier_response, default=str),
             "itinerary": json.dumps(itinerary) if itinerary else None},
        )
        if customer is not None:
            _save_people(connection, booking_id, customer, travelers, selling_total_minor, supplier_total_minor)
        _event(connection, booking_id, actor_user_id, "hold_requested", {"travelers": len(travelers)})
        if fare_terms:  # the non-refundable, non-changeable acknowledgement: who (the actor), when, and the text shown
            _event(connection, booking_id, actor_user_id, "fare_terms_acknowledged", fare_terms)
    return booking_id, reference


def mark_hold_failed(booking_id: str, actor: str | None, reason_code: str) -> None:
    """The supplier didn't hold (or didn't confirm): the record stays as 'hold_failed', nothing pending."""
    with agencies.application_engine().begin() as connection:
        connection.execute(
            text("UPDATE bookings SET booking_status = :s, supplier_response = COALESCE(supplier_response, '{}'::jsonb)"
                 " || jsonb_build_object('hold_failure', CAST(:r AS text)), updated_at = now(), revision = revision + 1"
                 " WHERE id = :b AND booking_status = :pending"),
            {"s": HOLD_FAILED, "r": reason_code, "b": booking_id, "pending": HOLD_PENDING},
        )
        _event(connection, booking_id, actor, "hold_failed", {"reason_code": reason_code})


def confirm_hold(booking_id: str, actor: str, *, pnr: str | None, supplier_ref: str, hold_expires_at: datetime,
                 selling_total_minor: int, supplier_total_minor: int, supplier_response: dict[str, Any], audit: dict[str, Any]) -> None:
    """The supplier held: 'hold_pending' -> 'held' with the PNR, supplier reference, totals and the supplier's answer."""
    with agencies.application_engine().begin() as connection:
        updated = connection.execute(
            text("UPDATE bookings SET booking_status = 'held', pnr = :pnr, supplier_ref = :ref, hold_expires_at = :expires,"
                 " total_minor = :total, supplier_cost_minor = :cost,"
                 " supplier_response = COALESCE(supplier_response, '{}'::jsonb) || CAST(:response AS jsonb),"
                 " updated_at = now(), revision = revision + 1 WHERE id = :b AND booking_status = :pending"),
            {"pnr": pnr, "ref": supplier_ref, "expires": hold_expires_at, "total": selling_total_minor, "cost": supplier_total_minor,
             "response": json.dumps(supplier_response, default=str), "b": booking_id, "pending": HOLD_PENDING},
        ).rowcount
        if updated != 1:
            raise RuntimeError("hold record is no longer pending")
        # Each traveler's cost follows the cost the supplier held at (their selling price stays).
        from app.services.pricing import split

        ids = connection.execute(text("SELECT id FROM booking_travelers WHERE booking_id = :b ORDER BY position"), {"b": booking_id}).scalars().all()
        for traveler_id, cost in zip(ids, split(supplier_total_minor, len(ids))):
            connection.execute(text("UPDATE booking_travelers SET cost_minor = :c WHERE id = :t"), {"c": cost, "t": traveler_id})
        _event(connection, booking_id, actor, "hold_created", audit)


def mark_unrecorded(booking_id: str, actor: str | None, *, supplier_pnr: str | None, supplier_ref: str | None, error: str,
                    now: datetime, release_pending: bool) -> None:
    """The supplier held but confirm_hold failed. Keep the supplier PNR (in supplier_response, which has no format rule)
    and the supplier reference on a 'hold_unrecorded' record. release_pending: the seats are to be released
    (Skypass); the expiry job retries up to GROUP_HOLD_RELEASE_ATTEMPTS."""
    state: dict[str, Any] = {"unrecorded": {"supplier_pnr": supplier_pnr, "error": error, "at": now.isoformat()}}
    if release_pending:
        state["release"] = {"status": RELEASE_PENDING, "attempts": 0, "updated_at": now.isoformat()}
    with agencies.application_engine().begin() as connection:
        connection.execute(
            text("UPDATE bookings SET booking_status = :s, supplier_ref = :ref,"
                 " supplier_response = COALESCE(supplier_response, '{}'::jsonb) || CAST(:state AS jsonb),"
                 " updated_at = now(), revision = revision + 1 WHERE id = :b"),
            {"s": HOLD_UNRECORDED, "ref": supplier_ref, "state": json.dumps(state), "b": booking_id},
        )
        _event(connection, booking_id, actor, "hold_not_recorded", {"pnr": supplier_pnr, "reason_code": error})


def mark_unrecorded_released(reference: str, actor: str | None) -> None:
    """The seats of an unrecorded hold were given back: the record reads as released (cancelled)."""
    with agencies.application_engine().begin() as connection:
        connection.execute(text("UPDATE bookings SET booking_status = 'cancelled', updated_at = now(), revision = revision + 1"
                                " WHERE reference = :r AND booking_status = :s"), {"r": reference, "s": HOLD_UNRECORDED})


def save_supplier_amounts(reference: str, booking_amounts: dict[str, Any], cancelled: bool, now: datetime,
                          supplier_expiry: datetime | None = None) -> GroupBooking:
    """The supplier's amounts as just read. Cancelled at the supplier (cancel_status '1'): a held booking becomes expired
    (nothing to release), so payments and issuing are refused. supplier_expiry: a usable supplier expiry read for a hold
    still on the default time; it becomes the hold's expiry (source 'supplier')."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        connection.execute(
            text("UPDATE bookings SET supplier_response = COALESCE(supplier_response, '{}'::jsonb)"
                 " || jsonb_build_object('supplier_amounts', CAST(:a AS jsonb)), updated_at = now(), revision = revision + 1 WHERE id = :b"),
            {"a": json.dumps({**booking_amounts, "read_at": now.isoformat()}, default=str), "b": booking.id},
        )
        if supplier_expiry is not None and not cancelled and booking.booking_status == "held" and booking.hold_time.get("source") == "default":
            connection.execute(
                text("UPDATE bookings SET hold_expires_at = :e, supplier_response = jsonb_set(supplier_response, '{hold_time,source}', '\"supplier\"'::jsonb),"
                     " updated_at = now(), revision = revision + 1 WHERE id = :b"), {"e": supplier_expiry, "b": booking.id})
            _event(connection, booking.id, None, "hold_time_from_supplier",
                   {"time_limit": supplier_expiry.isoformat(), "previous_time_limit": booking.hold_expires_at.isoformat() if booking.hold_expires_at else None})
        if cancelled and booking.booking_status == "held" and booking.issuance_status in ("not_issued", "requested"):
            connection.execute(text("UPDATE bookings SET booking_status = 'expired', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                               {"b": booking.id})
            _set_release(connection, booking.id, {"status": RELEASE_NOT_AVAILABLE, "attempts": 0, "updated_at": now.isoformat()})
            _event(connection, booking.id, None, "supplier_cancelled", {"pnr": booking.pnr, "reason_code": "cancel_status_1"})
        return _load(connection, reference, lock=False)


def accept_held_price(reference: str, actor: str) -> GroupBooking:
    """The customer agreed to the price the supplier held at (shown before the hold was different)."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if not booking.price_change_pending:
            raise GroupHoldRefused(409, "There is no price change to confirm.", "no_price_change")
        if booking.booking_status != "held":
            raise GroupHoldRefused(409, "This booking isn't on hold.", "not_held")
        if booking.supplier_cost_minor is not None and booking.total_minor < booking.supplier_cost_minor:
            raise GroupHoldRefused(409, "The selling price is below the new supplier cost. Set a higher selling price first.", "below_cost")
        connection.execute(
            text("UPDATE bookings SET supplier_response = jsonb_set(supplier_response, '{price_change,accepted}', 'true'::jsonb),"
                 " updated_at = now(), revision = revision + 1 WHERE id = :b"), {"b": booking.id})
        _event(connection, booking.id, actor, "held_price_accepted",
               {"amount_minor": booking.total_minor, "currency": booking.currency})
        return _load(connection, reference, lock=False)
