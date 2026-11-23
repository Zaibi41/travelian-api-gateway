"""gateway#62: the itinerary / e-ticket PDF reads airline logos from the website's one shared manifest, with an IATA code
badge when there is no logo; Gulf Air included. A fake site answers here; nothing reaches the real website."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import airline_logos, itinerary_pdf

PNG = (Path(__file__).parent / "tiny-logo.png").read_bytes()
MANIFEST = {
    "codes": {"gulf air": "GF", "airsial": "PF", "british airways": "BA"},
    "logos": {"gf": "/airline-logos/gulf-air.png", "pf": "/airline-logos/airsial-mark.png", "emirates": "/airline-logos/emirates.png",
              "ba": "/airline-logos/british-airways.png", "evil": "https://elsewhere.example/x.png", "svg": "/airline-logos/travelport.svg"},
}


class Answer:
    def __init__(self, status=200, json=None, content=b"", kind="application/json"):
        self.status_code, self._json, self.content, self.headers = status, json, content, {"content-type": kind}

    def json(self):
        return self._json


@pytest.fixture
def site(monkeypatch):
    calls = []

    def get(url):
        calls.append(url)
        if url == "https://travelian.pk/airline-logos/index.json":
            return Answer(json=MANIFEST)
        if url.startswith("https://travelian.pk/airline-logos/") and url.endswith(".png"):
            if "british" in url:
                return Answer(content=b"<html>not an image</html>", kind="text/html")
            return Answer(content=PNG, kind="image/png")
        return Answer(status=404)

    monkeypatch.setattr(settings, "public_site_url", "https://travelian.pk")
    monkeypatch.setattr(settings, "airline_logos_site_url", None)
    monkeypatch.setattr(airline_logos, "_get", get)
    airline_logos.clear_cache()
    return calls


def test_logos_come_from_the_shared_manifest_by_code_then_name(site):
    assert airline_logos.resolve("GF", None) == ("GF", "/airline-logos/gulf-air.png")       # Gulf Air (gateway#62)
    assert airline_logos.resolve(None, "AirSial") == ("PF", "/airline-logos/airsial-mark.png")
    assert airline_logos.resolve("ek", "Emirates") == ("EK", "/airline-logos/emirates.png")    # by name when the code has none
    assert airline_logos.resolve("ZZ", "Unknown Air") == ("ZZ", None)
    assert airline_logos.logo_for("GF", "Gulf Air") == ("GF", PNG)


def test_only_the_sites_own_png_or_jpeg_files_are_read(site):
    assert airline_logos.image_bytes("https://elsewhere.example/x.png") is None
    assert airline_logos.image_bytes("/airline-logos/travelport.svg") is None
    assert airline_logos.image_bytes("/airline-logos/../secrets.png") is None
    assert airline_logos.image_bytes("/airline-logos/british-airways.png") is None  # not an image: badge instead
    assert not any("elsewhere" in url or ".svg" in url or ".." in url for url in site)


def test_manifest_and_files_are_cached(site):
    for _ in range(3):
        airline_logos.logo_for("GF", None)
    assert site.count("https://travelian.pk/airline-logos/index.json") == 1
    assert site.count("https://travelian.pk/airline-logos/gulf-air.png") == 1


def test_a_logo_site_setting_overrides_the_public_site(site, monkeypatch):
    monkeypatch.setattr(settings, "airline_logos_site_url", "https://staging.travelian.pk")
    assert airline_logos.manifest() is None  # the fake site only answers travelian.pk
    assert site[-1] == "https://staging.travelian.pk/airline-logos/index.json"


def test_pdf_logo_is_the_image_or_the_code_badge_never_empty(site):
    from reportlab.platypus import Image, Table

    assert isinstance(itinerary_pdf._airline_logo("GF", "Gulf Air", 8), Image)
    badge = itinerary_pdf._airline_logo("BA", "British Airways", 8)   # the file isn't an image
    assert isinstance(badge, Table) and "BA" in str(badge._cellvalues[0][0].text)
    assert isinstance(itinerary_pdf._airline_logo("ZZ", None, 8), Table)
    assert itinerary_pdf._airline_logo(None, None, 8) is None


def test_offline_manifest_gives_code_badges(monkeypatch):
    from reportlab.platypus import Table

    airline_logos.clear_cache()   # the conftest keeps the site unreachable
    assert airline_logos.manifest() is None
    assert isinstance(itinerary_pdf._airline_logo("PK", "PIA", 8), Table)
    assert itinerary_pdf.AIRLINES["GF"] == "Gulf Air"
