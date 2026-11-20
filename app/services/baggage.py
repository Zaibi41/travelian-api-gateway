"""Baggage, one way everywhere (#36): the API, the itinerary PDF and the emails all format through here, and the
website's src/lib/baggage.ts follows the same rules (tests/baggage_cases.json is the shared table of cases).

Short form, hand carry first:  "7 kg" (hand carry only) · "7+20 kg" · "7+1×23 kg" · "7 kg + 1 pc"
Full words (tooltips, PDF, email): "7 kg hand carry + 20 kg checked" · "7 kg hand carry only, no checked bag"
Unknown: "Baggage: check with airline". Never a raw supplier code. A hand carry is never assumed, except Southair's
7 kg (SOUTHAIR_HAND_CARRY_KG): other suppliers without a hand-carry value show "20 kg checked".

Each supplier's raw data is read by its own function (Southair text, SooperFare baggage_policies, group checked and hand
carry). ``parse_text`` also reads what earlier versions stored on bookings ("20 kg checked, 7 kg hand carry", "7+20 kg").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

UNKNOWN = "Baggage: check with airline"
_MAX_KG = 100  # anything heavier is not a baggage allowance we can trust


@dataclass(frozen=True)
class Baggage:
    """hand_kg: hand carry. Checked baggage is either a total weight (checked_kg) or pieces (pieces, optionally
    piece_kg each). 0 checked kg or 0 pieces means no checked bag; None means not known."""
    hand_kg: int | None = None
    checked_kg: int | None = None
    pieces: int | None = None
    piece_kg: int | None = None

    @property
    def known(self) -> bool:
        return any(value is not None for value in (self.hand_kg, self.checked_kg, self.pieces))

    @property
    def no_checked(self) -> bool:
        return self.checked_kg == 0 or self.pieces == 0

    @property
    def _has_pieces(self) -> bool:
        return bool(self.pieces) and not self.checked_kg

    def short(self) -> str:
        if not self.known:
            return UNKNOWN
        hand, checked = self.hand_kg or None, self.checked_kg
        if self._has_pieces:
            piece = f"{self.pieces}×{self.piece_kg} kg" if self.piece_kg else f"{self.pieces} pc"
            if hand is None:
                return f"{piece} checked"
            return f"{hand}+{piece}" if self.piece_kg else f"{hand} kg + {piece}"
        if checked:
            return f"{hand}+{checked} kg" if hand is not None else f"{checked} kg checked"
        # No checked bag ("7 kg"), or checked not known ("7 kg hand carry": never read as "no checked bag").
        if hand is not None:
            return f"{hand} kg" if self.no_checked else f"{hand} kg hand carry"
        return "No checked bag" if self.no_checked else UNKNOWN

    def detail(self) -> str:
        if not self.known:
            return UNKNOWN
        hand = f"{self.hand_kg} kg hand carry" if self.hand_kg else None
        if self._has_pieces:
            noun = "piece" if self.pieces == 1 else "pieces"
            checked = f"{self.pieces} checked {noun}" + (f" of up to {self.piece_kg} kg each" if self.piece_kg and self.pieces > 1
                                                          else f" of up to {self.piece_kg} kg" if self.piece_kg else "")
        elif self.checked_kg:
            checked = f"{self.checked_kg} kg checked"
        else:
            checked = None
        if hand and checked:
            return f"{hand} + {checked}"
        if hand:
            return f"{hand} only, no checked bag" if self.no_checked else f"{hand}; checked baggage: check with airline"
        if checked:
            return f"{checked}; hand carry: check with airline"
        return "No checked bag; hand carry: check with airline" if self.no_checked else UNKNOWN


def _kg(value: Any) -> int | None:
    """A weight in kg from a supplier value: 20, "20", "20.0", "20KG", "07". None when it isn't one."""
    if value is None or isinstance(value, bool):
        return None
    text = re.sub(r"\s*(kgs?|kilos?|kilograms?)\s*$", "", str(value).strip(), flags=re.I)
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number > _MAX_KG or number != number.to_integral_value():
        return None
    return int(number)


# ---------- free text: Southair, and what earlier versions stored on bookings ----------

