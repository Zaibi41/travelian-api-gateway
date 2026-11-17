import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import text

from app.schemas.agencies import AgencyUpdate
from app.schemas.agency_applications import AgencyApplicationRequest

CONTACTS = {
    "contact_name": "Synthetic Person",
    "contact_email": "office@example.com",
    "contact_phone": "+923001234567",
}


@pytest.mark.parametrize(
    "field,value",
    [
        ("contact_name", "  "),
        ("contact_name", "Name\nInjected"),
        ("contact_name", "x" * 201),
        ("contact_email", "invalid"),
        ("contact_email", "Name <office@example.com>"),
        ("contact_phone", "03001234567"),
        ("contact_phone", "+00"),
        ("contact_phone", "+92 3001234567"),
        ("contact_phone", "+1234567890123456"),
        ("contact_phone", 923001234567),
    ],
)
def test_invalid_contacts_rejected(field, value):
    with pytest.raises(ValidationError):
        AgencyApplicationRequest(agency_name="Synthetic", **{**CONTACTS, field: value})


@pytest.mark.parametrize("field", list(CONTACTS))
def test_registration_requires_contacts(field):
    contacts = {key: value for key, value in CONTACTS.items() if key != field}
    with pytest.raises(ValidationError):
        AgencyApplicationRequest(agency_name="Synthetic", **contacts)


@pytest.mark.parametrize(
    "body",
    [
        {"expected_revision": 1},
        {"expected_revision": 1, "contact_name": None},
        {"expected_revision": 1, "contact_email": None},
    ],
)
def test_empty_or_null_patch_rejected(body):
    with pytest.raises(ValidationError):
        AgencyUpdate(**body)


def test_contact_migration_preserves_existing_agencies(database):
    connection, _, config = database
    command.downgrade(config, "0004_agency_integrations")
    connection.execute(
        text("INSERT INTO agencies (id,name) VALUES ('agency-legacy','Legacy')")
    )
    command.upgrade(config, "head")
    row = (
        connection.execute(
            text(
                "SELECT id,name,contact_name,contact_email,contact_phone FROM agencies WHERE id='agency-legacy'"
            )
        )
        .mappings()
        .one()
    )
    assert dict(row) == {
        "id": "agency-legacy",
        "name": "Legacy",
        "contact_name": None,
        "contact_email": None,
        "contact_phone": None,
    }


def test_unicode_names_and_contact_email_normalization():
    parsed = AgencyApplicationRequest(
        agency_name="Synthetic",
        **{
            **CONTACTS,
            "contact_name": "  محمد علی  ",
            "contact_email": "office@EXAMPLE.COM",
        },
    )
    assert parsed.contact_name == "محمد علی"
    assert parsed.contact_email == "office@example.com"
