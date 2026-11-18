"""Flight fare calendar: only real search results, never a guessed price; a bounded number of dates checked."""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.services import public_booking


def view(prices):
    fares = [SimpleNamespace(selling_price=SimpleNamespace(amount_minor=p, currency="PKR")) for p in prices]
    return SimpleNamespace(status="complete", journeys=[SimpleNamespace(direction="outbound", flights=[SimpleNamespace(fares=fares)])])


def test_calendar_shows_only_real_searched_prices_and_checks_a_bounded_set():
    public_booking._fares_by_date.clear()
    public_booking._refresh_jobs.clear()
    public_booking._flight_cache.clear()
    public_booking._supplier_slots = None
    today = public_booking.datetime.now(public_booking.timezone.utc).date()
    searched = []

    async def fake_search(request, viewer):
        searched.append(request.departure_date)
        offset = (request.departure_date - today).days
        return view([] if offset == 2 else [5_900_000 + offset * 100_000, 9_900_000])  # day 2: searched, no fares

    async def run():
        with patch("app.services.live_flights.search", fake_search):
            first = await public_booking.flight_calendar("KHI", "LHE", 1, "economy")
            again = await public_booking.flight_calendar("KHI", "LHE", 1, "economy")  # job running: not started twice
            await public_booking._refresh_jobs[("KHI", "LHE", "economy", 1)]
            done = await public_booking.flight_calendar("KHI", "LHE", 1, "economy")
        return first, again, done

    first, again, done = asyncio.run(run())
    assert len(first.days) == public_booking.CALENDAR_DAYS and first.pending
    assert not any(day.lowest_price for day in first.days)  # nothing known yet: no price anywhere
    assert again.pending and len(searched) == public_booking.CALENDAR_REFRESH_DAYS  # 14 dates, once each
    assert sorted(set(searched)) == [today + timedelta(days=n) for n in range(public_booking.CALENDAR_REFRESH_DAYS)]
    by_offset = {(day.date - today).days: day for day in done.days}
    assert by_offset[0].lowest_price.amount_minor == 5_900_000 and by_offset[1].lowest_price.amount_minor == 6_000_000
    assert by_offset[2].checked and not by_offset[2].available and by_offset[2].lowest_price is None
    assert all(by_offset[n].lowest_price is None and not by_offset[n].checked for n in range(14, 60))  # never guessed
