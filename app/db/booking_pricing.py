"""Setting a booking's selling price (Group Ticketing and Flights, one implementation; see app.services.pricing).

The selling price is set per passenger type (ADT, CHD, INF) for every traveler of that type, in whole rupees and not
below that traveler's supplier cost. Only before any payment, while the booking is held or awaiting payment and not
issued. The booking total (what payments are capped at) becomes the sum of the travelers' selling prices. Logged.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event
from app.services.pricing import Amounts, PriceRefused, check_selling

OPEN = ("held", "pending_payment")


class SellingRefused(Exception):
    def __init__(self, status: int, message: str, code: str, field: str | None = None):
        super().__init__(message)
        self.status, self.message, self.code, self.field = status, message, code, field


@dataclass(frozen=True)
class SellingSet:
    reference: str
    amounts: Amounts
    currency: str


def set_selling_price(reference: str, prices: dict[str, int], actor_user_id: str, currency: str | None = None) -> SellingSet:
    """prices: passenger type -> selling price per traveler in minor units, in `currency` (the booking's when None)."""
    with agencies.application_engine().begin() as connection:
        booking = connection.execute(
            text("SELECT id, booking_status, issuance_status, currency, total_minor, supplier_cost_minor FROM bookings"
                 " WHERE reference = :r FOR UPDATE"), {"r": reference},
        ).mappings().one_or_none()
        if booking is None:
            raise SellingRefused(404, "Booking not found.", "not_found")
        if currency is not None and currency.upper() != booking["currency"]:
            raise SellingRefused(422, f"The selling price must be in {booking['currency']}.", "invalid_selling_price", "currency")
        if booking["booking_status"] not in OPEN or booking["issuance_status"] != "not_issued":
            raise SellingRefused(409, "The selling price can only change while the booking is on hold and not issued.", "not_open")
        paid = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments WHERE booking_id = :b"), {"b": booking["id"]})
        if paid:
            raise SellingRefused(409, "A payment is already recorded, so the selling price can't change.", "payment_recorded")
        travelers = connection.execute(
            text("SELECT id, passenger_type, selling_minor, cost_minor FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
            {"b": booking["id"]},
        ).mappings().all()
        types = {t["passenger_type"] for t in travelers}
        if not prices or not set(prices) <= types:
            raise SellingRefused(422, "Give a selling price for the passenger types on this booking.", "invalid_selling_price", "prices")
        for passenger_type, selling in prices.items():
            try:
                check_selling(selling, max(t["cost_minor"] for t in travelers if t["passenger_type"] == passenger_type))
            except PriceRefused as error:
                raise SellingRefused(422, error.message, error.code, passenger_type) from None
        for traveler in travelers:
            if traveler["passenger_type"] in prices:
                connection.execute(text("UPDATE booking_travelers SET selling_minor = :s WHERE id = :t"),
                                   {"s": prices[traveler["passenger_type"]], "t": traveler["id"]})
        total = sum(prices.get(t["passenger_type"], t["selling_minor"]) for t in travelers)
        connection.execute(text("UPDATE bookings SET total_minor = :t, payment_status = 'unpaid', updated_at = now(), revision = revision + 1"
                                " WHERE id = :b"), {"t": total, "b": booking["id"]})
        _event(connection, booking["id"], actor_user_id, "selling_price_set",
               {"amount_minor": total, "previous_amount_minor": booking["total_minor"], "currency": booking["currency"]})
        return SellingSet(reference, Amounts(total, booking["supplier_cost_minor"]), booking["currency"])
