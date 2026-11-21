"""Admin announcements (travelian#92): validation and status rules, no database."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.admin_announcements import AnnouncementError, check_window, clean_message, status_of
from app.main import app

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


def test_message_is_trimmed_to_one_line():
    assert clean_message("  Airblue fares   are\nlower today  ") == "Airblue fares are lower today"


@pytest.mark.parametrize("message", ["", " ", "a", "  a \n", "x" * 241])
def test_message_must_be_2_to_240_characters(message):
    with pytest.raises(AnnouncementError, match="2 to 240 characters") as error:
        clean_message(message)
    assert error.value.status == 422


def test_240_characters_is_accepted():
    assert clean_message("x" * 240) == "x" * 240


@pytest.mark.parametrize("message", ["<script>alert(1)</script>", "Fares <b>lower</b>", "a > b", "<img src=x onerror=alert(1)>"])
def test_html_is_refused(message):
    with pytest.raises(AnnouncementError, match="plain text only: < and > are not allowed"):
        clean_message(message)


@pytest.mark.parametrize("message", ["Fares\x00 lower", "Fares ‮ lower", "Bell\x07 here"])
def test_control_characters_are_refused(message):
    with pytest.raises(AnnouncementError, match="Use plain text only"):
        clean_message(message)


def test_urdu_and_punctuation_are_plain_text():
    assert clean_message("نئی گروپ سیٹیں دستیاب ہیں & 20% off \"today\"") == "نئی گروپ سیٹیں دستیاب ہیں & 20% off \"today\""


def test_window_rules():
    check_window(None, None, NOW)
    check_window(NOW + timedelta(hours=1), NOW + timedelta(hours=2), NOW)
    with pytest.raises(AnnouncementError, match="end time must be in the future"):
        check_window(None, NOW, NOW)
    with pytest.raises(AnnouncementError, match="after the start time"):
        check_window(NOW + timedelta(hours=2), NOW + timedelta(hours=1), NOW)
    with pytest.raises(AnnouncementError, match="time zone"):
        check_window(datetime(2026, 11, 23, 10, 0), None, NOW)


def test_status():
    assert status_of(True, None, None, NOW) == "active"
    assert status_of(True, NOW - timedelta(minutes=1), NOW + timedelta(minutes=1), NOW) == "active"
    assert status_of(True, NOW + timedelta(minutes=1), None, NOW) == "scheduled"
    assert status_of(True, None, NOW, NOW) == "ended"
    assert status_of(False, None, None, NOW) == "ended"


def test_routes_are_in_the_openapi_schema():
    paths = app.openapi()["paths"]
    assert set(paths["/v1/admin/announcements"]) == {"get", "post"}
    assert set(paths["/v1/admin/announcements/all"]) == {"get"}
    assert set(paths["/v1/admin/announcements/{announcement_id}/end"]) == {"post"}
    assert set(paths["/v1/admin/announcements/{announcement_id}"]) == {"delete"}

