"""Flight pricing rules (migration 0041): Rs per passenger added to or taken off one supplier's fares. Super Admin only
(app.api.routes.flight_pricing). Saving or deleting a rule clears the search's cached copy at once."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import agencies

SUPPLIERS = ("southair", "sooperfare", "kyte")
#: SooperFare's own sources (its provider identifiers) and what each one is.
SOOPERFARE_SOURCES = {"PK": "PIA", "PA": "Airblue", "PF": "AirSial", "ONEAPI": "OneAPI (Fly Jinnah)", "Z8": "South Air", "SABRE": "Sabre (GDS)"}
#: At most Rs 100,000 either way.
MAX_AMOUNT_MINOR = 10_000_000


class RuleRefused(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_rule"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Rule:
    id: str
    supplier: str
    source: str | None
    airline_code: str | None
    amount_minor: int
    active: bool
    updated_at: datetime


_SELECT = "SELECT id, supplier, source, airline_code, amount_minor, active, updated_at FROM flight_pricing_rules"


def _rule(row) -> Rule:
    return Rule(row["id"], row["supplier"], row["source"], row["airline_code"], row["amount_minor"], row["active"], row["updated_at"])


def clean(supplier: str, source: str | None, airline_code: str | None, amount_minor: int) -> tuple[str, str | None, str | None, int]:
    """The rule's fields checked and normalised. Raises RuleRefused (422)."""
    supplier = (supplier or "").strip().lower()
    if supplier not in SUPPLIERS:
        raise RuleRefused(422, "Choose a supplier.")
    source = (source or "").strip().upper() or None
    if source is not None and (supplier != "sooperfare" or source not in SOOPERFARE_SOURCES):
        raise RuleRefused(422, "Choose one of SooperFare's sources, or any source.")
    airline_code = (airline_code or "").strip().upper() or None
    if airline_code is not None and not (2 <= len(airline_code) <= 3 and airline_code.isalnum() and any(c.isalpha() for c in airline_code)):
        raise RuleRefused(422, "Choose an airline, or any airline.")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor == 0 or amount_minor % 100:
        raise RuleRefused(422, "Enter the amount in whole rupees, for example -100 or 500.")
    if abs(amount_minor) > MAX_AMOUNT_MINOR:
        raise RuleRefused(422, "The amount can be at most Rs 100,000 either way.")
    return supplier, source, airline_code, amount_minor


def list_rules(active_only: bool = False) -> list[Rule]:
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(_SELECT + (" WHERE active" if active_only else "")
                                       + " ORDER BY supplier, source NULLS FIRST, airline_code NULLS FIRST")).mappings().all()
    return [_rule(row) for row in rows]


def _duplicate() -> RuleRefused:
    return RuleRefused(409, "There is already a rule for this supplier, source and airline. Edit that rule instead.", "duplicate_rule")


def create_rule(supplier: str, source: str | None, airline_code: str | None, amount_minor: int, active: bool, actor_user_id: str) -> Rule:
    supplier, source, airline_code, amount_minor = clean(supplier, source, airline_code, amount_minor)
    rule_id = str(uuid4())
    try:
        with agencies.application_engine().begin() as connection:
            connection.execute(text("INSERT INTO flight_pricing_rules (id, supplier, source, airline_code, amount_minor, active,"
                                    " created_by_user_id, updated_by_user_id) VALUES (:id, :s, :src, :a, :m, :act, :u, :u)"),
                               {"id": rule_id, "s": supplier, "src": source, "a": airline_code, "m": amount_minor, "act": active, "u": actor_user_id})
            row = connection.execute(text(_SELECT + " WHERE id = :id"), {"id": rule_id}).mappings().one()
    except IntegrityError:
        raise _duplicate() from None
    return _rule(row)


def update_rule(rule_id: str, supplier: str, source: str | None, airline_code: str | None, amount_minor: int, active: bool,
                actor_user_id: str) -> Rule:
    supplier, source, airline_code, amount_minor = clean(supplier, source, airline_code, amount_minor)
    try:
        with agencies.application_engine().begin() as connection:
            updated = connection.execute(text("UPDATE flight_pricing_rules SET supplier = :s, source = :src, airline_code = :a,"
                                              " amount_minor = :m, active = :act, updated_by_user_id = :u, updated_at = clock_timestamp()"
                                              " WHERE id = :id"),
                                         {"id": rule_id, "s": supplier, "src": source, "a": airline_code, "m": amount_minor, "act": active,
                                          "u": actor_user_id}).rowcount
            if not updated:
                raise RuleRefused(404, "Rule not found.", "not_found")
            row = connection.execute(text(_SELECT + " WHERE id = :id"), {"id": rule_id}).mappings().one()
    except IntegrityError:
        raise _duplicate() from None
    return _rule(row)


def delete_rule(rule_id: str) -> None:
    with agencies.application_engine().begin() as connection:
        if not connection.execute(text("DELETE FROM flight_pricing_rules WHERE id = :id"), {"id": rule_id}).rowcount:
            raise RuleRefused(404, "Rule not found.", "not_found")
