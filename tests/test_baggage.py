"""#36: one baggage format for every supplier (tests/baggage_cases.json is shared with the website's tests)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import baggage
from app.services.baggage import Baggage

CASES = json.loads((Path(__file__).parent / "baggage_cases.json").read_text())["cases"]


def _read(case: dict) -> Baggage:
    given = case["input"]
    if case["source"] == "parts":
        return Baggage(**given)
    if case["source"] == "southair":
        return baggage.from_southair(given.get("checked"), given.get("hand_carry"))
    if case["source"] == "sooperfare":
        return baggage.from_sooperfare(given["policies"])
    if case["source"] == "group":
        return baggage.from_group(given["checked"], given["hand_carry"])
    return baggage.parse_text(given["text"])


@pytest.mark.parametrize("case", CASES, ids=[f"{c['source']}:{json.dumps(c['input'], ensure_ascii=False)[:60]}" for c in CASES])
def test_shared_cases(case):
    found = _read(case)
    assert (found.short(), found.detail()) == (case["short"], case["detail"])


@pytest.mark.parametrize("raw", ["SUBJECT TO SUPPLIER RULES", "See supplier details", "BAG-CODE-XJ", "null", "999KG", "-5 kg"])
def test_never_shows_raw_supplier_text(raw):
    assert baggage.short(raw) == baggage.UNKNOWN


def test_stored_parts_win_over_text_and_survive_a_round_trip():
    parts = baggage.to_dict(Baggage(hand_kg=7, pieces=1, piece_kg=23))
    assert baggage.of_segment({"baggage": "garbage", "baggage_parts": parts}).short() == "7+1×23 kg"
    assert baggage.of_segment({"baggage": "20 kg checked, 7 kg hand carry"}).short() == "7+20 kg"  # older bookings
    assert baggage.of_segment({"baggage_parts": {"hand_kg": "7"}}).short() == baggage.UNKNOWN  # not trusted: falls back to text
    assert baggage.of_segment(None).short() == baggage.UNKNOWN


def test_short_forms_read_back_the_same():
    for given in (Baggage(hand_kg=7, checked_kg=20), Baggage(hand_kg=7, pieces=1, piece_kg=23), Baggage(hand_kg=7, pieces=2)):
        assert baggage.parse_text(given.short()) == given


def test_customer_email_shows_baggage_in_full_words():
    from app.services import email_templates as t
    from app.services.customer_emails import baggage_of
    same = [{"origin": "ISB", "destination": "JED", "baggage": "10+20 kg"}, {"origin": "JED", "destination": "ISB", "baggage": "10+20 kg"}]
    assert baggage_of(same) == "10 kg hand carry + 20 kg checked"
    mixed = [same[0], {"origin": "JED", "destination": "ISB", "baggage_parts": {"hand_kg": 7, "checked_kg": 0}}]
    assert baggage_of(mixed) == "ISB–JED: 10 kg hand carry + 20 kg checked; JED–ISB: 7 kg hand carry only, no checked bag"
    assert baggage_of([]) is None
    email = t.booking_held(t.Booking(reference="TRV-1", passengers=["Ali"], amount_minor=100, baggage=baggage_of(same),
                                     hold_expires_at=datetime(2026, 12, 1, tzinfo=timezone.utc)))
    assert "Baggage" in email.html and "10 kg hand carry + 20 kg checked" in email.html
    assert "Baggage: 10 kg hand carry + 20 kg checked" in email.text


@pytest.mark.parametrize("case", [c for c in CASES if c["short"] != baggage.UNKNOWN], ids=lambda c: c["short"])
def test_full_words_read_back_the_same(case):
    assert baggage.parse_text(case["detail"]).short() == case["short"]
