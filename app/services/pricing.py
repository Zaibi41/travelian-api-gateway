"""Supplier cost, selling price and profit: one model for Group Ticketing and Flights.

- Cost: what the supplier (or airline) charges. Selling: what the customer pays. Profit = selling - cost.
- Kept per traveler (booking_travelers.cost_minor / selling_minor) and as totals (bookings.supplier_cost_minor /
  total_minor). Payments, Due and the payment summary use the selling total.
- The default selling price is the cost plus the product's existing markup rule (group: GROUP_TICKETING_MARKUP_*,
  flights: FLIGHT_MARKUP_*); this module doesn't change those rules.
- A selling price is in whole rupees and never below the cost. Super Admin and staff with "record payments" may set
  it before any payment (app.db.booking_pricing). Cost and profit only reach viewers with cost and margin access.
"""

from __future__ import annotations

from dataclasses import dataclass

#: One rupee in minor units (paisa).
RUPEE = 100


class PriceRefused(ValueError):
    def __init__(self, message: str, code: str = "invalid_selling_price"):
        super().__init__(message)
        self.message = message
        self.code = code


def check_selling(selling_each_minor: int, cost_each_minor: int | None) -> None:
    """Whole rupees, more than zero, and not below the cost. Raises PriceRefused."""
    if selling_each_minor <= 0 or selling_each_minor % RUPEE:
        raise PriceRefused("Enter the selling price in whole rupees.")
    if cost_each_minor is not None and selling_each_minor < cost_each_minor:
        raise PriceRefused("The selling price can't be below the supplier cost.", "below_cost")


def split(total_minor: int, count: int) -> list[int]:
    """A total shared over travelers: equal parts, any remainder on the first."""
    if count <= 0:
        return []
    base, rest = divmod(total_minor, count)
    return [base + rest] + [base] * (count - 1)


def scale(parts: list[int | None], total_minor: int) -> list[int]:
    """A new total shared in the same proportions as `parts` (e.g. per-passenger-type costs), summing exactly to it:
    each part rounded down, the leftover paisa to the largest remainders (first on ties). Equal split without parts."""
    known = [p or 0 for p in parts]
    whole = sum(known)
    if not parts or whole <= 0:
        return split(total_minor, len(parts))
    shares = [divmod(p * total_minor, whole) for p in known]
    result = [q for q, _ in shares]
    for index in sorted(range(len(parts)), key=lambda i: -shares[i][1])[: total_minor - sum(result)]:
        result[index] += 1
    return result


def cost_unknown(import_source: str | None, cost_minor: int | None, total_minor: int | None) -> bool:
    """No real supplier cost: none recorded, or an imported booking whose cost was filled in as its total (migration
    0021). Profit is then "n/a", never 0."""
    return cost_minor is None or (import_source is not None and cost_minor == total_minor)


@dataclass(frozen=True)
class Amounts:
    selling_total_minor: int
    cost_total_minor: int | None

    @property
    def profit_minor(self) -> int | None:
        return None if self.cost_total_minor is None else self.selling_total_minor - self.cost_total_minor

    @property
    def below_cost(self) -> bool:
        return self.cost_total_minor is not None and self.selling_total_minor < self.cost_total_minor
