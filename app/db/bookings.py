"""Gateway-owned bookings (table `bookings`, migration 0008). Read access for
the Admin Dashboard. Booking creation through the gateway is not built yet."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import agencies

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BookingRow:
    """Fields the dashboard needs. The customer's name only; never traveler identity or contact details."""

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
    #: Set on rows loaded by a one-time import (e.g. "legacy_test_csv"); None for real bookings.
    import_source: str | None = None
    agency_id: str | None = None
    agency_name: str | None = None
    updated_at: datetime | None = None
    #: The booking's customer (payer and contact), when one was saved (migration 0011).
    customer_name: str | None = None
    #: The flights as held (migration 0018); None for bookings held before it.
    itinerary: list[dict] | None = None
    #: Group bookings: the airline's final PNR, recorded by staff after payment.
    final_pnr: str | None = None


_SELECT = text(
    """SELECT b.reference, b.pnr, b.product, b.provider_id, b.booking_status, b.payment_status,
           b.issuance_status, b.created_at, b.hold_expires_at, b.currency, b.total_minor,
           b.supplier_cost_minor, b.import_source, b.agency_id, a.name AS agency_name, b.updated_at,
           NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), '') AS customer_name, b.itinerary,
           b.supplier_response ->> 'final_pnr' AS final_pnr
       FROM bookings b LEFT JOIN agencies a ON a.id = b.agency_id
       LEFT JOIN booking_customers c ON c.booking_id = b.id
       WHERE b.booking_status NOT IN ('hold_pending', 'hold_failed')"""
)


def _error_type(error: Exception) -> str:
    """Error type only (e.g. "ProgrammingError/UndefinedTable"); never messages or data."""
    original = getattr(error, "orig", None)
    return f"{type(error).__name__}/{type(original).__name__}" if original is not None else type(error).__name__


def list_dashboard_bookings() -> list[BookingRow]:
    try:
        # agencies.application_engine is resolved at call time (tests patch it).
        with agencies.application_engine().begin() as connection:
            rows = connection.execute(_SELECT).mappings().all()
    except HTTPException:
        logger.warning("Bookings read failed: database not configured")
        raise BookingsUnavailable() from None
    except SQLAlchemyError as error:
        logger.warning("Bookings read failed: %s", _error_type(error))
        raise BookingsUnavailable() from None
    return [
        BookingRow(
            booking_id=row["reference"], pnr=row["pnr"], product=row["product"], provider=row["provider_id"],
            status=row["booking_status"], payment_status=row["payment_status"], ticket_status=row["issuance_status"],
            created_at=row["created_at"], hold_expires_at=row["hold_expires_at"], currency=row["currency"],
            total_minor=row["total_minor"], supplier_cost_minor=row["supplier_cost_minor"],
            import_source=row["import_source"], agency_id=row["agency_id"],
            agency_name=row["agency_name"], updated_at=row["updated_at"], customer_name=row["customer_name"],
            itinerary=row["itinerary"] if isinstance(row["itinerary"], list) else None, final_pnr=row.get("final_pnr") or None,
        )
        for row in rows
    ]


class BookingsUnavailable(Exception):
    """The bookings database could not be read."""
