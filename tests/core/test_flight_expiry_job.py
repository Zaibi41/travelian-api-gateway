"""The hold expiry job's flight step: counts logged each run, payments on expired bookings flagged with the booking id,
no supplier call. The store is mocked; tests/database/test_flight_expiry.py covers the SQL."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.db import flight_expiry
from app.db import group_holds as group_store
from app.services import group_hold_expiry

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def run(expired):
    supplier = MagicMock()
    with patch.object(flight_expiry, "expire_due", MagicMock(return_value=expired)) as expire, \
         patch.object(group_store, "expire_due", lambda now, no_release=frozenset(): []), patch.object(group_store, "releases_due", lambda limit: []), \
         patch.object(group_hold_expiry, "provider_for", lambda vendor: supplier):
        outcome = asyncio.run(group_hold_expiry.run_once(NOW))
    return outcome, expire, supplier


def test_expired_flights_are_counted_and_payments_flagged_by_booking(caplog):
    from app.services import flight_fallback

    caplog.set_level("INFO")
    lost = []

    async def hold_lost(reference, reason):
        lost.append(reference)

    with patch.object(flight_fallback, "hold_lost", hold_lost):
        outcome, expire, supplier = run([flight_expiry.FlightExpired("TRV-ML2RP3", 2_666_000, "PKR", "paid_not_ticketed"),
                                     flight_expiry.FlightExpired("TRV-AB12CD", 0, "PKR")])
    assert outcome["flights_expired"] == ["TRV-ML2RP3", "TRV-AB12CD"] and expire.call_args.args == (NOW,)
    counted = next(r for r in caplog.records if r.getMessage() == "Flight holds expired")
    assert counted.count == 2
    flagged = [r for r in caplog.records if r.getMessage().startswith("Flight hold ended with a payment recorded")]
    assert [(r.reference, r.amount_minor) for r in flagged] == [("TRV-ML2RP3", 2_666_000)]
    # Staff get the "hold lost" red alert for the paid one only (refund or rebook by hand).
    assert lost == ["TRV-ML2RP3"]
    assert supplier.mock_calls == []  # no supplier or airline call


def test_nothing_due_logs_zero(caplog):
    caplog.set_level("INFO")
    outcome, _, _ = run([])
    assert outcome["flights_expired"] == [] and next(r for r in caplog.records if r.getMessage() == "Flight holds expired").count == 0
