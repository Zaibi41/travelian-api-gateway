"""Real RSA signatures with local keys and mocked JWKS HTTP; no real tokens."""

import asyncio
import io
import json
import time
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import URLError

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from jwt.algorithms import RSAAlgorithm

from app.api.routes.group_ticketing import router as group_router
from app.api.routes.identity import router as identity_router
from app.core.authentication import get_verifier
from app.core.config import settings

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL"
CLIENT = "syntheticclient123"


@pytest.fixture(scope="module")
def keys():
    return [
        rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)
    ]


@pytest.fixture
def auth(monkeypatch, keys):
    monkeypatch.setattr(settings, "cognito_issuer", ISSUER)
    monkeypatch.setattr(settings, "cognito_app_client_id", CLIENT)
    monkeypatch.setattr(settings, "cognito_agency_claim", None)
    monkeypatch.setattr(settings, "booking_create_groups", [])
    monkeypatch.setattr(settings, "booking_details_groups", [])
    monkeypatch.setattr(
        "app.core.booking_security.require_active_assignment",
        lambda principal: principal,
    )
    from app.core.authorization import authorize_agency_operation

    monkeypatch.setattr(
        "app.core.booking_security.authorize_agency_operation",
        authorize_agency_operation,
    )
    monkeypatch.setattr(
        "app.api.routes.identity.local_identity",
        lambda identity: {
            "subject": identity.subject,
            "issuer": identity.issuer,
            "client_id": identity.client_id,
            "user_id": "11111111-1111-4111-8111-111111111111",
            "role_id": 1,
            "role": "customer",
        },
    )
    get_verifier.cache_clear()
    keyset = {
        "keys": [
            dict(
                json.loads(RSAAlgorithm.to_jwk(keys[0].public_key())),
                kid="test-key",
                alg="RS256",
                use="sig",
            )
        ]
    }

    def open_document(request, timeout):
        assert request.full_url == ISSUER + "/.well-known/jwks.json"
        assert not request.has_header("Authorization")
        assert timeout == 3
        return io.BytesIO(json.dumps(keyset).encode())

    opener = Mock()
    opener.open.side_effect = open_document
    monkeypatch.setattr(
        "jwt.jwks_client.urllib.request.build_opener", lambda *args: opener
    )

    def token(changes=None, remove=(), key=None, headers=None):
        claims = {
            "iss": ISSUER,
            "sub": "synthetic-user",
            "client_id": CLIENT,
            "token_use": "access",
            "iat": int(time.time()) - 10,
            "exp": int(time.time()) + 300,
        }
        claims.update(changes or {})
        for field in remove:
            claims.pop(field, None)
        return jwt.encode(
            claims,
            key or keys[0],
            algorithm="RS256",
            headers=headers or {"kid": "test-key"},
        )

    yield get_verifier(ISSUER, CLIENT), token, opener, keyset
    get_verifier.cache_clear()


def test_valid_token_and_jwks_cache(auth):
    verifier, token, opener, _ = auth
    signed = token()
    assert verifier.verify(signed).subject == "synthetic-user"
    assert verifier.verify(signed).client_id == CLIENT
    assert opener.open.call_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://wrong.invalid"},
        {"client_id": "wrongclient"},
        {"token_use": "id"},
        {"token_use": "refresh"},
        {"exp": 1},
        {"iat": int(time.time()) + 3600},
        {"nbf": int(time.time()) + 3600},
        {"sub": ""},
        {"sub": " "},
        {"sub": 123},
        {"sub": CLIENT},
        {"exp": str(int(time.time()) + 300)},
        {"iat": True},
    ],
)
def test_invalid_claims_rejected(auth, changes):
    verifier, token, _, _ = auth
    with pytest.raises(HTTPException) as error:
        verifier.verify(token(changes))
    assert error.value.status_code == 401
    assert error.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize(
    "field", ["iss", "sub", "client_id", "token_use", "iat", "exp"]
)
def test_required_claims(auth, field):
    verifier, token, _, _ = auth
    with pytest.raises(HTTPException) as error:
        verifier.verify(token(remove=(field,)))
    assert error.value.status_code == 401


def test_signature_algorithm_and_unknown_key(auth, keys):
    verifier, token, _, _ = auth
    invalid = [
        token(key=keys[1]),
        token(headers={"kid": "unknown-key"}),
        jwt.encode(
            {"sub": "forged"},
            "synthetic-secret-that-is-at-least-32-bytes-long",
            algorithm="HS256",
        ),
        jwt.encode({"sub": "forged"}, None, algorithm="none"),
        "malformed-token",
        "X" * 16385,
    ]
    for value in invalid:
        with pytest.raises(HTTPException) as error:
            verifier.verify(value)
        assert error.value.status_code == 401
        assert value not in error.value.detail


def test_signing_key_rotation(auth, keys):
    verifier, token, opener, keyset = auth
    verifier.verify(token())
    keyset["keys"] = [
        dict(
            json.loads(RSAAlgorithm.to_jwk(keys[1].public_key())),
            kid="rotated-key",
            alg="RS256",
            use="sig",
        )
    ]
    # Simulate rotation after the library's unknown-kid refresh cooldown.
    verifier.keys._last_successful_fetch -= 31
    result = verifier.verify(token(key=keys[1], headers={"kid": "rotated-key"}))
    assert result.subject == "synthetic-user"
    assert opener.open.call_count == 2


