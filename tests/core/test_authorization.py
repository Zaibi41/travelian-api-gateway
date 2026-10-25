"""Policy tests use explicitly synthetic identities; production defaults deny."""

import pytest
from fastapi import HTTPException

from app.core.authentication import AuthenticatedUser
from app.core.authorization import (
    BookingOwner,
    BookingPermission,
    authorize_agency_operation,
    authorize_owned_booking,
)
from app.core.config import settings


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setattr(settings, "cognito_agency_claim", "custom:agency_id")
    monkeypatch.setattr(settings, "booking_create_groups", ["test-bookers"])
    monkeypatch.setattr(settings, "booking_details_groups", ["test-readers"])


def user(**changes):
    return AuthenticatedUser(
        **{
            "issuer": "https://issuer.invalid",
            "subject": "test-user",
            "client_id": "testclient",
            "groups": ("test-bookers",),
            "agency_id": "agency-1",
            "agency_claim": "custom:agency_id",
            **changes,
        }
    )


def test_explicit_create_policy_produces_agency_scope(policy):
    result = authorize_agency_operation(user(), BookingPermission.CREATE)
    assert result.subject == "test-user"
    assert result.agency_id == "agency-1"
    assert result.permission == BookingPermission.CREATE


def test_create_permission_does_not_imply_read_permission(policy):
    with pytest.raises(HTTPException) as error:
        authorize_agency_operation(user(), BookingPermission.DETAILS)
    assert error.value.status_code == 403
    assert authorize_agency_operation(
        user(groups=("test-readers",)), BookingPermission.DETAILS
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"groups": ()},
        {"groups": ("admin",)},
        {"groups": ("customer",)},
        {"groups": ("TEST-BOOKERS",)},
        {"agency_id": None},
        {"agency_id": ""},
        {"agency_id": "agency 1"},
        {"agency_id": "../agency-1"},
        {"agency_claim": None},
        {"agency_claim": "custom:unapproved"},
    ],
)
def test_no_implicit_role_or_agency_permission(policy, changes):
    with pytest.raises(HTTPException) as error:
        authorize_agency_operation(user(**changes), BookingPermission.CREATE)
    assert error.value.status_code == 403
    assert error.value.detail == "Booking access denied."


@pytest.mark.parametrize(
    "name,value",
    [
        ("cognito_agency_claim", None),
        ("cognito_agency_claim", "sub"),
        ("booking_create_groups", []),
        ("booking_create_groups", [""]),
        ("booking_create_groups", [" test-bookers"]),
    ],
)
def test_missing_invalid_policy_fails_closed(policy, monkeypatch, name, value):
    monkeypatch.setattr(settings, name, value)
    with pytest.raises(HTTPException) as error:
        authorize_agency_operation(user(), BookingPermission.CREATE)
    assert error.value.status_code == 503


def test_internal_claims_not_exposed_by_identity_model(policy):
    assert set(user().model_dump()) == {"issuer", "subject", "client_id"}


@pytest.mark.parametrize(
    "changes",
    [
        {"agency_id": "agency-2"},
        {"subject": "other-user"},
        {"issuer": "https://other.invalid"},
        None,
    ],
)
def test_cross_agency_other_user_and_unknown_owner_denied(policy, changes):
    principal = authorize_agency_operation(
        user(groups=("test-readers",)), BookingPermission.DETAILS
    )
    owner = (
        None
        if changes is None
        else BookingOwner(
            **{
                "issuer": principal.issuer,
                "subject": principal.subject,
                "agency_id": principal.agency_id,
                **changes,
            }
        )
    )
    with pytest.raises(HTTPException) as error:
        authorize_owned_booking(principal, owner)
    assert error.value.status_code == 404
    assert error.value.detail == "Booking not found."


def test_exact_owner_matches(policy):
    principal = authorize_agency_operation(
        user(groups=("test-readers",)), BookingPermission.DETAILS
    )
    owner = BookingOwner(
        issuer=principal.issuer,
        subject=principal.subject,
        agency_id=principal.agency_id,
    )
    authorize_owned_booking(principal, owner)


def test_creation_entitlement_cannot_be_used_as_details_permission(policy):
    principal = authorize_agency_operation(user(), BookingPermission.CREATE)
    owner = BookingOwner(
        issuer=principal.issuer,
        subject=principal.subject,
        agency_id=principal.agency_id,
    )
    with pytest.raises(HTTPException) as error:
        authorize_owned_booking(principal, owner)
    assert error.value.status_code == 403
