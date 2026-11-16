"""Group fares can't be cancelled through the supplier APIs: the gateway offers no cancel route for bookings or group
holds (an unpaid hold lapses at the supplier's expiry; Release hold stays, it isn't a cancellation)."""

from app.main import app


def test_no_cancel_route_for_bookings_or_group_holds():
    paths = list(app.openapi()["paths"])
    booking_paths = [p for p in paths if "/admin/bookings" in p or "group" in p]
    assert booking_paths, "the booking routes are mounted"
    assert not [p for p in booking_paths if "cancel" in p.lower()]
    assert any(p.endswith("/release") for p in booking_paths)
