"""B7 / D33: the group fare calendar shows only real supplier prices; a day the supplier didn't return has no price."""

from datetime import date
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.group_ticketing import GroupInventory
from app.services import group_calendar, public_booking
from tests.group_ticketing.test_return_and_search import SKY_ONE_WAY, SKY_RETURN, sky


def groups():
    one_way = sky(SKY_ONE_WAY)  # LHE-DMM on 2026-12-01, Rs 95,000
    cheaper = sky({**SKY_ONE_WAY, "id": 12002, "adult_price": "90000", "airline_pnr": "4ONEWY-4BBBBB"})  # same day, cheaper
    later = sky({**SKY_ONE_WAY, "id": 12003, "adult_price": "99000", "airline_pnr": "4ONEWY-4CCCCC",
                 "sectors": [{**SKY_ONE_WAY["sectors"][0], "departure_date": "2026-12-04", "arrival_date": "2026-12-04"}]})
    other = sky(SKY_RETURN)  # ISB-JED: must not appear on LHE-DMM
    return [GroupInventory(id="g", vendor="skypass", sector="LHE - DMM", airline="9P", tickets=[one_way, cheaper, later]),
            GroupInventory(id="o", vendor="skypass", sector="ISB - JED", airline="9P", tickets=[other])]


def test_only_supplier_dates_have_prices_and_every_other_day_is_shown_as_no_availability():
    calendar = group_calendar.build(groups(), "LHE", "DMM", 1, date(2026, 11, 28), days=10)
    days = {day.date.isoformat(): day for day in calendar.days}
    assert len(calendar.days) == 10 and calendar.start == date(2026, 11, 28)  # every day in the range, none skipped
    priced = {d: day.lowest_price.amount_minor for d, day in days.items() if day.lowest_price}
    assert priced == {"2026-12-01": 9_000_000, "2026-12-04": 9_900_000}  # the lowest real price that day, nothing else
    for d, day in days.items():
        if d not in priced:
            assert day.available is False and day.lowest_price is None and day.fares == 0, d
    assert days["2026-12-01"].fares == 2


def test_a_sector_the_supplier_has_nothing_for_is_all_no_availability():
    calendar = group_calendar.build(groups(), "LHE", "JED", 1, date(2026, 11, 28), days=30)
    assert len(calendar.days) == 30 and not any(day.available or day.lowest_price for day in calendar.days)


def test_public_calendar_route():
    public_booking._cache = None
    with patch.object(public_booking, "fetch_all_group_inventory", AsyncMock(return_value=groups())), \
         patch("app.services.group_calendar.date") as fake_date:
        fake_date.fromisoformat = date.fromisoformat
        response = TestClient(app).post("/v1/public/group-ticketing/calendar", json={"origin": "lhe", "destination": "DMM"})
    public_booking._cache = None
    assert response.status_code == 200
    body = response.json()
    assert all(("lowest_price" in day) == day["available"] for day in body["days"])
    assert "skypass" not in response.text.lower()
