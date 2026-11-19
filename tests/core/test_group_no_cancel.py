"""Group fares can't be cancelled through the supplier APIs: the gateway offers no cancel route for group bookings or group
holds (an unpaid hold lapses at the supplier's expiry; Release hold stays, it isn't a cancellation). The one cancel route,
/admin/bookings/{id}/supplier-cancel (owner's request, Nov 2026), is for held SooperFare flight bookings only."""

import asyncio

import pytest

from app.main import app
from app.services import sooperfare_bookings


def test_no_cancel_route_for_group_bookings_or_group_holds():
    paths = list(app.openapi()["paths"])
    booking_paths = [p for p in paths if "/admin/bookings" in p or "group" in p]
    assert booking_paths, "the booking routes are mounted"
    assert [p for p in booking_paths if "cancel" in p.lower()] == ["/v1/admin/bookings/{booking_id}/supplier-cancel"]
    assert not [p for p in paths if "group" in p and "cancel" in p.lower()]
    assert any(p.endswith("/release") for p in booking_paths)


def test_supplier_cancel_refuses_anything_but_a_sooperfare_booking(monkeypatch):
    record = sooperfare_bookings.Record("b1", "TRV-GRP-1", "dhillu_sahi", "held", "ABC123", "g-1", 100, "PKR", {})
    monkeypatch.setattr(sooperfare_bookings, "_load", lambda reference: record)
    with pytest.raises(sooperfare_bookings.SupplierCancelError) as error:
        asyncio.run(sooperfare_bookings.cancel("TRV-GRP-1", "u1", lambda: None))
    assert error.value.code == "not_sooperfare"
