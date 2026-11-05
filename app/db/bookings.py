"""Gateway-owned bookings (table `bookings`, migration 0008). Read access for
the Admin Dashboard. Booking creation through the gateway is not built yet."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import agencies


@dataclass(frozen=True)
class BookingRow:
    """Fields the dashboard needs. Never includes traveler or contact data."""

    booking_id: str
    pnr: str | None
    product: str
    provider: str | None
    status: str
    payment_status: str | None
    ticket_status: str | None
    created_at: datetime | None
    hold_expires_at: datetime | None
    currency: str | None
    total_minor: int | None
    supplier_cost_minor: int | None


_SELECT = text(
    """SELECT reference, pnr, product, provider_id, booking_status, payment_status,
           issuance_status, created_at, hold_expires_at, currency, total_minor,
           supplier_cost_minor
       FROM bookings"""
)


def list_dashboard_bookings() -> list[BookingRow]:
    try:
        # agencies.application_engine is resolved at call time (tests patch it).
        with agencies.application_engine().begin() as connection:
            rows = connection.execute(_SELECT).mappings().all()
    except (SQLAlchemyError, HTTPException):
        raise BookingsUnavailable() from None
    return [
        BookingRow(
            booking_id=row["reference"], pnr=row["pnr"], product=row["product"], provider=row["provider_id"],
            status=row["booking_status"], payment_status=row["payment_status"], ticket_status=row["issuance_status"],
            created_at=row["created_at"], hold_expires_at=row["hold_expires_at"], currency=row["currency"],
            total_minor=row["total_minor"], supplier_cost_minor=row["supplier_cost_minor"],
        )
        for row in rows
    ]


class BookingsUnavailable(Exception):
    """The bookings database could not be read."""