_NONE = re.compile(r"^(nil+|no(ne)?|0\s*(kgs?|pcs?|pieces?)?|not included)(\s+(checked|check-?in))?(\s+(baggage|bags?))?(\s+(included|allowed))?$", re.I)
_NO_CHECKED = re.compile(r"\b(no checked (baggage|bag)|checked baggage not included|nil+ baggage|no baggage)\b|^nil+\b|\b(cabin|hand)[\s-]*(bag|baggage|carry) only\b", re.I)
_PIECES = re.compile(r"(\d+)\s*(?:checked\s+)?(?:pcs?|pieces?)\b(?:\s*(?:x|×|of(?: up to)?|@)?\s*(\d+)\s*kgs?)?", re.I)
_TIMES = re.compile(r"(\d+)\s*[x×]\s*(\d+)\s*kgs?", re.I)
_HAND = re.compile(r"(\d+)\s*kgs?\s*(?:hand[\s-]*carry|hand[\s-]*bag(?:gage)?|cabin(?:\s+bag(?:gage)?)?|carry[\s-]*on)", re.I)
_CHECKED = re.compile(r"(\d+)\s*kgs?\s*(?:checked|check-?in)", re.I)
_PLUS = re.compile(r"^(\d+)\s*\+\s*(\d+)\s*kgs?$", re.I)
_PLUS_PIECES = re.compile(r"^(\d+)\s*\+\s*(\d+)\s*[x×]\s*(\d+)\s*kgs?$", re.I)
_HAND_PLUS_PC = re.compile(r"^(\d+)\s*kgs?\s*\+\s*(\d+)\s*pcs?$", re.I)
_WEIGHT = re.compile(r"^(\d+)\s*kgs?$", re.I)


def parse_text(text: str | None) -> Baggage:
    """Baggage from text. Reads Southair's checked-baggage text ("20KG", "Nill Baggage", "1PC", "2PC 23KG") and the
    texts earlier versions stored ("20 kg checked, 7 kg hand carry", "7+20 kg", "7 kg"). Anything else is unknown."""
    value = re.sub(r"\s+", " ", str(text or "")).strip().strip(".")
    if not value or value.lower() in {"null", "undefined", "n/a", "na", "-", UNKNOWN.lower(), "baggage info not available"}:
        return Baggage()
    if match := _PLUS_PIECES.match(value):  # our own short form "7+1×23 kg"
        return Baggage(hand_kg=_kg(match[1]), pieces=int(match[2]), piece_kg=_kg(match[3]))
    if match := _HAND_PLUS_PC.match(value):  # "7 kg + 1 pc"
        return Baggage(hand_kg=_kg(match[1]), pieces=int(match[2]))
    if match := _PLUS.match(value):  # our own short form "7+20 kg": hand carry first
        return Baggage(hand_kg=_kg(match[1]), checked_kg=_kg(match[2]))
    hand = _HAND.search(value)
    hand_kg = _kg(hand[1]) if hand else None
    if _NONE.match(value) or _NO_CHECKED.search(value):
        return Baggage(hand_kg=hand_kg, checked_kg=0)
    if match := _TIMES.search(value):
        return Baggage(hand_kg=hand_kg, pieces=int(match[1]), piece_kg=_kg(match[2]))
    if match := _PIECES.search(value):
        return Baggage(hand_kg=hand_kg, pieces=int(match[1]), piece_kg=_kg(match[2]) if match[2] else None)
    if match := _CHECKED.search(value):
        return Baggage(hand_kg=hand_kg, checked_kg=_kg(match[1]))
    if hand_kg is not None:
        return Baggage(hand_kg=hand_kg)
    if match := _WEIGHT.match(value):  # a bare weight from a supplier is the checked allowance ("20KG")
        return Baggage(checked_kg=_kg(match[1]))
    return Baggage()


#: Southair sends no hand-carry value; its fares include 7 kg hand carry (owner, 20 Nov 2026: "No baggage means 7 kg,
#: 20 kg baggage means 7+20"). A hand-carry value on the fare, when sent, wins.
SOUTHAIR_HAND_CARRY_KG = 7


