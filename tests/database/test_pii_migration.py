"""gateway#84 migration 0035: additive room for encrypted identity; existing rows untouched; round trip."""

import pytest
import sqlalchemy as sa
from alembic import command


def columns(connection, table):
    return {c["name"]: c for c in sa.inspect(connection).get_columns(table)}


def test_encrypted_columns_added_and_plain_columns_kept(database):
    connection, _metadata, _config = database
    travelers, people, documents = (columns(connection, t) for t in ("booking_travelers", "saved_people", "saved_person_documents"))
    assert {"date_of_birth_enc", "document_number_enc"} <= set(travelers)
    assert {"date_of_birth_enc", "date_of_birth_hmac"} <= set(people)
    assert {"document_number_enc", "document_number_hmac"} <= set(documents)
    # Plain columns stay (read until the backfill runs) and may now be cleared once a value is encrypted.
    assert travelers["date_of_birth"]["nullable"] and documents["document_number"]["nullable"]
    assert documents["document_number_normalized"]["nullable"]


def test_a_row_must_hold_its_value_one_way_or_the_other(database):
    connection, _metadata, _config = database
    person = "p-" + "1" * 10
    connection.execute(sa.text("INSERT INTO saved_people (id, first_name, last_name) VALUES (:id, 'A', 'B')"), {"id": person})
    insert = ("INSERT INTO saved_person_documents (id, person_id, id_type, document_country, document_number, document_number_normalized,"
              " document_number_enc, document_number_hmac) VALUES (:id, :p, 'PSPT', 'PAK', :n, :nn, :enc, :h)")
    connection.execute(sa.text(insert), {"id": "d1", "p": person, "n": "AB1234567", "nn": "AB1234567", "enc": None, "h": None})
    connection.execute(sa.text(insert), {"id": "d2", "p": person, "n": None, "nn": None, "enc": "k1:xxx", "h": "a" * 64})
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(sa.text(insert), {"id": "d3", "p": person, "n": None, "nn": None, "enc": None, "h": None})
    # The hashed match key is unique per owner like the plain one.
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(sa.text(insert), {"id": "d4", "p": person, "n": None, "nn": None, "enc": "k1:yyy", "h": "a" * 64})


def test_round_trip_keeps_existing_plain_rows(database):
    connection, _metadata, config = database
    connection.execute(sa.text("INSERT INTO saved_people (id, first_name, last_name, date_of_birth) VALUES ('p2', 'C', 'D', '1990-01-02')"))
    command.downgrade(config, "0034_payment_account_logo")
    assert "date_of_birth_enc" not in columns(connection, "saved_people")
    command.upgrade(config, "head")
    assert connection.scalar(sa.text("SELECT date_of_birth FROM saved_people WHERE id = 'p2'")).isoformat() == "1990-01-02"
