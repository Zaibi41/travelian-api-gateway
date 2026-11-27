"""Pricing & Revenue > Flight Pricing & Rules (Super Admin only): the flight suppliers and the pricing rules on them."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Supplier = Literal["southair", "sooperfare", "kyte"]


class SupplierSource(BaseModel):
    code: str = Field(description="SooperFare's own source identifier, e.g. PK, PA, ONEAPI.")
    name: str


class FlightSupplier(BaseModel):
    code: Supplier
    name: str
    status: Literal["live", "not_public", "not_configured"] = Field(
        description="live: its fares are searched for customers; not_public: searched for staff only (Kyte until "
                    "KYTE_PUBLIC_ENABLED); not_configured: no credentials on this server, so its rules apply to nothing yet.")
    sources: list[SupplierSource] = Field([], description="SooperFare only: the sources a rule can be limited to.")


class FlightSupplierList(BaseModel):
    suppliers: list[FlightSupplier]


class FlightPricingRuleInput(BaseModel):
    supplier: Supplier
    source: str | None = Field(None, max_length=20, description="SooperFare only; empty = any source.")
    airline_code: str | None = Field(None, max_length=3, description="IATA code; empty = any airline.")
    amount_minor: int = Field(description="Rs per passenger, in paisa (whole rupees): negative takes it off the selling "
                                          "price, positive adds it. Never below the supplier's cost.")
    active: bool = True


class FlightPricingRule(FlightPricingRuleInput):
    id: str
    updated_at: datetime


class FlightPricingRuleList(BaseModel):
    rules: list[FlightPricingRule]


class DeletedFlightPricingRule(BaseModel):
    id: str
    deleted: bool = True