def test_jwks_outage_fails_closed_without_exception_details(auth):
    verifier, token, opener, _ = auth
    opener.open.side_effect = URLError("private-test-diagnostics")
    with pytest.raises(HTTPException) as error:
        verifier.verify(token())
    assert error.value.status_code == 503
    assert "private-test-diagnostics" not in error.value.detail


def test_invalid_jwks_is_unavailable(auth):
    verifier, token, _, keyset = auth
    keyset["keys"] = []
    with pytest.raises(HTTPException) as error:
        verifier.verify(token())
    assert error.value.status_code == 503


def call(path, headers=None, method="GET", body=None):
    app = FastAPI()
    app.include_router(identity_router, prefix="/v1")
    app.include_router(group_router, prefix="/v1")

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.invalid"
        ) as client:
            return await client.request(method, path, headers=headers, json=body)

    return asyncio.run(request())


def test_identity_route_returns_only_verified_identity(auth):
    _, token, _, _ = auth
    response = call("/v1/auth/me", {"Authorization": "Bearer " + token()})
    assert response.status_code == 200
    assert response.json() == {
        "subject": "synthetic-user",
        "issuer": ISSUER,
        "client_id": CLIENT,
        "user_id": "11111111-1111-4111-8111-111111111111",
        "role_id": 1,
        "role": "customer",
        "agency_id": None,
        "agency_status": None,
        "application_status": None,
    }
    assert "token" not in response.text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic invalid"}, {"Authorization": "Bearer invalid"}],
)
def test_identity_route_requires_bearer(auth, headers):
    response = call("/v1/auth/me", headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "issuer,client",
    [
        (None, None),
        (ISSUER, None),
        (None, CLIENT),
        ("https://attacker.invalid", CLIENT),
    ],
)
def test_missing_or_invalid_configuration_is_unavailable(monkeypatch, issuer, client):
    monkeypatch.setattr(settings, "cognito_issuer", issuer)
    monkeypatch.setattr(settings, "cognito_app_client_id", client)
    with patch("jwt.jwks_client.urllib.request.build_opener") as network:
        assert call("/v1/auth/me").status_code == 503
        network.assert_not_called()


