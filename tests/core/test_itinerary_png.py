"""GET /v1/admin/bookings/{id}/itinerary.pdf?format=png: the finished itinerary PDF as one PNG (PyMuPDF, about 150 DPI,
white background, pages stacked). Same permissions, 403/404 and full passport numbers as the PDF."""

import io

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.db import booking_people
from app.services.itinerary_pdf import to_png
from tests.core.test_admin_bookings import SUPER_ADMIN, client
from tests.core.test_itinerary_pdf import FLIGHT_HOLD, GROUP_ONE_WAY, GROUP_PAID, ISSUED, people, traveler
from tests.core.test_itinerary_pdf import get as get_pdf

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
A4_PX = (round(A4[0] / 72 * 150), round(A4[1] / 72 * 150))  # about 1240 x 1754 at 150 DPI


def fetch(monkeypatch, reference, viewer=SUPER_ADMIN, who=None, fmt="png"):
    api = client(monkeypatch, rows=[FLIGHT_HOLD, GROUP_PAID, ISSUED, GROUP_ONE_WAY], viewer=viewer)
    monkeypatch.setattr(booking_people, "read", lambda ref: who)
    return api.get(f"/v1/admin/bookings/{reference}/itinerary.pdf", params={"format": fmt})


def image_of(content: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(content))
    image.load()
    return image


def test_png_for_hold_paid_and_issued_is_a_valid_a4_image_named_after_the_booking(monkeypatch):
    cases = {"TRV-41": people(traveler(0, "Ali")),
             "TRV-42": people(traveler(0, "Ali", fare=8_820_000), traveler(1, "Sana", fare=8_820_000), paid=17_640_000),
             "TRV-43": people(traveler(0, "Ali", ticket="2147483647001"), traveler(1, "Sana", ticket="2147483647002"), paid=17_600_000)}
    for reference, who in cases.items():
        response = fetch(monkeypatch, reference, who=who)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"] == f'attachment; filename="{reference}-itinerary.png"'
        assert response.content.startswith(PNG_SIGNATURE)
        image = image_of(response.content)
        assert image.format == "PNG" and image.mode == "RGB"  # no alpha channel: a white background
        assert abs(image.width - A4_PX[0]) <= 2 and abs(image.height - A4_PX[1]) <= 2
        assert len(image.getcolors(maxcolors=1_000_000) or []) > 10  # drawn, not blank


def test_pdf_stays_the_default(monkeypatch):
    response = get_pdf(monkeypatch, "TRV-41", who=people(traveler(0, "Ali")))
    assert response.headers["content-type"] == "application/pdf" and response.content.startswith(b"%PDF-")


def test_403_and_404_match_the_pdf(monkeypatch):
    assert fetch(monkeypatch, "TRV-NOPE").status_code == get_pdf(monkeypatch, "TRV-NOPE").status_code == 404
    assert fetch(monkeypatch, "TRV-41", viewer=None).status_code == get_pdf(monkeypatch, "TRV-41", viewer=None).status_code == 403
    assert fetch(monkeypatch, "TRV-41", fmt="gif").status_code == 422


def test_more_than_one_page_is_stacked_into_one_white_image():
    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=A4)
    for number in (1, 2, 3):
        sheet.drawString(72, 720, f"Page {number}")
        sheet.showPage()
    sheet.save()
    image = image_of(to_png(buffer.getvalue()))
    assert abs(image.width - A4_PX[0]) <= 2 and abs(image.height - 3 * A4_PX[1]) <= 6
    assert image.getpixel((5, 5)) == (255, 255, 255)
