"""Flight pricing rules: Rs per passenger on one supplier's fares (optionally one SooperFare source and/or one airline),
the most specific rule alone, never below the supplier's cost, and the same amount at search and at reprice (so the
price the customer saw is the price they confirm). No database: the active rules are patched in."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.db.flight_pricing_rules import Rule, RuleRefused, clean
from app.services import flight_pricing_rules as pricing_rules
from app.services import live_flights

from tests.sooperfare.test_agency_visibility import run_reprice, run_search
from tests.southair.fixtures import search_data
from tests.southair.test_reprice import STAFF, fake_client, guide_fare_id, searched

NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)


def rule(supplier, amount_rupees, source=None, airline=None, active=True, rule_id=None):
    return Rule(rule_id or f"{supplier}-{source}-{airline}", supplier, source, airline, amount_rupees * 100, active, NOW)


@pytest.fixture
def rules(monkeypatch):
    """Set the active rules for a test (and clear the cache around it)."""
    current: list[Rule] = []
    pricing_rules.clear_cache()
    monkeypatch.setattr(pricing_rules, "_active_rules", lambda: current)
    yield current
    pricing_rules.clear_cache()


@pytest.fixture(autouse=True)
def markup():
    live_flights._sessions.clear()
    live_flights._quotes.clear()
    live_flights._account_currency = None
    # Rs 500 markup, so a Rs 100 rule has room above the cost (the floor has its own test).
    with patch.object(settings, "flight_markup_percent", Decimal("0")), patch.object(settings, "flight_markup_fixed_minor", 50_000):
        yield


def test_the_most_specific_rule_applies_alone():
    all_sf = [rule("sooperfare", -50), rule("sooperfare", -100, source="PK"), rule("sooperfare", -200, airline="PK"),
              rule("sooperfare", -300, source="PK", airline="PK"), rule("southair", 500)]
    match = lambda *args: pricing_rules.matching_rule(*args, rules=all_sf)  # noqa: E731
    assert match("sooperfare", "PK", "PK").amount_minor == -30_000      # source and airline
    assert match("sooperfare", "SABRE", "PK").amount_minor == -20_000   # airline beats source
    assert match("sooperfare", "PK", "EK").amount_minor == -10_000      # source
    assert match("sooperfare", "PA", "PA").amount_minor == -5_000       # the whole supplier
    assert match("southair", None, "Z8").amount_minor == 50_000
    assert match("kyte", None, "EK") is None
    assert pricing_rules.matching_rule("sooperfare", "PK", "PK", rules=[rule("sooperfare", -100, source="PK", active=False)]) is None


def test_rules_are_in_rupees_only(rules):
    rules.append(rule("kyte", -100))
    assert pricing_rules.adjustment("kyte", None, "EK", "PKR") == -10_000
    assert pricing_rules.adjustment("kyte", None, "EK", "EUR") == 0


def test_a_selling_price_never_goes_below_the_cost():
    assert live_flights.sell(45_770) == 95_800                 # 457.70 + Rs 500 -> 958
    assert live_flights.sell(45_770, -10_000) == 85_800        # Rs 100 off
    assert live_flights.sell(45_770, -60_000) == 45_800        # Rs 600 off would go below the cost: the cost, in whole rupees
    assert live_flights.sell(45_770, 50_000) == 145_800        # Rs 500 on


def test_unreadable_rules_mean_no_rule(monkeypatch):
    pricing_rules.clear_cache()

    def broken(active_only=False):
        raise RuntimeError("database down")

    monkeypatch.setattr(pricing_rules.store, "list_rules", broken)
    assert pricing_rules.adjustment("southair", None, "MM", "PKR") == 0
    pricing_rules.clear_cache()


def test_southair_rule_on_search_and_the_same_at_reprice(rules):
    rules.append(rule("southair", -100, airline="MM"))
    client = fake_client(search_data(), search_data(), fare_info={"FINAL_TOTAL": "915.40"})  # 2 adults x 457.70
    test, patcher, result = searched(STAFF, client)
    fare = next(f for f in result["journeys"][0]["flights"] if f["flight_number"] == "MM112")["fares"][0]
    assert fare["selling_price"]["amount_minor"] == 85_800  # 958 - 100
    assert fare["total_price"]["amount_minor"] == 2 * 85_800
    body = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]}).json()
    patcher.stop()
    assert body["selling_total"]["amount_minor"] == 2 * 85_800 and body["price_changed"] is False


@pytest.mark.parametrize("other", [rule("southair", -100, airline="PK"), rule("sooperfare", -100), rule("southair", -100, active=False)])
def test_a_rule_for_another_airline_or_supplier_or_switched_off_changes_nothing(rules, other):
    rules.append(other)
    test, patcher, result = searched(STAFF, fake_client(search_data()))
    patcher.stop()
    fare = next(f for f in result["journeys"][0]["flights"] if f["flight_number"] == "MM112")["fares"][0]
    assert fare["selling_price"]["amount_minor"] == 95_800


def _round_trip(rules_now):
    body = {"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27", "return_date": "2026-11-30", "adults": 1}
    client = fake_client(search_data(round_trip=True), search_data(round_trip=True), fare_info={"FINAL_TOTAL": "857.70"})
    test, patcher, result = searched(STAFF, client, body)
    out_fare = next(f for f in result["journeys"][0]["flights"] if f["flight_number"] == "MM112")["fares"][0]
    ret_fare = result["journeys"][1]["flights"][0]["fares"][0]
    shown = out_fare["selling_price"]["amount_minor"] + ret_fare["selling_price"]["amount_minor"]
    priced = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [out_fare["id"], ret_fare["id"]]}).json()
    patcher.stop()
    return shown, priced["selling_total"]["amount_minor"]


def test_round_trip_gets_the_rule_on_each_fare_at_search_and_at_reprice(rules):
    plain_shown, plain_priced = _round_trip(rules)
    rules.append(rule("southair", -100))
    shown, priced = _round_trip(rules)
    assert plain_shown - shown == 20_000 and plain_priced - priced == 20_000  # Rs 100 off each way, the same at reprice


def test_sooperfare_source_rule_on_search_and_at_reprice(monkeypatch, rules):
    _, plain = run_search(monkeypatch, "super_admin", provider="PA")
    plain_fare = next(f for f in plain.json()["journeys"][0]["flights"] if f["flight_number"] == "PA400")["fares"][0]
    rules.append(rule("sooperfare", -100, source="PA"))
    _, ruled = run_search(monkeypatch, "super_admin", provider="PA")
    fare = next(f for f in ruled.json()["journeys"][0]["flights"] if f["flight_number"] == "PA400")["fares"][0]
    assert fare["selling_price"]["amount_minor"] == plain_fare["selling_price"]["amount_minor"] - 10_000
    repriced = run_reprice(monkeypatch, "super_admin")
    assert repriced.status_code == 200, repriced.text
    rules.clear()
    plain_reprice = run_reprice(monkeypatch, "super_admin").json()
    assert repriced.json()["selling_total"]["amount_minor"] == plain_reprice["selling_total"]["amount_minor"] - 10_000


@pytest.mark.parametrize("args,message", [
    (("airblue", None, None, -10_000), "Choose a supplier."),
    (("southair", "PK", None, -10_000), "SooperFare's sources"),
    (("sooperfare", "XX", None, -10_000), "SooperFare's sources"),
    (("sooperfare", None, "P", -10_000), "Choose an airline"),
    (("southair", None, None, 0), "whole rupees"),
    (("southair", None, None, -10_050), "whole rupees"),
    (("southair", None, None, 10_000_100), "at most Rs 100,000"),
])
def test_invalid_rules_are_refused(args, message):
    with pytest.raises(RuleRefused) as refused:
        clean(*args)
    assert refused.value.status == 422 and message in refused.value.message


def test_rule_fields_are_normalised():
    assert clean(" SooperFare ", "pk", "pk", -10_000) == ("sooperfare", "PK", "PK", -10_000)
    assert clean("kyte", "", "", 50_000) == ("kyte", None, None, 50_000)
