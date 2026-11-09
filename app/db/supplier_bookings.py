"""Supplier holds in the `bookings` table, with audit entries in `booking_events`.

supplier_ref (e.g. Southair ticket_ref) and supplier_response never leave the gateway.
No passenger data is stored here.
"""

from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies

UAT_SOURCE = "southair_uat"
_REF_ALPHABET = string.ascii_uppercase + string.digits


def new_reference() -> str:
    return "TRV-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def _event(connection, booking_id: str, actor: str | None, action: str, details: dict[str, Any]) -> None:
    connection.execute(
        text("INSERT INTO booking_events (id, booking_id, actor_user_id, action, details) VALUES (:id, :b, :a, :action, CAST(:d AS jsonb))"),
        {"id": str(uuid4()), "b": booking_id, "a": actor, "action": action, "d": json.dumps(details)},
    )


def save_hold(*, provider: str, pnr: str, supplier_ref: str, hold_expires_at: datetime | None, currency: str,
              selling_total_minor: int, supplier_total_minor: int, supplier_response: dict[str, Any],
              actor_user_id: str, audit: dict[str, Any]) -> str:
    """Insert a held, unpaid, not-issued UAT booking plus its audit entry. Returns the reference."""
    with agencies.application_engine().begin() as connection:
        booking_id, reference = str(uuid4()), new_reference()
        connection.execute(
            text(
                "INSERT INTO bookings (id, reference, pnr, product, provider_id, created_by_user_id, booking_status, payment_status,"
                " issuance_status, hold_expires_at, currency, total_minor, supplier_cost_minor, import_source, supplier_ref, supplier_response)"
                " VALUES (:id, :ref, :pnr, 'flight', :provider, :actor, 'held', 'unpaid', 'not_issued', :expires, :currency, :total,"
                " :cost, :source, :supplier_ref, CAST(:response AS jsonb))"
            ),
            {"id": booking_id, "ref": reference, "pnr": pnr, "provider": provider, "actor": actor_user_id, "expires": hold_expires_at,
             "currency": currency, "total": selling_total_minor, "cost": supplier_total_minor, "source": UAT_SOURCE,
             "supplier_ref": supplier_ref, "response": json.dumps(supplier_response)},
        )
        _event(connection, booking_id, actor_user_id, "hold_created", {**audit, "label": "UAT test data"})
    return reference


@dataclass(frozen=True)
class TicketingTarget:
    id: str
    reference: str
    pnr: str | None
    provider: str | None
    booking_status: str
    issuance_status: str
    hold_expires_at: datetime | None
    currency: str
    supplier_cost_minor: int | None
    supplier_ref: str | None


def claim_for_ticketing(reference: str, actor_user_id: str, check) -> TicketingTarget:
    """Lock the booking, run `check(target)` (raises to refuse), then mark issuance 'unknown'
    before the supplier call, so a second attempt can never send ticketing twice."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(
            text("SELECT id, reference, pnr, provider_id, booking_status, issuance_status, hold_expires_at, currency,"
                 " supplier_cost_minor, supplier_ref FROM bookings WHERE reference = :ref FOR UPDATE"),
            {"ref": reference},
        ).mappings().first()
        if row is None:
            raise LookupError(reference)
        target = TicketingTarget(row["id"], row["reference"], row["pnr"], row["provider_id"], row["booking_status"],
                                 row["issuance_status"], row["hold_expires_at"], row["currency"], row["supplier_cost_minor"],
                                 row["supplier_ref"])
        check(target)
        connection.execute(text("UPDATE bookings SET issuance_status = 'unknown', updated_at = now(), revision = revision + 1 WHERE id = :id"), {"id": target.id})
        _event(connection, target.id, actor_user_id, "ticketing_requested", {"pnr": target.pnr})
    return target


def record_ticketing(booking_id: str, actor_user_id: str, outcome: str, response: dict[str, Any], details: dict[str, Any]) -> None:
    """outcome: 'issued' or 'unknown'."""
    with agencies.application_engine().begin() as connection:
        if outcome == "issued":
            connection.execute(
                text("UPDATE bookings SET issuance_status = 'issued', booking_status = 'ticket_issued', updated_at = now(),"
                     " revision = revision + 1, supplier_response = COALESCE(supplier_response, '{}'::jsonb) || jsonb_build_object('ticketing', CAST(:r AS jsonb))"
                     " WHERE id = :id"),
                {"id": booking_id, "r": json.dumps(response)},
            )
        else:
            connection.execute(
                text("UPDATE bookings SET issuance_status = 'unknown', updated_at = now(), revision = revision + 1,"
                     " supplier_response = COALESCE(supplier_response, '{}'::jsonb) || jsonb_build_object('ticketing', CAST(:r AS jsonb))"
                     " WHERE id = :id"),
                {"id": booking_id, "r": json.dumps(response)},
            )
        _event(connection, booking_id, actor_user_id, f"ticketing_{outcome}", details)
