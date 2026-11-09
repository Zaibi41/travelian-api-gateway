"""Supplier holds in the `bookings` table, with audit entries in `booking_events`.

supplier_ref (e.g. Southair ticket_ref) and supplier_response never leave the gateway.
The customer and travelers of a hold are saved in the same transaction (migration 0011);
date of birth and document number are encrypted (app/core/pii.py). Audit details hold no
personal data.
"""

from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.core import pii
from app.db import agencies

UAT_SOURCE = "southair_uat"
_REF_ALPHABET = string.ascii_uppercase + string.digits


def new_reference() -> str:
    return "TRV-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


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

    def __repr__(self) -> str:  # never print identity data
        return f"TravelerRecord(type={self.passenger_type})"


def identity_context(booking_id: str, position: int, field: str) -> str:
    """Associated data binding an encrypted value to its booking, traveler and field."""
    return f"booking:{booking_id}:traveler:{position}:{field}"


def _save_people(connection, booking_id: str, customer: CustomerRecord, travelers: list[TravelerRecord], keys: pii.Keys) -> None:
    connection.execute(
        text("INSERT INTO booking_customers (booking_id, first_name, last_name, phone_code, phone, email)"
             " VALUES (:b, :first, :last, :code, :phone, :email)"),
        {"b": booking_id, "first": customer.first_name, "last": customer.last_name, "code": customer.phone_code,
         "phone": customer.phone, "email": customer.email},
    )
    for position, traveler in enumerate(travelers):
        dob = traveler.date_of_birth.isoformat()
        document = (traveler.document_number or "").strip() or None
        connection.execute(
            text("INSERT INTO booking_travelers (id, booking_id, position, passenger_type, title, first_name, last_name,"
                 " date_of_birth_encrypted, date_of_birth_masked, id_type, document_number_encrypted, document_number_masked,"
                 " document_number_hash, document_country, nationality, document_expiry, infant_of_position, key_id)"
                 " VALUES (:id, :b, :pos, :type, :title, :first, :last, :dob, :dob_masked, :id_type, :doc, :doc_masked, :doc_hash,"
                 " :country, :nationality, :expiry, :infant_of, :key_id)"),
            {"id": str(uuid4()), "b": booking_id, "pos": position, "type": traveler.passenger_type, "title": traveler.title,
             "first": traveler.first_name, "last": traveler.last_name,
             "dob": pii.encrypt(dob, identity_context(booking_id, position, "date_of_birth"), keys),
             "dob_masked": pii.mask_date_of_birth(dob), "id_type": traveler.id_type,
             "doc": pii.encrypt(document, identity_context(booking_id, position, "document_number"), keys) if document else None,
             "doc_masked": pii.mask_document(document) if document else None,
             "doc_hash": pii.document_hash(document, keys) if document else None,
             "country": traveler.document_country, "nationality": traveler.nationality, "expiry": traveler.document_expiry,
             "infant_of": traveler.infant_of_position, "key_id": keys.active_id},
        )


def save_hold(*, provider: str, pnr: str, supplier_ref: str, hold_expires_at: datetime | None, currency: str,
              selling_total_minor: int, supplier_total_minor: int, supplier_response: dict[str, Any],
              actor_user_id: str, audit: dict[str, Any],
              customer: CustomerRecord | None = None, travelers: list[TravelerRecord] | None = None) -> str:
    """Insert a held, unpaid, not-issued UAT booking, its customer and travelers, and the audit
    entry, in one transaction. Returns the reference. Raises pii.PiiNotConfigured before writing
    anything if travelers are given and the encryption keys aren't set."""
    keys = pii.load_keys() if customer is not None or travelers else None
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
        if customer is not None and keys is not None:
            _save_people(connection, booking_id, customer, travelers or [], keys)
        _event(connection, booking_id, actor_user_id, "hold_created",
               {**audit, "label": "UAT test data", "travelers": len(travelers or [])})
    return reference
