"""The website (another origin) sends X-Booking-Token on guest booking calls: the CORS preflight must allow it."""

import importlib
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings


def test_preflight_allows_the_booking_token_header():
    with patch.object(settings, "cors_allowed_origins", ["https://travelian.pk"]):
        import app.main as main

        app = importlib.reload(main).app
    response = TestClient(app).options("/v1/public/bookings/TRV-ABC123/payments/payfast", headers={
        "Origin": "https://travelian.pk", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-booking-token"})
    assert response.status_code == 200
    assert "x-booking-token" in response.headers["access-control-allow-headers"].lower()
    importlib.reload(main)  # back to the test settings
