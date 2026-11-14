"""GET /v1/admin/bookings/{id}/itinerary.pdf: a PDF named after the booking reference, for back-office users, with no
supplier name, group id, cost or profit; a group booking's PNR (the supplier's) only with supplier access."""

import re
from datetime import timedelta

from app.core.field_visibility import NONE, BackOfficeUser
from app.db import booking_people
from tests.core.test_admin_bookings import NOW, STAFF_NAMES, SUPER_ADMIN, booking, client

STAFF = BackOfficeUser("u-2", "staff", NONE)
FLIGHT = booking(41, status="held", payment_status="unpaid", ticket_status="not_issued", pnr="63B55C", provider="southair",
                 hold_expires_at=NOW + timedelta(hours=3), itinerary=[{"direction": "outbound", "origin": "KHI", "destination": "MUX",
                 "airline_name": "Synthetic Aviation", "flight_number": "MM112", "departure_local": "2026-11-27T23:00", "arrival_local": "2026-11-28T00:00"}])
GROUP_RETURN = booking(42, product="group", status="held", payment_status="unpaid", ticket_status="not_issued", pnr="4YB8HL-4VK1MF",
                       provider="skypass", total_minor=17_640_000, supplier_cost_minor=17_600_000, hold_expires_at=NOW + timedelta(minutes=30),
                       itinerary=[{"direction": "outbound", "origin": "ISB", "destination": "JED", "airline_name": "Fly Jinnah", "flight_number": "9P700",
                                   "departure_local": "2026-12-02T06:40", "arrival_local": "2026-12-02T10:05", "baggage": "10+20 kg"},
                                  {"direction": "return", "origin": "JED", "destination": "ISB", "airline_name": "Fly Jinnah", "flight_number": "9P701",
                                   "departure_local": "2026-12-21T11:05", "arrival_local": "2026-12-21T18:05", "baggage": "10+30 kg"}])
GROUP_ONE_WAY = booking(43, product="group", status="held", payment_status="unpaid", ticket_status="not_issued", pnr="41SDS4", provider="dhillu_sahi",
                        itinerary=[{"direction": "outbound", "origin": "LHE", "destination": "DMM", "flight_number": "PF742", "departure_local": "2026-12-05T21:10",
                                    "meal": "0"}])


def text_of(pdf: bytes) -> str:
    """The text drawn on the pages (the content streams are uncompressed)."""
    return " ".join(match.decode("latin-1").replace("\\(", "(").replace("\\)", ")") for match in re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", pdf))


def get(monkeypatch, reference, viewer=SUPER_ADMIN, rows=None):
    api = client(monkeypatch, rows=rows or [FLIGHT, GROUP_RETURN, GROUP_ONE_WAY], viewer=viewer)
    monkeypatch.setattr(booking_people, "read", lambda reference: None)
    return api.get(f"/v1/admin/bookings/{reference}/itinerary.pdf")


def test_flight_itinerary_is_a_pdf_named_after_the_reference(monkeypatch):
    response = get(monkeypatch, "TRV-41")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="TRV-41.pdf"'
    assert response.content.startswith(b"%PDF-") and response.content.rstrip().endswith(b"%%EOF") and len(response.content) > 500
    text = text_of(response.content)
    assert "Booking reference: TRV-41" in text and "PNR: 63B55C" in text  # the airline PNR, for everyone
    assert "Outbound: KHI to MUX" in text and "Departs 27 Nov 2026 23:00, arrives 28 Nov 2026 00:00" in text
    assert "Total: Rs 10,000" in text


def test_group_return_shows_both_flights_and_the_selling_price_only(monkeypatch):
    text = text_of(get(monkeypatch, "TRV-42").content)
    assert text.index("Outbound: ISB to JED") < text.index("Return: JED to ISB")
    assert "Fly Jinnah 9P701, Baggage 10+30 kg" in text and "Total: Rs 176,400" in text
    assert "176,000" not in text and "Profit" not in text and "cost" not in text.lower()  # no cost or profit, even for Super Admin
    assert "skypass" not in text.lower() and not re.search(r"Group \d", text)  # never a supplier name or group id


def test_group_one_way_works_and_its_supplier_pnr_needs_supplier_access(monkeypatch):
    for viewer, sees in ((SUPER_ADMIN, True), (STAFF_NAMES, True), (STAFF, False)):
        text = text_of(get(monkeypatch, "TRV-43", viewer=viewer).content)
        assert "Outbound: LHE to DMM" in text and "PF742, No meal" in text and "Meal 0" not in text
        assert ("41SDS4" in text) is sees
        assert "dhillu" not in text.lower()
    assert "PNR: 63B55C" in text_of(get(monkeypatch, "TRV-41", viewer=STAFF).content)  # a flight's airline PNR: everyone


def test_unknown_booking_is_404_and_other_roles_are_refused(monkeypatch):
    assert get(monkeypatch, "TRV-NOPE").status_code == 404
    assert get(monkeypatch, "TRV-41", viewer=None).status_code == 403


def test_travelers_and_payments_when_saved_without_documents(monkeypatch):
    from types import SimpleNamespace

    people = SimpleNamespace(customer=SimpleNamespace(first_name="Ali", last_name="Raza"),
                             travelers=[SimpleNamespace(passenger_type="ADT", title="MR", first_name="Ali", last_name="Raza", document_number_masked="AB•••4567"),
                                        SimpleNamespace(passenger_type="INF", title="MISS", first_name="Baby", last_name="Raza", document_number_masked="CD•••4321")],
                             payments=[SimpleNamespace(amount_minor=400_000)])
    api = client(monkeypatch, rows=[FLIGHT], viewer=SUPER_ADMIN)
    monkeypatch.setattr(booking_people, "read", lambda reference: people)
    text = text_of(api.get("/v1/admin/bookings/TRV-41/itinerary.pdf").content)
    assert "1. Mr Ali Raza (Adult)" in text and "2. Miss Baby Raza (Infant)" in text and "Customer Ali Raza" in text
    assert "Paid: Rs 4,000" in text and "Due: Rs 6,000" in text and "4567" not in text  # never a document number
