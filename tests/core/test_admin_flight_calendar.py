from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.field_visibility import ALL, BackOfficeUser, require_search_viewer
from app.main import app
from app.services import public_booking


def test_the_admin_date_picker_gets_the_website_fare_calendar():
    calendar = public_booking.FlightCalendar(origin="LHE", destination="KHI", pending=False, days=[])
    app.dependency_overrides[require_search_viewer] = lambda: BackOfficeUser("u1", "staff", ALL)
    try:
        with patch.object(public_booking, "flight_calendar", AsyncMock(return_value=calendar)) as build:
            answer = TestClient(app).post("/v1/live-ticketing/flights/calendar", json={"origin": "lhe", "destination": "KHI", "adults": 2})
    finally:
        app.dependency_overrides.pop(require_search_viewer, None)
    assert answer.status_code == 200 and answer.json()["origin"] == "LHE"
    build.assert_awaited_once_with("LHE", "KHI", 2, "economy", None)


def test_signed_out_is_401(monkeypatch):
    from fastapi import FastAPI

    from app.api.routes.live_ticketing import router
    from app.core.config import settings

    monkeypatch.setattr(settings, "cognito_issuer", "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Test1")
    monkeypatch.setattr(settings, "cognito_app_client_id", "testclient123")
    signed_out = FastAPI()
    signed_out.include_router(router, prefix="/v1")
    assert TestClient(signed_out).post("/v1/live-ticketing/flights/calendar", json={"origin": "LHE", "destination": "KHI"}).status_code == 401
