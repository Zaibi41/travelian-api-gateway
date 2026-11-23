"""travelian#39: the PDF's airport city names are the city the airport serves (ATH is Athens, not Spata-Artemida),
the same as the website's airport list."""

from app.services import itinerary_pdf


def test_ath_is_athens():
    assert itinerary_pdf.city("ATH") == "Athens"
    assert "Spata-Artemida" not in itinerary_pdf._cities().values()
