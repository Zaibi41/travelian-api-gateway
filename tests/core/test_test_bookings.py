"""Test bookings are never issued: a passenger named TEST, or a booking marked as a test (owner, 20 Nov 2026)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.db import issuance as store


def held(test_booking=False):
    return store.Target("b1", "TRV-1", "IKVIMS", "sooperfare", "flight", "held", "not_issued",
                        datetime.now(timezone.utc) + timedelta(hours=20), "PKR", 2207000, 2207000, "uuid-1", 2207000,
                        [(0, "ADT")], test_booking)


@pytest.mark.parametrize("name", ["TEST", "test", "Test Passenger", "Ali TEST", "TEST1", "Mr-Test"])
def test_names_with_the_word_test(name):
    assert store.is_test_name(name)


@pytest.mark.parametrize("name", [None, "", "Asim", "Celeste", "Testa", "Contester", "Attest"])
def test_real_names(name):
    assert not store.is_test_name(name)


def test_a_test_booking_has_a_blocker_and_a_real_one_does_not():
    now = datetime.now(timezone.utc)
    assert store.blockers(held(), now, 600) == []
    assert store.blockers(held(test_booking=True), now, 600) == [store.TEST_BOOKING]
    assert store.TEST_BOOKING in store.blockers(replace(held(), test_booking=True, booking_status="expired"), now, 600)
