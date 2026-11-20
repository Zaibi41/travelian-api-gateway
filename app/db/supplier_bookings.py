"""Supplier holds in the `bookings` table, with audit entries in `booking_events`.

supplier_ref (e.g. Southair ticket_ref) and supplier_response never leave the gateway.
The customer and travelers of a hold are saved in the same transaction (migrations 0011, 0015).
Sandbox phase: date of birth and document number are stored as plain text and masked on read
(docs/pii-storage.md). Audit details hold no personal data.
"""

from __future__ import annotations

import json
import secrets
import string
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies

UAT_SOURCE = "southair_uat"
_REF_ALPHABET = string.ascii_uppercase + string.digits


def new_reference(prefix: str = "TRV-") -> str:
    return prefix + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def _event(connection, booking_id: str, actor: str | None, action: str, details: dict[str, Any]) -> None:
    connection.execute(
        # clock_timestamp(): the real time of each event, so events in one transaction keep their order.
        text("INSERT INTO booking_events (id, booking_id, actor_user_id, action, details, created_at)"
             " VALUES (:id, :b, :a, :action, CAST(:d AS jsonb), clock_timestamp())"),
        {"id": str(uuid4()), "b": booking_id, "a": actor, "action": action, "d": json.dumps(details)},
    )


@dataclass(frozen=True)
class CustomerRecord:
    first_name: str
    last_name: str
    phone_code: str
    phone: str
    email: str


@dataclass(frozen=True)
class TravelerRecord:
    passenger_type: str
    title: str
    first_name: str
    last_name: str
    date_of_birth: date
    id_type: str
    document_number: str | None = None
    document_country: str | None = None
    nationality: str | None = None
    document_expiry: date | None = None
    infant_of_position: int | None = None  # position of the adult an infant travels with
    person_id: str | None = None           # saved person this copy came from (traveler_id)
    document_id: str | None = None
    selling_minor: int | None = None       # this traveler's selling price; None: an even share of the booking total
    cost_minor: int | None = None          # this traveler's supplier cost; None: an even share of the cost total

    def __repr__(self) -> str:  # never print identity data
        return f"TravelerRecord(type={self.passenger_type})"


def _save_people(connection, booking_id: str, customer: CustomerRecord, travelers: list[TravelerRecord],
                 selling_total_minor: int = 0, cost_total_minor: int | None = None) -> None:
    from app.services.pricing import split

    sellings = split(selling_total_minor, len(travelers))
    costs = split(cost_total_minor if cost_total_minor is not None else selling_total_minor, len(travelers))
    connection.execute(
        text("INSERT INTO booking_customers (booking_id, first_name, last_name, phone_code, phone, email)"
             " VALUES (:b, :first, :last, :code, :phone, :email)"),
        {"b": booking_id, "first": customer.first_name, "last": customer.last_name, "code": customer.phone_code,
         "phone": customer.phone, "email": customer.email},
    )
    for position, traveler in enumerate(travelers):
        connection.execute(
            text("INSERT INTO booking_travelers (id, booking_id, position, passenger_type, title, first_name, last_name,"
                 " date_of_birth, id_type, document_number, document_country, nationality, document_expiry, infant_of_position, person_id, document_id,"
                 " selling_minor, cost_minor)"
                 " VALUES (:id, :b, :pos, :type, :title, :first, :last, :dob, :id_type, :doc, :country, :nationality, :expiry, :infant_of, :person, :document,"
                 " :selling, :cost)"),
            {"id": str(uuid4()), "b": booking_id, "pos": position, "type": traveler.passenger_type, "title": traveler.title,
             "first": traveler.first_name, "last": traveler.last_name, "dob": traveler.date_of_birth, "id_type": traveler.id_type,
             "doc": (traveler.document_number or "").strip() or None, "country": traveler.document_country,
             "nationality": traveler.nationality, "expiry": traveler.document_expiry, "infant_of": traveler.infant_of_position,
             "person": traveler.person_id, "document": traveler.document_id,
             "selling": traveler.selling_minor if traveler.selling_minor is not None else sellings[position],
             "cost": traveler.cost_minor if traveler.cost_minor is not None else costs[position]},
        )


