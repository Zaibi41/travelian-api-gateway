"""Finance > Accounts & Ledgers, "Bank and cash book": list, add and deactivate receiving accounts
(/v1/admin/finance/payment-accounts). Same store and validation as app.cli.payment_accounts."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.routes.payment_accounts import finance_router, router
from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.db import booking_payments
from tests.database.test_booking_payments import AT
from tests.database.test_booking_people import ACTOR, save

ROLE_IDS = {"customer": 1, "agency_owner": 2, "super_admin": 3, "staff": 4}
PATH = "/v1/admin/finance/payment-accounts"


def client_as(connection, role, grants=()):
    """A test client whose caller is a real user with this role (the real role check runs)."""
    subject = f"sub-{uuid.uuid4().hex}"
    user_id = str(uuid.uuid4())
    connection.execute(text("INSERT INTO users (id, cognito_issuer, cognito_sub, role_id) VALUES (:id, 'https://issuer.test', :sub, :role)"),
                       {"id": user_id, "sub": subject, "role": ROLE_IDS[role]})
    for permission in grants:   # granted to the role (staff), as role_permissions holds them
        connection.execute(text("INSERT INTO role_permissions (role_id, permission) VALUES (:r, :p) ON CONFLICT DO NOTHING"),
                           {"r": ROLE_IDS[role], "p": permission})
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.include_router(finance_router, prefix="/v1")
    identity = AuthenticatedUser(subject=subject, issuer="https://issuer.test", client_id="portal")
    app.dependency_overrides[require_authenticated_user] = lambda: identity
    return TestClient(app), user_id


@pytest.fixture
def admin(database):
    connection, _, _ = database
    client, user_id = client_as(connection, "super_admin")
    return connection, client, user_id


def test_super_admin_adds_an_account_and_sees_it_listed(admin):
    connection, client, user_id = admin
    response = client.post(PATH, json={"method": "bank_transfer", "label": "  Meezan Bank,   Travelian current account "})
    assert response.status_code == 201
    created = response.json()
    assert created["label"] == "Meezan Bank, Travelian current account" and created["active"] is True
    assert set(created) == {"id", "method", "label", "active", "bank_name", "account_title", "account_number_masked", "account_number", "iban", "qr_asset_key", "qr_url", "logo_asset_key", "logo_url"}
    assert client.get(PATH).json()["accounts"] == [created]
    assert connection.execute(text("SELECT created_by_user_id FROM payment_accounts")).scalar() == user_id


@pytest.mark.parametrize("label", ["a", "  a  ", "x" * 121, ""])
def test_name_must_be_2_to_120_characters(admin, label):
    _, client, _ = admin
    response = client.post(PATH, json={"method": "jazzcash", "label": label})
    assert response.status_code == 422
    assert response.json()["detail"] == "The account name must be 2 to 120 characters."
    assert client.get(PATH).json()["accounts"] == []


def test_120_characters_is_accepted(admin):
    _, client, _ = admin
    assert client.post(PATH, json={"method": "1link", "label": "x" * 120}).status_code == 201


def test_a_duplicate_active_name_and_method_is_refused(admin):
    _, client, _ = admin
    assert client.post(PATH, json={"method": "bank_transfer", "label": "HBL collections"}).status_code == 201
    response = client.post(PATH, json={"method": "bank_transfer", "label": "hbl  COLLECTIONS"})
    assert response.status_code == 409
    assert response.json()["detail"] == "An active account with this name and method already exists."
    assert client.post(PATH, json={"method": "jazzcash", "label": "HBL collections"}).status_code == 201   # another method: allowed


def test_the_same_name_is_allowed_after_the_first_is_deactivated(admin):
    _, client, _ = admin
    first = client.post(PATH, json={"method": "bank_transfer", "label": "HBL collections"}).json()
    deactivated = client.post(f"{PATH}/{first['id']}/deactivate")
    assert deactivated.status_code == 200 and deactivated.json() == {**first, "active": False}
    second = client.post(PATH, json={"method": "bank_transfer", "label": "HBL collections"})
    assert second.status_code == 201 and second.json()["id"] != first["id"]
    listed = client.get(PATH).json()["accounts"]
    assert sorted((a["label"], a["active"]) for a in listed) == [("HBL collections", False), ("HBL collections", True)]


def test_deactivating_twice_or_an_unknown_id_is_404(admin):
    _, client, _ = admin
    account = client.post(PATH, json={"method": "jazzcash", "label": "JazzCash business wallet"}).json()
    assert client.post(f"{PATH}/{account['id']}/deactivate").status_code == 200
    again = client.post(f"{PATH}/{account['id']}/deactivate")
    assert again.status_code == 404 and again.json()["detail"] == "No active account with that id."
    assert client.post(f"{PATH}/{uuid.uuid4()}/deactivate").status_code == 404


def test_deactivating_keeps_past_payments_and_stops_new_ones(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    client, _ = client_as(connection, "super_admin")
    account = client.post(PATH, json={"method": "bank_transfer", "label": "Meezan Bank current account"}).json()
    booking_payments.record(reference, amount_minor=10_000, currency="PKR", method="bank_transfer", account_id=account["id"],
                            payer_reference="TXN-1", received_at=AT, actor_user_id=ACTOR)
    assert client.post(f"{PATH}/{account['id']}/deactivate").status_code == 200

    row = connection.execute(text("SELECT account_id, received_into FROM booking_payments")).one()
    assert tuple(row) == (account["id"], "Meezan Bank current account")          # the past payment keeps the account
    assert connection.execute(text("SELECT count(*) FROM payment_accounts")).scalar() == 1   # never deleted
    assert client.get("/v1/admin/payment-accounts").json()["accounts"] == []     # not offered on the Payments tab
    with pytest.raises(booking_payments.PaymentRefused) as error:
        booking_payments.record(reference, amount_minor=10_000, currency="PKR", method="bank_transfer", account_id=account["id"],
                                payer_reference="TXN-2", received_at=AT, actor_user_id=ACTOR)
    assert error.value.status == 422


@pytest.mark.parametrize("role, grants", [
    ("staff", ()),
    ("staff", ("record_payments", "see_cost_and_margin", "see_supplier_names")),   # grants don't open it
    ("agency_owner", ()),
    ("customer", ()),
])
def test_other_roles_get_403_on_every_finance_route(database, role, grants):
    connection, _, _ = database
    admin_client, _ = client_as(connection, "super_admin")
    account = admin_client.post(PATH, json={"method": "bank_transfer", "label": "Meezan Bank current account"}).json()
    client, _ = client_as(connection, role, grants)
    assert client.get(PATH).status_code == 403
    assert client.post(PATH, json={"method": "bank_transfer", "label": "Another account"}).status_code == 403
    assert client.post(f"{PATH}/{account['id']}/deactivate").status_code == 403
    listed = admin_client.get(PATH).json()["accounts"]
    assert [(a["label"], a["active"]) for a in listed] == [("Meezan Bank current account", True)]   # nothing changed


def test_staff_still_read_the_active_list_for_the_payments_tab(database):
    connection, _, _ = database
    admin_client, _ = client_as(connection, "super_admin")
    admin_client.post(PATH, json={"method": "jazzcash", "label": "JazzCash business wallet"})
    staff, _ = client_as(connection, "staff")
    assert [a["label"] for a in staff.get("/v1/admin/payment-accounts").json()["accounts"]] == ["JazzCash business wallet"]


def test_qr_image_is_saved_returned_replaced_and_removed(database):
    """Add a bank account with an uploaded QR, read it back (admin list and checkout), replace it, then remove it."""
    from unittest.mock import patch

    from app.core.config import settings
    from app.services import public_booking

    connection, _, _ = database
    client, _ = client_as(connection, "super_admin")
    base = "https://travelian-assets-test.s3.ap-southeast-1.amazonaws.com"
    with patch.object(settings, "assets_public_base_url", base), patch.object(settings, "bank_transfer_enabled", True):
        created = client.post(PATH, json={"method": "bank_transfer", "label": "Meezan QR", "account_title": "Travelian (Private) Limited",
                                          "account_number": "0101-0105555555", "qr_asset_key": "payment-qr/first.png"})
        assert created.status_code == 201, created.text
        account_id = created.json()["id"]
        assert created.json()["qr_url"] == f"{base}/payment-qr/first.png"
        listed = next(a for a in client.get(PATH).json()["accounts"] if a["id"] == account_id)
        assert listed["qr_url"] == f"{base}/payment-qr/first.png"  # shown again when the account is reopened
        checkout = [a.qr_url for a in public_booking.bank_accounts() if a.account_title == "Travelian (Private) Limited"]
        assert checkout == [f"{base}/payment-qr/first.png"]  # and at checkout

        replaced = client.patch(f"{PATH}/{account_id}", json={"qr_asset_key": "payment-qr/second.webp"})
        assert replaced.json()["qr_url"] == f"{base}/payment-qr/second.webp"
        kept = client.patch(f"{PATH}/{account_id}", json={"bank_name": "Meezan Bank"})
        assert kept.json()["qr_asset_key"] == "payment-qr/second.webp"  # untouched when left out
        removed = client.patch(f"{PATH}/{account_id}", json={"remove_qr": True})
        assert removed.json()["qr_asset_key"] is None and removed.json()["qr_url"] is None

        # A key that isn't an uploaded QR (never signed for this purpose) is refused; nothing is saved.
        bad = client.post(PATH, json={"method": "bank_transfer", "label": "Broken QR", "account_title": "X Ltd",
                                      "account_number": "0101-0105555556", "qr_asset_key": "site/logo.png"})
        assert bad.status_code == 422 and "Upload the QR image first" in bad.json()["detail"]
        assert not any(a["label"] == "Broken QR" for a in client.get(PATH).json()["accounts"])


def test_bank_logo_is_saved_shown_at_checkout_replaced_and_removed(database):
    """travelian#76 (option 2): a logo upload per receiving account; checkout gets its URL; until then, none (initials)."""
    from unittest.mock import patch

    from app.core.config import settings
    from app.services import public_booking

    connection, _, _ = database
    client, _ = client_as(connection, "super_admin")
    base = "https://travelian-assets-test.s3.ap-southeast-1.amazonaws.com"
    with patch.object(settings, "assets_public_base_url", base), patch.object(settings, "bank_transfer_enabled", True):
        created = client.post(PATH, json={"method": "bank_transfer", "label": "HBL main", "account_title": "Travelian (Private) Limited",
                                          "bank_name": "Habib Bank Limited (HBL)", "account_number": "0101-0107777777"})
        assert created.status_code == 201 and created.json()["logo_url"] is None
        account_id = created.json()["id"]
        checkout = [a.logo_url for a in public_booking.bank_accounts() if a.id == account_id]
        assert checkout == [None]  # no logo yet: the website shows the bank's initials
        added = client.patch(f"{PATH}/{account_id}", json={"logo_asset_key": "site/bank-logo/hbl.png"})
        assert added.json()["logo_url"] == f"{base}/site/bank-logo/hbl.png"
        assert [a.logo_url for a in public_booking.bank_accounts() if a.id == account_id] == [f"{base}/site/bank-logo/hbl.png"]
        kept = client.patch(f"{PATH}/{account_id}", json={"qr_asset_key": "payment-qr/hbl.png"})
        assert kept.json()["logo_asset_key"] == "site/bank-logo/hbl.png" and kept.json()["qr_asset_key"] == "payment-qr/hbl.png"
        removed = client.patch(f"{PATH}/{account_id}", json={"remove_logo": True})
        assert removed.json()["logo_asset_key"] is None and removed.json()["qr_asset_key"] == "payment-qr/hbl.png"
        bad = client.patch(f"{PATH}/{account_id}", json={"logo_asset_key": "payment-qr/hbl.png"})
        assert bad.status_code == 422 and "Upload the bank logo first" in bad.json()["detail"]
