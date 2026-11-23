"""gateway#84: passport / ID numbers and dates of birth encrypted at rest once the identity keys are set, on the real
database. Without keys nothing changes (plain, as before); with keys new values are stored encrypted, plain rows keep
working until the backfill CLI encrypts them, matching still works, and no value reaches a log. Synthetic people only."""

import base64
import logging
from datetime import date

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.cli import encrypt_identity
from app.core import pii
from app.core.config import settings
from app.db import booking_people, saved_people as store
from tests.database.test_booking_people import save
from tests.database.test_saved_people import ACTOR, cnic, passport, person

K1, K2, H = (base64.b64encode(bytes([n]) * 32).decode() for n in (1, 2, 3))
SECRET_VALUES = ("AB1234567", "35202-1234567-1", "3520212345671", "1990-01-31", "1990, 1, 31")


@pytest.fixture
def db(database):
    connection, metadata, _ = database
    connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    return connection, metadata


def keys_on(monkeypatch, keys=f"k1:{K1}", active="k1"):
    monkeypatch.setattr(settings, "pii_keys", SecretStr(keys))
    monkeypatch.setattr(settings, "pii_active_key", active)
    monkeypatch.setattr(settings, "pii_hash_key", SecretStr(H))


class Engine:
    """The CLI's engine on the test connection (each batch a savepoint)."""

    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return _Nested(self.connection)


class _Nested:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.tx = self.connection.begin_nested()
        return self.connection

    def __exit__(self, *exc):
        self.tx.__exit__(*exc)


def rows(connection, sql):
    return [dict(r) for r in connection.execute(text(sql)).mappings().all()]


def test_without_keys_everything_stays_plain(db):
    connection, metadata = db
    assert not pii.encryption_on()
    person(document=passport())
    save(connection, metadata)
    [p] = rows(connection, "SELECT date_of_birth, date_of_birth_enc, date_of_birth_hmac FROM saved_people")
    assert p == {"date_of_birth": date(1990, 1, 31), "date_of_birth_enc": None, "date_of_birth_hmac": None}
    [d] = rows(connection, "SELECT document_number, document_number_enc, document_number_normalized, document_number_hmac FROM saved_person_documents")
    assert d == {"document_number": "ab1234567", "document_number_enc": None, "document_number_normalized": "AB1234567", "document_number_hmac": None}


def test_with_keys_new_values_are_stored_encrypted_and_read_back(db, monkeypatch):
    connection, metadata = db
    keys_on(monkeypatch)
    saved, outcome = person(document=passport())
    assert outcome == "created" and saved.date_of_birth == date(1990, 1, 31) and saved.documents[0].document_number == "ab1234567"
    save(connection, metadata)
    dump = str(rows(connection, "SELECT * FROM saved_people")) + str(rows(connection, "SELECT * FROM saved_person_documents")) \
        + str(rows(connection, "SELECT * FROM booking_travelers"))
    for value in SECRET_VALUES + ("ab1234567", "1990-01-31", "datetime.date(1990, 1, 31)"):
        assert value not in dump, value  # nothing readable in the database
    [p] = rows(connection, "SELECT date_of_birth, date_of_birth_enc, date_of_birth_hmac FROM saved_people")
    assert p["date_of_birth"] is None and p["date_of_birth_enc"].startswith("k1:") and len(p["date_of_birth_hmac"]) == 64
    travelers = booking_people.read(connection.execute(text("SELECT reference FROM bookings")).scalar()).travelers
    assert (travelers[0].date_of_birth, travelers[0].document_number) == (date(1990, 1, 31), "AB1234567")
    assert (travelers[1].date_of_birth, travelers[1].document_number) == (date(2025, 6, 1), None)


def test_matching_works_on_encrypted_and_still_plain_rows(db, monkeypatch):
    plain, _ = person(first="Plain", last="Row", document=cnic())   # saved before the keys were set
    keys_on(monkeypatch)
    sealed, _ = person(first="Sealed", last="Row", dob=date(1985, 5, 5), document=passport())
    # Exact document search finds both kinds; the CNIC also without dashes.
    assert [p.id for p in store.search(None, "3520212345671")] == [plain.id]
    assert [p.id for p in store.search(None, "AB 1234567")] == [sealed.id]
    assert [p.id for p in store.typeahead(None, "AB1234567", "traveler")] == [sealed.id]
    # The same document for someone else is refused, whichever way it is stored.
    for document in (cnic(), passport()):
        with pytest.raises(store.PeopleError) as refused:
            person(first="Someone", last="Else", document=document)
        assert refused.value.code == "document_belongs_to_another_person"
    # Same name and date of birth without a document: a possible duplicate, for both kinds.
    for first, last, dob in (("Plain", "Row", date(1990, 1, 31)), ("Sealed", "Row", date(1985, 5, 5))):
        with pytest.raises(store.PeopleError) as duplicate:
            person(first=first, last=last, dob=dob)
        assert duplicate.value.code == "possible_duplicate"
    # Editing a person and renewing a document keep them encrypted.
    store.update(None, ACTOR, plain.id, {"date_of_birth": date(1990, 2, 1)})
    store.edit_document(None, ACTOR, sealed.id, sealed.documents[0].id, passport(expiry=date(2033, 1, 1)))
    with store.agencies.application_engine().begin() as connection:
        assert store.load(connection, None, plain.id).date_of_birth == date(1990, 2, 1)
        assert store.load(connection, None, sealed.id).documents[0].document_expiry == date(2033, 1, 1)
    assert db[0].scalar(text("SELECT count(*) FROM saved_people WHERE date_of_birth IS NOT NULL AND id = :id"), {"id": plain.id}) == 0