def save_request(*, provider: str, currency: str, selling_total_minor: int, supplier_total_minor: int | None,
                 actor_user_id: str | None, customer: CustomerRecord, travelers: list[TravelerRecord], itinerary: list[dict[str, Any]],
                 details: dict[str, Any], channel: str = "staff", product: str = "flight") -> str:
    """A booking request (no supplier hold, no PNR): status 'requested'. Staff confirm the fare and payment with the
    customer and book it themselves (restricted airlines, D32). Returns the reference."""
    with agencies.application_engine().begin() as connection:
        booking_id, reference = str(uuid4()), new_reference("TRV-R-")
        connection.execute(
            text("INSERT INTO bookings (id, reference, product, provider_id, created_by_user_id, booking_status, payment_status,"
                 " issuance_status, currency, total_minor, supplier_cost_minor, supplier_response, itinerary, channel)"
                 " VALUES (:id, :ref, :product, :provider, :actor, 'requested', 'unpaid', 'not_issued', :currency, :total, :cost,"
                 " CAST(:response AS jsonb), CAST(:itinerary AS jsonb), :channel)"),
            {"id": booking_id, "ref": reference, "product": product, "provider": provider, "actor": actor_user_id, "currency": currency,
             "total": selling_total_minor, "cost": supplier_total_minor, "response": json.dumps({"request": details}),
             "itinerary": json.dumps(itinerary) if itinerary else None, "channel": channel},
        )
        _save_people(connection, booking_id, customer, travelers, selling_total_minor, supplier_total_minor)
        _event(connection, booking_id, actor_user_id, "booking_requested", {**details, "travelers": len(travelers), "channel": channel})
    return reference


# A staff re-hold (app.services.flight_fallback.staff_rehold): who asked, and the changed selling total they accepted
# (None: the same total only). Set for the one call; the automatic re-hold after a card payment never changes the price.
# The third value: the supplier reference staff saw (the new hold is saved only if the booking still has it).
STAFF_REHOLD: ContextVar[tuple[str | None, int | None, str | None] | None] = ContextVar("staff_rehold", default=None)
REHOLD_LOCK_MINUTES = 5


def claim_rehold(reference: str, supplier_ref: str) -> bool:
    """One staff re-hold at a time per booking: set the lock if the booking still has this supplier reference and no
    lock younger than REHOLD_LOCK_MINUTES (a lock left by a crash expires). False: another re-hold is running, or the
    booking changed meanwhile."""
    with agencies.application_engine().begin() as connection:
        return connection.execute(text(
            "UPDATE bookings SET supplier_response = COALESCE(supplier_response, '{}'::jsonb)"
            " || jsonb_build_object('rehold_lock', CAST(now() AS text))"
            " WHERE reference = :r AND supplier_ref = :ref AND (supplier_response ->> 'rehold_lock' IS NULL"
            " OR CAST(supplier_response ->> 'rehold_lock' AS timestamptz) < now() - make_interval(mins => :m)) RETURNING id"),
            {"r": reference, "ref": supplier_ref, "m": REHOLD_LOCK_MINUTES}).first() is not None


def release_rehold(reference: str) -> None:
    with agencies.application_engine().begin() as connection:
        connection.execute(text("UPDATE bookings SET supplier_response = supplier_response - 'rehold_lock' WHERE reference = :r"),
                           {"r": reference})
REHOLD_STATUSES = ("held", "pending_payment", "payment_received", "expired", "paid_not_ticketed")


