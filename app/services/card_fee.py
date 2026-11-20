"""Card payment fee (travelian#74), calculated only here, in integer paisa.

Owner's defaults (20 Nov 2026) until changed in Finance settings:
- rate 3% (300 basis points), charged on card and 1Link payments (both go through PayFast checkout);
- group fares included;
- the fee is not refunded (refunds are capped at the fare money received, app.db.finance.refundable_minor).
Rounding: to the nearest rupee, halves up. Bank transfer and JazzCash: no fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RUPEE = 100
DEFAULTS: dict[str, Any] = {"card_fee_bps": 300, "card_fee_on_group": True}
MAX_BPS = 1000  # 10%: a guard against a typo, not a policy


@dataclass(frozen=True)
class CardFee:
    bps: int
    fee_minor: int
    total_minor: int

    @property
    def percent_label(self) -> str:
        whole, rest = divmod(self.bps, 100)
        return f"{whole}%" if not rest else f"{whole}.{rest:02d}".rstrip("0") + "%"


def fee_minor(amount_minor: int, bps: int) -> int:
    """amount * bps / 10000, rounded to the nearest whole rupee (halves up). Integers only."""
    if not isinstance(amount_minor, int) or not isinstance(bps, int):
        raise TypeError("card fee needs integer paisa and integer basis points")
    if amount_minor <= 0 or bps <= 0:
        return 0
    denominator = 10_000 * RUPEE
    return (amount_minor * bps + denominator // 2) // denominator * RUPEE


def settings_from(stored: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    return merged


def quote(amount_minor: int, product: str, stored: dict[str, Any]) -> CardFee:
    config = settings_from(stored)
    bps = int(config["card_fee_bps"])
    if product == "group" and not config["card_fee_on_group"]:
        bps = 0
    fee = fee_minor(amount_minor, bps)
    return CardFee(bps=bps if fee else 0, fee_minor=fee, total_minor=amount_minor + fee)


def validate(key: str, value: Any) -> Any:
    if key == "card_fee_bps":
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_BPS:
            raise ValueError("Enter the card fee as 0 to 10 percent.")
        return value
    if key == "card_fee_on_group":
        if not isinstance(value, bool):
            raise ValueError("Choose yes or no.")
        return value
    raise ValueError("Unknown setting.")