@pytest.mark.parametrize("endpoint", ["bookings", "booking-details"])
def test_valid_identity_cannot_bypass_booking_authorization(auth, endpoint):
    _, token, _, _ = auth
    with patch(
        "app.api.routes.group_ticketing._authenticated_dhillu_sahi_client",
        new_callable=AsyncMock,
    ) as supplier:
        response = call(
            f"/v1/group-ticketing/{endpoint}",
            {"Authorization": "Bearer " + token()},
            "POST",
            {"booking_id": 123},
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Booking authorization is not configured."}
    supplier.assert_not_called()


@pytest.mark.parametrize("endpoint", ["bookings", "booking-details"])
def test_invalid_identity_cannot_reach_booking_supplier(auth, endpoint):
    with patch(
        "app.api.routes.group_ticketing._authenticated_dhillu_sahi_client",
        new_callable=AsyncMock,
    ) as supplier:
        response = call(
            f"/v1/group-ticketing/{endpoint}",
            {"Authorization": "Bearer invalid"},
            "POST",
            {"booking_id": 123},
        )
    assert response.status_code == 401
    supplier.assert_not_called()


def test_token_cannot_choose_signing_key_url(auth):
    verifier, token, opener, _ = auth
    identity = verifier.verify(
        token(headers={"kid": "test-key", "jku": "https://attacker.invalid/keys"})
    )
    assert identity.subject == "synthetic-user"
    assert opener.open.call_count == 1  # Fixture asserts only configured pool URL.


def test_missing_key_id_is_rejected_without_network(auth):
    verifier, token, opener, _ = auth
    with pytest.raises(HTTPException) as error:
        verifier.verify(token(headers={"typ": "JWT"}))
    assert error.value.status_code == 401
    opener.open.assert_not_called()


def test_duplicate_authorization_headers_are_rejected(auth):
    _, token, _, _ = auth
    signed = "Bearer " + token()
    response = call(
        "/v1/auth/me", [("Authorization", signed), ("Authorization", signed)]
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "claim,groups",
    [
        ("custom:agency_id", ["test-bookers"]),
        (None, "admin"),
        (None, [123]),
        (None, [""]),
    ],
)
def test_signed_claim_shape_is_checked(auth, claim, groups):
    _, token, _, _ = auth
    verifier = get_verifier(ISSUER, CLIENT, "custom:agency_id")
    changes = {"cognito:groups": groups}
    if claim:
        changes[claim] = "agency-1"
        result = verifier.verify(token(changes))
        assert result.groups == ("test-bookers",)
        assert result.agency_id == "agency-1"
        assert set(result.model_dump()) == {"subject", "issuer", "client_id"}
    else:
        with pytest.raises(HTTPException) as error:
            verifier.verify(token(changes))
        assert error.value.status_code == 401


@pytest.mark.parametrize(
    "endpoint,groups,status",
    [
        ("bookings", ["test-bookers"], 503),
        ("bookings", ["test-readers"], 403),
        ("booking-details", ["test-readers"], 503),
        ("booking-details", ["test-bookers"], 403),
        ("bookings", ["admin"], 403),
        ("bookings", [], 403),
    ],
)
def test_booking_operation_policy_uses_verified_claims(
    auth, monkeypatch, endpoint, groups, status
):
    _, token, _, _ = auth
    monkeypatch.setattr(settings, "cognito_agency_claim", "custom:agency_id")
    monkeypatch.setattr(settings, "booking_create_groups", ["test-bookers"])
    monkeypatch.setattr(settings, "booking_details_groups", ["test-readers"])
    signed = token({"cognito:groups": groups, "custom:agency_id": "agency-1"})
    with patch(
        "app.api.routes.group_ticketing._authenticated_dhillu_sahi_client",
        new_callable=AsyncMock,
    ) as supplier:
        response = call(
            f"/v1/group-ticketing/{endpoint}",
            {
                "Authorization": "Bearer " + signed,
                "X-Role": "admin",
                "X-Agency-ID": "agency-2",
            },
            "POST",
            {"booking_id": 123, "agency_id": "agency-2", "agent": 999},
        )
    assert response.status_code == status
    supplier.assert_not_called()


def test_group_without_agency_claim_cannot_book(auth, monkeypatch):
    _, token, _, _ = auth
    monkeypatch.setattr(settings, "cognito_agency_claim", "custom:agency_id")
    monkeypatch.setattr(settings, "booking_create_groups", ["test-bookers"])
    response = call(
        "/v1/group-ticketing/bookings",
        {
            "Authorization": "Bearer " + token({"cognito:groups": ["test-bookers"]}),
            "X-Agency-ID": "agency-1",
        },
        "POST",
        {"agency_id": "agency-1"},
    )
    assert response.status_code == 403


def test_machine_tokens_are_distinct_from_human_identity(auth):
    from app.core.authentication import AuthenticatedMachine, CognitoVerifier

    _, token, _, _ = auth
    verifier = CognitoVerifier(ISSUER, CLIENT, machine=True)
    signed = token({"sub": CLIENT, "scope": "test-api/search"})
    machine = verifier.verify(signed)
    assert isinstance(machine, AuthenticatedMachine)
    assert machine.scopes == ("test-api/search",)
    with pytest.raises(HTTPException) as rejected:
        get_verifier(ISSUER, CLIENT).verify(signed)
    assert rejected.value.status_code == 401
    with pytest.raises(HTTPException):
        verifier.verify(token({"scope": "test-api/search"}))
    for scope in [None, [], "", 123]:
        with pytest.raises(HTTPException):
            verifier.verify(token({"sub": CLIENT, "scope": scope}))


def test_machine_endpoint_scopes_registry_and_human_routes(auth, monkeypatch):
    from app.api.routes.agency_integrations import router as integration_router
    from app.core.integration_security import machine_verifier

    _, token, _, _ = auth
    monkeypatch.setattr(settings, "cognito_app_client_id", "separatehumanclient")
    monkeypatch.setattr(settings, "integration_search_scope", "test-api/search")
    registry = Mock(
        return_value={"agency_id": "test-agency", "scopes": ["test-api/search"]}
    )
    monkeypatch.setattr("app.core.integration_security.lookup", registry)
    machine_verifier.cache_clear()
    app = FastAPI()
    app.include_router(integration_router, prefix="/v1")
    app.include_router(identity_router, prefix="/v1")
    app.include_router(group_router, prefix="/v1")
    signed = token({"sub": CLIENT, "scope": "test-api/search"})

    async def request(path, bearer_token, method="get"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test.invalid"
        ) as client:
            return await getattr(client, method)(
                path, headers={"Authorization": "Bearer " + bearer_token}
            )

    result = asyncio.run(request("/v1/auth/integration", signed))
    assert result.status_code == 200
    assert result.json()["agency_id"] == "test-agency"
    assert asyncio.run(request("/v1/auth/me", signed)).status_code == 401
    assert (
        asyncio.run(
            request("/v1/auth/integration", token({"scope": "test-api/search"}))
        ).status_code
        == 401
    )
    assert (
        asyncio.run(
            request(
                "/v1/auth/integration", token({"sub": CLIENT, "scope": "other/scope"})
            )
        ).status_code
        == 403
    )
    monkeypatch.setattr(settings, "integration_booking_create_scope", "test-api/book")
    booking_token = token({"sub": CLIENT, "scope": "test-api/book"})
    registry.return_value = {"agency_id": "test-agency", "scopes": ["test-api/book"]}
    assert (
        asyncio.run(
            request("/v1/group-ticketing/bookings", booking_token, "post")
        ).status_code
        == 503
    )
    registry.side_effect = HTTPException(403, "Integration access denied.")
    assert asyncio.run(request("/v1/auth/integration", signed)).status_code == 403
    machine_verifier.cache_clear()
