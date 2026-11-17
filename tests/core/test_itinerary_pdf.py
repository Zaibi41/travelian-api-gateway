"""GET /v1/admin/bookings/{id}/itinerary.pdf (ReportLab layout): a PDF named after the booking reference, for
back-office users. Status banner per state (hold, paid not issued, issued), one block per flight outbound then return,
the selling price only, no supplier name, group id, cost or profit; a group booking only once Ticketed, with the
airline's final PNR (409 before); full passport numbers for every back-office viewer (nothing is masked). The text is read back with pdftotext."""

import re
import shutil
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.field_visibility import NONE, BackOfficeUser
from app.db import booking_people
from tests.core.test_admin_bookings import NOW, STAFF_NAMES, SUPER_ADMIN, booking, client

pytestmark = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext (poppler-utils) reads the PDF text back")

STAFF = BackOfficeUser("u-2", "staff", NONE)
OUT = {"direction": "outbound", "origin": "ISB", "destination": "JED", "airline_name": "Fly Jinnah", "flight_number": "9P700",
       "departure_local": "2026-12-02T06:40", "arrival_local": "2026-12-02T10:05", "baggage": "10+20 kg", "meal": "No"}
BACK = {"direction": "return", "origin": "JED", "destination": "ISB", "airline_name": "Fly Jinnah", "flight_number": "9P701",
        "departure_local": "2026-12-21T11:05", "arrival_local": "2026-12-21T18:05", "baggage": "10+30 kg", "meal": "No"}
FLIGHT_HOLD = booking(41, status="held", payment_status="unpaid", ticket_status="not_issued", pnr="63B55C", provider="southair",
                      hold_expires_at=NOW + timedelta(hours=3), total_minor=8_800_000, supplier_cost_minor=8_500_000, itinerary=[OUT])
GROUP_PAID = booking(42, product="group", status="held", payment_status="paid", ticket_status="not_issued", pnr="4YB8HL-4VK1MF", provider="skypass",
                     total_minor=17_640_000, supplier_cost_minor=17_600_000, hold_expires_at=NOW + timedelta(minutes=30), itinerary=[OUT, BACK])
ISSUED = booking(43, status="ticket_issued", payment_status="paid", ticket_status="issued", pnr="6393B8", provider="southair", itinerary=[OUT, BACK],
                 total_minor=17_600_000, supplier_cost_minor=17_000_000)
GROUP_ONE_WAY = booking(44, product="group", status="held", payment_status="unpaid", ticket_status="not_issued", pnr="41SDS4", provider="dhillu_sahi",
                        hold_expires_at=NOW + timedelta(minutes=40), itinerary=[{**OUT, "origin": "LHE", "destination": "DMM", "airline_name": "PF",
                                                                                  "flight_number": "PF742", "meal": "0"}])
GROUP_TICKETED = booking(45, product="group", status="ticket_issued", payment_status="paid", ticket_status="issued", pnr="4YB8HL-4VK1MF",
                         provider="skypass", final_pnr="K7P2QX", total_minor=17_640_000, supplier_cost_minor=17_600_000, itinerary=[OUT, BACK])
GROUP_TICKETS = (lambda: people(traveler(0, "Ali", fare=8_820_000, ticket="2147483647011"), traveler(1, "Sana", fare=8_820_000, ticket="2147483647012"),
                                paid=17_640_000))


def traveler(position, first, last="Raza", kind="ADT", fare=8_800_000, ticket=None):
    return SimpleNamespace(position=position, passenger_type=kind, title="MR", first_name=first, last_name=last, id_type="PSPT",
                           document_number="ZU3124251", selling_minor=fare, ticket_number=ticket)


def people(*travelers, paid=0):
    return SimpleNamespace(customer=SimpleNamespace(first_name="Ali", last_name="Raza"), travelers=list(travelers),
                           payments=[SimpleNamespace(amount_minor=paid)] if paid else [])


def pdf_text(content: bytes, tmp_path) -> str:
    path = tmp_path / "itinerary.pdf"
    path.write_bytes(content)
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True).stdout


def get(monkeypatch, reference, viewer=SUPER_ADMIN, who=None):
    api = client(monkeypatch, rows=[FLIGHT_HOLD, GROUP_PAID, ISSUED, GROUP_ONE_WAY, GROUP_TICKETED], viewer=viewer)
    monkeypatch.setattr(booking_people, "read", lambda ref: who)
    return api.get(f"/v1/admin/bookings/{reference}/itinerary.pdf")


def test_hold_pdf_status_banner_pkt_expiry_and_one_flight(monkeypatch, tmp_path):
    response = get(monkeypatch, "TRV-41", who=people(traveler(0, "Ali")))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="TRV-41.pdf"'
    assert response.content.startswith(b"%PDF-") and len(response.content) > 10_000
    text = pdf_text(response.content, tmp_path)
    assert "Your booking is on hold" in text and re.search(r"Hold expires \d\d \w{3} 2026, \d\d:\d\d PKT", text)
    assert "TRV-41" in text and "PNR 63B55C" in text and "BOOKING ITINERARY" in text
    assert "Outbound" in text and "Return" not in text and "Islamabad to Jeddah" in text
    assert "06:40" in text and "10:05" in text and "Flight 9P 700" in text and "10+20 kg" in text
    assert "Mr Ali Raza" in text and "Rs 88,000" in text and "Travelian" in text and "+92 324 4440014" in text