def from_southair(checked_text: str | None, hand_carry_text: str | None = None) -> Baggage:
    """Southair sends only the checked allowance as text (HIGHLIGHT checkin_baggage): "20KG" -> 7+20 kg, "Nill Baggage"
    -> 7 kg. A hand-carry feature is read when the fare has one. Checked text we can't read stays unknown."""
    checked = parse_text(checked_text)
    if not checked.known:
        return Baggage()
    hand = parse_text(f"{hand_carry_text} hand carry") if hand_carry_text and _WEIGHT.match(hand_carry_text.strip()) \
        else parse_text(hand_carry_text) if hand_carry_text else Baggage()
    hand_kg = next((kg for kg in (checked.hand_kg, hand.hand_kg, hand.checked_kg) if kg), SOUTHAIR_HAND_CARRY_KG)
    return Baggage(hand_kg=hand_kg, checked_kg=checked.checked_kg, pieces=checked.pieces, piece_kg=checked.piece_kg)


def from_sooperfare(policies: list[dict] | None) -> Baggage:
    """SooperFare baggage_policies: one 'carry' and one 'checked' row per traveller type, weight_limit in kg and pieces.
    A checked weight_limit is the total for all pieces ("40 kg checked baggage (up to 2 bags)")."""
    rows = [row for row in policies or [] if isinstance(row, dict) and row.get("traveler_type") in (None, "adult")]
    carry = next((row for row in rows if row.get("type") == "carry"), None)
    checked = next((row for row in rows if row.get("type") == "checked"), None)

    def weight(row: dict | None) -> int | None:
        if row is None or str(row.get("weight_unit") or "kg").lower() not in ("kg", "kgs"):
            return None
        return _kg(row.get("weight_limit"))

    hand_kg = weight(carry)
    if checked is None:
        return Baggage(hand_kg=hand_kg)
    checked_kg, pieces = weight(checked), checked.get("pieces")
    pieces = pieces if isinstance(pieces, int) and not isinstance(pieces, bool) and 0 <= pieces <= 10 else None
    if checked_kg:
        return Baggage(hand_kg=hand_kg, checked_kg=checked_kg)
    if checked_kg == 0:
        return Baggage(hand_kg=hand_kg, checked_kg=0)
    return Baggage(hand_kg=hand_kg, pieces=pieces)


def from_group(checked: Any, hand_carry: Any) -> Baggage:
    """Group fares: checked and hand carry in kg as the supplier sends them ("20", 20, "20 KG", "1PC"; hand "7", "07").
    The combined "20+7" strings are split by the vendor first (skypass.inventory.split_baggage)."""
    hand_kg = _kg(hand_carry)
    checked_kg = _kg(checked)
    if checked_kg is None and checked not in (None, ""):
        parsed = parse_text(str(checked))
        return Baggage(hand_kg=hand_kg if hand_kg else parsed.hand_kg, checked_kg=parsed.checked_kg, pieces=parsed.pieces,
                       piece_kg=parsed.piece_kg)
    return Baggage(hand_kg=hand_kg or None, checked_kg=checked_kg)


def to_dict(baggage: Baggage) -> dict[str, int]:
    """What a booking stores (itinerary "baggage_parts"), so it is shown later without parsing text again."""
    return {key: value for key, value in baggage.__dict__.items() if value is not None}


def from_dict(parts: Any) -> Baggage | None:
    if not isinstance(parts, dict):
        return None
    values = {key: parts.get(key) for key in ("hand_kg", "checked_kg", "pieces", "piece_kg")}
    if any(value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_KG) for value in values.values()):
        return None
    return Baggage(**values)


def of_segment(segment: dict | None) -> Baggage:
    """A stored itinerary segment's baggage: its parts when saved, else its text (older bookings)."""
    segment = segment if isinstance(segment, dict) else {}
    return from_dict(segment.get("baggage_parts")) or parse_text(segment.get("baggage"))


def short(text: str | None) -> str:
    """Stored or supplier text in the short form."""
    return parse_text(text).short()


def detail(text: str | None) -> str:
    """Stored or supplier text in full words."""
    return parse_text(text).detail()