def test_backfill_dry_run_apply_rotate_and_decrypt(db, monkeypatch, capsys, caplog):
    connection, metadata = db
    caplog.set_level(logging.DEBUG)
    person(document=cnic())
    person(first="Other", last="Person", dob=None, document=passport())
    save(connection, metadata)
    engine = Engine(connection)
    monkeypatch.setattr(encrypt_identity.agencies, "application_engine", lambda: engine)

    # No keys: a dry run counts, --apply refuses.
    assert encrypt_identity.main([]) == 0 and "Rows to change: booking_travelers: 2, saved_people: 1, saved_person_documents: 2" in capsys.readouterr().out
    assert encrypt_identity.main(["--apply"]) == 2

    keys_on(monkeypatch)
    assert encrypt_identity.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "Rows changed: booking_travelers: 2, saved_people: 1, saved_person_documents: 2" in out
    assert connection.scalar(text("SELECT count(*) FROM saved_person_documents WHERE document_number IS NOT NULL OR document_number_normalized IS NOT NULL")) == 0
    assert connection.scalar(text("SELECT count(*) FROM booking_travelers WHERE date_of_birth IS NOT NULL OR document_number IS NOT NULL")) == 0
    # Values read back unchanged; matching on the hashed keys.
    assert [d.document_number for p in store.search(None, "35202-1234567-1") for d in p.documents] == ["35202-1234567-1"]
    assert booking_people.read(connection.execute(text("SELECT reference FROM bookings")).scalar()).travelers[0].document_number == "AB1234567"
    # Idempotent.
    assert encrypt_identity.main(["--apply"]) == 0
    assert "Rows changed: booking_travelers: 0, saved_people: 0, saved_person_documents: 0" in capsys.readouterr().out

    # Key rotation: a new active key re-encrypts everything; the old key stays listed until then.
    keys_on(monkeypatch, keys=f"k1:{K1},k2:{K2}", active="k2")
    assert encrypt_identity.main(["--apply"]) == 0
    assert connection.scalar(text("SELECT count(*) FROM booking_travelers WHERE split_part(date_of_birth_enc, ':', 1) <> 'k2'")) == 0
    keys_on(monkeypatch, keys=f"k2:{K2}", active="k2")  # k1 retired
    assert store.search(None, "AB1234567")[0].documents[0].document_number == "ab1234567"

    # And back to plain (before a downgrade).
    assert encrypt_identity.main(["--decrypt", "--apply"]) == 0
    [d] = rows(connection, "SELECT document_number, document_number_normalized, document_number_enc, document_number_hmac"
                           " FROM saved_person_documents WHERE id_type = 'NATI'")
    assert d == {"document_number": "35202-1234567-1", "document_number_normalized": "3520212345671", "document_number_enc": None, "document_number_hmac": None}
    assert connection.scalar(text("SELECT date_of_birth FROM booking_travelers WHERE position = 0")) == date(1990, 1, 31)

    printed = capsys.readouterr().out + "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for value in SECRET_VALUES + ("ab1234567",):
        assert value not in printed, value


def test_a_value_moved_to_another_column_or_with_a_missing_key_does_not_open(db, monkeypatch):
    keys_on(monkeypatch)
    sealed = pii.seal("AB1234567", "booking_travelers.document_number")
    with pytest.raises(pii.PiiDecryptError):
        pii.unseal(sealed, "saved_person_documents.document_number")
    keys_on(monkeypatch, keys=f"k2:{K2}", active="k2")
    with pytest.raises(pii.PiiDecryptError):
        pii.unseal(sealed, "booking_travelers.document_number")


def test_incomplete_keys_refuse_to_write_rather_than_store_plain(db, monkeypatch):
    monkeypatch.setattr(settings, "pii_keys", SecretStr(f"k1:{K1}"))
    monkeypatch.setattr(settings, "pii_active_key", "k1")
    monkeypatch.setattr(settings, "pii_hash_key", None)
    with pytest.raises(pii.PiiNotConfigured) as error:
        person(document=passport())
    assert K1 not in str(error.value)
    assert not str(rows(db[0], "SELECT * FROM saved_people"))  .count("1990")
