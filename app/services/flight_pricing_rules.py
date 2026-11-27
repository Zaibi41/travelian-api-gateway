"""Which flight pricing rule applies to a fare, and its amount (app.db.flight_pricing_rules, migration 0041).

A fare matches a rule when the rule's supplier is the fare's supplier, and its source (SooperFare only) and airline are
the fare's or "any". The most specific matching rule applies, alone: airline and source > airline > source > the whole
supplier. The amount is Rs per passenger per fare (a return trip priced as two fares, one each way, gets it on each).
Rules are in rupees: a fare in another currency gets none.

The active rules are kept in memory for CACHE_SECONDS (searches don't read the database each time); saving or deleting a
rule clears the copy at once. If the rules can't be read, no rule applies (the plain markup) until the next try.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

from app.db import flight_pricing_rules as store

logger = logging.getLogger(__name__)

CACHE_SECONDS = 30.0
_cache: tuple[float, list[store.Rule]] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def _active_rules() -> list[store.Rule]:
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return _cache[1]
    try:
        rules = [rule for rule in store.list_rules(active_only=True) if rule.active]
    except Exception as error:  # the database is unavailable: the plain markup, and try again later
        logger.warning("Flight pricing rules not read", extra={"error_type": type(error).__name__})
        rules = []
    _cache = (now + CACHE_SECONDS, rules)
    return rules


def matching_rule(supplier: str | None, source: str | None, airline_code: str | None,
                  rules: Iterable[store.Rule] | None = None) -> store.Rule | None:
    supplier = (supplier or "").lower()
    source = (source or "").upper() or None
    airline_code = (airline_code or "").upper() or None
    best, best_rank = None, -1
    for rule in _active_rules() if rules is None else rules:
        if not rule.active or rule.supplier != supplier:
            continue
        if rule.source is not None and rule.source != source:
            continue
        if rule.airline_code is not None and rule.airline_code != airline_code:
            continue
        rank = (2 if rule.airline_code else 0) + (1 if rule.source else 0)
        if rank > best_rank:
            best, best_rank = rule, rank
    return best


def adjustment(supplier: str | None, source: str | None, airline_code: str | None, currency: str | None) -> int:
    """The matching rule's amount in minor units per passenger (0 when none applies or the fare isn't in rupees)."""
    if (currency or "").upper() != "PKR":
        return 0
    rule = matching_rule(supplier, source, airline_code)
    return rule.amount_minor if rule else 0


def for_fare(ref, currency: str | None) -> int:
    """The adjustment for one fare of a search (app.services.live_flights.FareRef)."""
    source = ref.sf.provider_id if getattr(ref, "sf", None) else None
    airline = (ref.segment or {}).get("airline_code") if getattr(ref, "segment", None) else None
    return adjustment(ref.provider, source, airline, currency)


def for_fares(refs, currency: str | None) -> int:
    """The total adjustment per passenger for the fares booked together (one per direction)."""
    return sum(for_fare(ref, currency) for ref in refs)