def test_ticketed_group_return_shows_both_flights_the_final_pnr_and_the_selling_price_only(monkeypatch, tmp_path):
    text = pdf_text(get(monkeypatch, "TRV-45", who=GROUP_TICKETS()).content, tmp_path)
    assert "E-ticket issued" in text and "PNR K7P2QX" in text and "4YB8HL" not in text
    assert "2147483647011" in text and "2147483647012" in text
    assert text.index("Outbound") < text.index("Return") and "Jeddah to Islamabad" in text and "Flight 9P 701" in text
    assert "Rs 176,400" in text and "176,000" not in text and "Profit" not in text and "cost" not in text.lower()
    assert "skypass" not in text.lower() and not re.search(r"Group \d", text)


def test_issued_pdf_is_an_e_ticket_with_ticket_numbers(monkeypatch, tmp_path):
    who = people(traveler(0, "Ali", ticket="2147483647001"), traveler(1, "Sana", ticket="2147483647002"), paid=17_600_000)
    text = pdf_text(get(monkeypatch, "TRV-43", who=who).content, tmp_path)
    assert "E-TICKET & ITINERARY" in text and "E-ticket issued" in text and "Ticketed" in text
    assert "2147483647001" in text and "2147483647002" in text and "Rs 0" in text


def test_a_group_itinerary_is_refused_until_ticketed(monkeypatch):
    for reference, who in (("TRV-44", people(traveler(0, "Ali"))),                                     # held, unpaid
                           ("TRV-42", people(traveler(0, "Ali", fare=8_820_000), traveler(1, "Sana", fare=8_820_000), paid=17_640_000))):  # paid
        response = get(monkeypatch, reference, who=who)
        assert response.status_code == 409
        assert response.json()["detail"] == "Available once the tickets are issued (final PNR)"


def test_a_ticketed_group_prints_the_final_pnr_for_every_viewer_and_never_the_supplier(monkeypatch, tmp_path):
    for viewer in (SUPER_ADMIN, STAFF_NAMES, STAFF):
        text = pdf_text(get(monkeypatch, "TRV-45", viewer=viewer, who=GROUP_TICKETS()).content, tmp_path)
        assert "PNR K7P2QX" in text and "4YB8HL" not in text and "skypass" not in text.lower()
    assert "PNR 63B55C" in pdf_text(get(monkeypatch, "TRV-41", viewer=STAFF, who=people(traveler(0, "Ali"))).content, tmp_path)


def test_passport_numbers_print_in_full_for_every_back_office_viewer(monkeypatch, tmp_path):
    for viewer in (SUPER_ADMIN, STAFF):
        text = pdf_text(get(monkeypatch, "TRV-41", viewer=viewer, who=people(traveler(0, "Ali"))).content, tmp_path)
        assert "ZU3124251" in text and "•" not in text


def test_three_travelers_and_names_beyond_latin(monkeypatch, tmp_path):
    who = people(traveler(0, "Naziran", "Bibi"), traveler(1, "Aysha", "Qamar"), traveler(2, "Ölüm", "Øresund Ñandú"))
    text = pdf_text(get(monkeypatch, "TRV-41", who=who).content, tmp_path)
    assert "Mr Naziran Bibi" in text and "Mr Aysha Qamar" in text and "Mr Ölüm Øresund Ñandú" in text and "?" not in text


def test_unknown_booking_is_404_and_other_roles_are_refused(monkeypatch):
    assert get(monkeypatch, "TRV-NOPE").status_code == 404
    assert get(monkeypatch, "TRV-41", viewer=None).status_code == 403


def acknowledged(*travelers, paid=0):
    """A group booking whose events carry the stored non-refundable, non-changeable acknowledgement."""
    from app.db.booking_people import AuditEvent
    from app.services import group_terms
    who = people(*travelers, paid=paid)
    who.events = [AuditEvent("fare_terms_acknowledged", "u-1", NOW, group_terms.event_details())]
    return who


def test_group_pdf_carries_non_refundable_next_to_the_fare_and_the_full_line_in_the_terms_box(monkeypatch, tmp_path):
    who = acknowledged(traveler(0, "Ali", fare=8_820_000, ticket="2147483647011"), traveler(1, "Sana", fare=8_820_000, ticket="2147483647012"),
                       paid=17_640_000)
    text = " ".join(pdf_text(get(monkeypatch, "TRV-45", who=who).content, tmp_path).split())
    assert "Non-refundable · Non-changeable" in text
    assert "TERMS 1 Group fares are non-refundable and non-changeable. Names and dates cannot be altered after booking." in text
    assert text.index("Rs 176,400") < text.index("Non-refundable · Non-changeable") < text.index("FLIGHTS")   # right under the fare


def test_a_booking_without_the_stored_term_has_no_terms_box(monkeypatch, tmp_path):
    text = pdf_text(get(monkeypatch, "TRV-41", who=people(traveler(0, "Ali"))).content, tmp_path)
    assert "Non-refundable" not in text and "TERMS" not in text