def replace_hold(reference: str, *, provider: str, pnr: str, supplier_ref: str, hold_expires_at: datetime | None,
                 supplier_total_minor: int, selling_total_minor: int, supplier_response: dict[str, Any]) -> str:
    """A new supplier hold (re-hold of the same fare) for an existing paid booking whose hold was lost. Only while the booking
    is open, not issued, from the same provider and at the same selling total; travelers' costs are rescaled to the new
    supplier total. The previous PNR is kept on the booking and in the audit log (rehold_created)."""
    from app.services.pricing import scale

    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT id, pnr, provider_id, booking_status, issuance_status, total_minor, supplier_ref FROM bookings WHERE reference = :r FOR UPDATE"),
            {"r": reference}).one_or_none()
        actor, accepted, expected_ref = STAFF_REHOLD.get() or (None, None, None)
        if (row is None or (expected_ref is not None and row.supplier_ref != expected_ref) or row.booking_status not in REHOLD_STATUSES or row.issuance_status != "not_issued"
                or (row.provider_id or "southair") != provider
                or (row.total_minor != selling_total_minor and accepted != selling_total_minor)):
            raise ValueError("The booking can't take a new hold.")
        if row.total_minor != selling_total_minor:  # a new price the staff member accepted
            from app.db.booking_payments import payment_status

            paid = connection.execute(text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments WHERE booking_id = :b"),
                                      {"b": row.id}).scalar_one()
            sellings = connection.execute(text("SELECT id, selling_minor FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
                                          {"b": row.id}).all()
            for (traveler_id, _), selling in zip(sellings, scale([previous or 0 for _, previous in sellings], selling_total_minor)):
                connection.execute(text("UPDATE booking_travelers SET selling_minor = :s WHERE id = :t"), {"s": selling, "t": traveler_id})
            connection.execute(text("UPDATE bookings SET total_minor = :t, payment_status = :p WHERE id = :b"),
                               {"t": selling_total_minor, "p": payment_status(paid, selling_total_minor), "b": row.id})
            _event(connection, row.id, actor, "selling_price_set", {"amount_minor": selling_total_minor,
                                                                     "previous_amount_minor": row.total_minor, "reason": "rehold"})
        connection.execute(text(
            "UPDATE bookings SET pnr = :pnr, supplier_ref = :ref, hold_expires_at = :exp, supplier_cost_minor = :cost,"
            " booking_status = 'held', supplier_response = COALESCE(supplier_response, '{}'::jsonb)"
            " || jsonb_build_object('rehold', CAST(:response AS jsonb), 'previous_pnr', CAST(:previous AS text)),"
            " updated_at = now(), revision = revision + 1 WHERE id = :b"),
            {"pnr": pnr, "ref": supplier_ref, "exp": hold_expires_at, "cost": supplier_total_minor, "b": row.id,
             "response": json.dumps(supplier_response, default=str), "previous": row.pnr})
        travelers = connection.execute(text("SELECT id, cost_minor FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
                                       {"b": row.id}).all()
        for (traveler_id, _), cost in zip(travelers, scale([previous or 0 for _, previous in travelers], supplier_total_minor)):
            connection.execute(text("UPDATE booking_travelers SET cost_minor = :c WHERE id = :t"), {"c": cost, "t": traveler_id})
        _event(connection, row.id, actor, "rehold_created", {"pnr": pnr, "previous_pnr": row.pnr, "by_staff": actor is not None,
                                                             "time_limit": hold_expires_at.isoformat() if hold_expires_at else None,
                                                             "supplier_total_minor": supplier_total_minor})
    return reference


def save_hold(*, provider: str, pnr: str, supplier_ref: str, hold_expires_at: datetime | None, currency: str,
              selling_total_minor: int, supplier_total_minor: int, supplier_response: dict[str, Any],
              actor_user_id: str, audit: dict[str, Any],
              customer: CustomerRecord | None = None, travelers: list[TravelerRecord] | None = None,
              itinerary: list[dict[str, Any]] | None = None, product: str = "flight", source: str | None = UAT_SOURCE,
              reference_prefix: str = "TRV-") -> str:
    """Insert a held, unpaid, not-issued booking (Southair: UAT test data; Group Ticketing: product
    'group', no import source), its customer and travelers, and the audit entry, in one
    transaction. Returns the reference."""
    with agencies.application_engine().begin() as connection:
        booking_id, reference = str(uuid4()), new_reference(reference_prefix)
        connection.execute(
            text(
                "INSERT INTO bookings (id, reference, pnr, product, provider_id, created_by_user_id, booking_status, payment_status,"
                " issuance_status, hold_expires_at, currency, total_minor, supplier_cost_minor, import_source, supplier_ref, supplier_response, itinerary)"
                " VALUES (:id, :ref, :pnr, :product, :provider, :actor, 'held', 'unpaid', 'not_issued', :expires, :currency, :total,"
                " :cost, :source, :supplier_ref, CAST(:response AS jsonb), CAST(:itinerary AS jsonb))"
            ),
            {"id": booking_id, "ref": reference, "pnr": pnr, "provider": provider, "actor": actor_user_id, "expires": hold_expires_at,
             "currency": currency, "total": selling_total_minor, "cost": supplier_total_minor, "source": source, "product": product,
             "supplier_ref": supplier_ref, "response": json.dumps(supplier_response),
             "itinerary": json.dumps(itinerary) if itinerary else None},
        )
        if customer is not None:
            _save_people(connection, booking_id, customer, travelers or [], selling_total_minor, supplier_total_minor)
        _event(connection, booking_id, actor_user_id, "hold_created",
               {**audit, **({"label": "UAT test data"} if source == UAT_SOURCE else {}), "travelers": len(travelers or [])})
    return reference
