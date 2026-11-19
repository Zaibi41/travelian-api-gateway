"""A link opens exactly its booking until it expires; only the token's hash is stored."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.services import customer_emails
from tests.database.test_booking_people import save


def test_link_opens_one_booking_until_it_expires(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    booking_id = connection.scalar(text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference})
    now = datetime.now(timezone.utc)
    token = customer_emails.create_link(booking_id, [], "booking_held", now=now)
    assert connection.scalar(text("SELECT count(*) FROM booking_view_links WHERE token_hash = :t"), {"t": token}) == 0  # hash only
    assert customer_emails.booking_for_link(token, now=now) == reference
    assert customer_emails.booking_for_link(token + "x", now=now) is None
    assert customer_emails.booking_for_link(token, now=now + timedelta(days=31)) is None
