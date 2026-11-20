"""Profit per booking (travelian#70, owner's formula of 20 Nov 2026), integer paisa only:

    profit = selling price + fees charged to the customer - supplier's actual charge - payment fees - refunds paid

- Supplier's actual charge: the supplier_charges recorded for the booking (at issue, or by Finance). Before any is
  recorded, the booking's stored supplier cost, labelled "quoted". When neither is known (old bookings whose cost was
  filled in as the selling price, migration 0021 / 0028 cost_known = false) the profit is None: "Profit: not known".
- Payment fees: payment_fees rows. A card (PayFast) payment without a recorded fee is listed in `missing`, so the
  page says "Not recorded" instead of treating it as 0.
- Refunds: only refunds marked paid. A card fee charged to the customer is not refunded (travelian#74).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProfitInputs:
    selling_minor: int
    stored_cost_minor: int | None
    cost_known: bool
    fees_charged_minor: int = 0
    supplier_charges_minor: list[int] = field(default_factory=list)
    payment_fees_minor: list[int] = field(default_factory=list)
    refunds_paid_minor: list[int] = field(default_factory=list)
    #: Card (PayFast) payments that have no fee recorded yet.
    card_payments_without_fee: int = 0


@dataclass(frozen=True)
class Profit:
    selling_minor: int
    fees_charged_minor: int
    supplier_charge_minor: int | None
    #: "recorded" (supplier_charges), "quoted" (stored cost, nothing recorded yet) or "not_known".
    supplier_charge_basis: str
    payment_fees_minor: int
    refunds_minor: int
    profit_minor: int | None
    #: Parts not recorded yet (shown as "Not recorded", never 0): "payment_fees".
    missing: tuple[str, ...] = ()


def calculate(inputs: ProfitInputs) -> Profit:
    for name in ("selling_minor", "fees_charged_minor"):
        if not isinstance(getattr(inputs, name), int):
            raise TypeError(f"{name} must be integer paisa")
    if inputs.supplier_charges_minor:
        supplier, basis = sum(inputs.supplier_charges_minor), "recorded"
    elif inputs.cost_known and inputs.stored_cost_minor is not None:
        supplier, basis = inputs.stored_cost_minor, "quoted"
    else:
        supplier, basis = None, "not_known"
    fees = sum(inputs.payment_fees_minor)
    refunds = sum(inputs.refunds_paid_minor)
    profit = None if supplier is None else inputs.selling_minor + inputs.fees_charged_minor - supplier - fees - refunds
    missing = ("payment_fees",) if inputs.card_payments_without_fee else ()
    return Profit(inputs.selling_minor, inputs.fees_charged_minor, supplier, basis, fees, refunds, profit, missing)
