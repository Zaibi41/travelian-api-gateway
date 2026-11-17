from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth_sessions import router
from app.core import cognito_sessions as service
from app.core.config import settings


@pytest.fixture
def sessions(monkeypatch):
    monkeypatch.setattr(
        settings,
        "cognito_issuer",
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test",
    )
    monkeypatch.setattr(settings, "cognito_app_client_id", "testclient")
    client = Mock()
    client.initiate_auth.return_value = {
        "AuthenticationResult": {
            "AccessToken": "synthetic-access",
            "IdToken": "synthetic-id",
            "RefreshToken": "synthetic-refresh",
            "TokenType": "Bearer",
            "ExpiresIn": 3600,
        }
    }
    client.revoke_token.return_value = {}
    monkeypatch.setattr(service, "public_client", lambda region: client)
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    with TestClient(app) as http:
        yield http, client


def test_login_refresh_logout_mapping(sessions):
    http, client = sessions
    response = http.post(
        "/v1/auth/login",
        json={"username": "synthetic-user", "password": "synthetic-password"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["access_token"] == "synthetic-access"
    client.initiate_auth.assert_called_once_with(
        ClientId="testclient",
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": "synthetic-user", "PASSWORD": "synthetic-password"},
    )
    del client.initiate_auth.return_value["AuthenticationResult"]["RefreshToken"]
    response = http.post(
        "/v1/auth/refresh", json={"refresh_token": "synthetic-refresh"}
    )
    assert response.status_code == 200
    assert "refresh_token" not in response.json()
    client.initiate_auth.assert_called_with(
        ClientId="testclient",
        AuthFlow="REFRESH_TOKEN_AUTH",
        AuthParameters={"REFRESH_TOKEN": "synthetic-refresh"},
    )
    response = http.post("/v1/auth/logout", json={"refresh_token": "synthetic-refresh"})
    assert response.status_code == 204
    assert not response.content
    assert response.headers["cache-control"] == "no-store"
    client.revoke_token.assert_called_once_with(
        ClientId="testclient", Token="synthetic-refresh"
    )


@pytest.mark.parametrize(
    "code,status",
    [
        ("NotAuthorizedException", 401),
        ("UserNotFoundException", 401),
        ("UserNotConfirmedException", 401),
        ("PasswordResetRequiredException", 401),
        ("TooManyRequestsException", 429),
        ("LimitExceededException", 429),
        ("InvalidParameterException", 503),
        ("InternalErrorException", 503),
    ],
)
@pytest.mark.parametrize("endpoint", ["login", "refresh", "logout"])
def test_provider_errors_are_generic(sessions, code, status, endpoint, caplog):
    http, client = sessions
    error = ClientError(
        {"Error": {"Code": code, "Message": "private-provider-detail"}}, "Test"
    )
    client.initiate_auth.side_effect = error
    client.revoke_token.side_effect = error
    payload = (
        {"username": "synthetic-user", "password": "private-password"}
        if endpoint == "login"
        else {"refresh_token": "private-refresh"}
    )
    response = http.post("/v1/auth/" + endpoint, json=payload)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    for private in ["private-provider-detail", "private-password", "private-refresh"]:
        assert private not in response.text + caplog.text


@pytest.mark.parametrize(
    "challenge",
    ["NEW_PASSWORD_REQUIRED", "SMS_MFA", "SOFTWARE_TOKEN_MFA", "SELECT_CHALLENGE"],
)
def test_challenges_do_not_issue_tokens(sessions, challenge):
    http, client = sessions
    client.initiate_auth.return_value = {
        "ChallengeName": challenge,
        "Session": "private-session",
    }
    response = http.post(
        "/v1/auth/login",
        json={"username": "synthetic-user", "password": "private-password"},
    )
    assert response.status_code == 409
    assert "private-session" not in response.text
    assert "access_token" not in response.json()


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"AccessToken": "private-access"},
        {
            "AccessToken": "private-access",
            "IdToken": "private-id",
            "TokenType": "Bearer",
            "ExpiresIn": 0,
        },
    ],
)
def test_malformed_provider_result(sessions, result):
    http, client = sessions
    client.initiate_auth.return_value = {"AuthenticationResult": result}
    response = http.post(
        "/v1/auth/login",
        json={"username": "synthetic-user", "password": "private-password"},
    )
    assert response.status_code == 503
    assert "private-access" not in response.text


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("login", {"password": "private-password"}),
        ("refresh", {"refresh_token": 123, "extra": "private-refresh"}),
        ("logout", {"refresh_token": ""}),
    ],
)
def test_validation_does_not_echo_input(sessions, endpoint, payload):
    http, client = sessions
    response = http.post("/v1/auth/" + endpoint, json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid authentication request."}
    client.initiate_auth.assert_not_called()
    client.revoke_token.assert_not_called()


def test_unconfigured_auth_and_timeouts(sessions, monkeypatch):
    http, client = sessions
    monkeypatch.setattr(settings, "cognito_issuer", None)
    payload = {"username": "synthetic-user", "password": "private-password"}
    assert http.post("/v1/auth/login", json=payload).status_code == 503
    client.initiate_auth.assert_not_called()
    monkeypatch.setattr(
        settings,
        "cognito_issuer",
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test",
    )
    client.initiate_auth.side_effect = ReadTimeoutError(
        endpoint_url="https://test.invalid"
    )
    assert http.post("/v1/auth/login", json=payload).status_code == 503
    assert client.initiate_auth.call_count == 1


def test_public_sdk_client_is_unsigned_and_bounded(monkeypatch):
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(service.boto3, "client", factory)
    service.public_client.cache_clear()
    try:
        assert service.public_client("us-east-1") is client
        assert service.public_client("us-east-1") is client
        factory.assert_called_once()
        config = factory.call_args.kwargs["config"]
        from botocore import UNSIGNED

        assert config.signature_version is UNSIGNED
        assert config.retries["total_max_attempts"] == 1
        assert config.read_timeout == 5
    finally:
        service.public_client.cache_clear()


@pytest.mark.parametrize(
    "response", [None, [], {"AuthenticationResult": None}, {"AuthenticationResult": []}]
)
def test_invalid_provider_response_shape_is_closed(response):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        service.tokens(response)
    assert error.value.status_code == 503


def test_password_reset_mapping_and_no_store(sessions):
    http, client = sessions
    client.forgot_password.return_value = {
        "CodeDeliveryDetails": {"Destination": "private-address"}
    }
    client.confirm_forgot_password.return_value = {}
    response = http.post(
        "/v1/auth/forgot-password", json={"username": "synthetic-user"}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "private-address" not in response.text
    client.forgot_password.assert_called_once_with(
        ClientId="testclient", Username="synthetic-user"
    )
    response = http.post(
        "/v1/auth/reset-password",
        json={
            "username": "synthetic-user",
            "confirmation_code": "synthetic-code",
            "new_password": "synthetic-password",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "synthetic-code" not in response.text
    client.confirm_forgot_password.assert_called_once_with(
        ClientId="testclient",
        Username="synthetic-user",
        ConfirmationCode="synthetic-code",
        Password="synthetic-password",
    )
    client.initiate_auth.assert_not_called()


@pytest.mark.parametrize(
    "code",
    [
        "UserNotFoundException",
        "InvalidParameterException",
        "NotAuthorizedException",
        "UserNotConfirmedException",
    ],
)
def test_forgot_password_account_errors_match_success(sessions, code):
    http, client = sessions
    success = http.post("/v1/auth/forgot-password", json={"username": "synthetic-user"})
    client.forgot_password.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "private-provider-detail"}},
        "ForgotPassword",
    )
    failure = http.post("/v1/auth/forgot-password", json={"username": "synthetic-user"})
    assert success.status_code == failure.status_code == 200
    assert success.json() == failure.json()


@pytest.mark.parametrize(
    "code,status",
    [
        ("CodeMismatchException", 400),
        ("ExpiredCodeException", 400),
        ("InvalidPasswordException", 400),
        ("PasswordHistoryPolicyViolationException", 400),
        ("UserNotFoundException", 400),
        ("NotAuthorizedException", 400),
        ("InvalidParameterException", 400),
        ("TooManyRequestsException", 429),
        ("LimitExceededException", 429),
        ("InternalErrorException", 503),
        ("CodeDeliveryFailureException", 503),
    ],
)
def test_reset_provider_errors_are_redacted(sessions, code, status, caplog):
    http, client = sessions
    client.confirm_forgot_password.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "private-provider-detail"}},
        "ConfirmForgotPassword",
    )
    response = http.post(
        "/v1/auth/reset-password",
        json={
            "username": "synthetic-user",
            "confirmation_code": "synthetic-code",
            "new_password": "synthetic-password",
        },
    )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    for private in ["private-provider-detail", "synthetic-code", "synthetic-password"]:
        assert private not in response.text + caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "synthetic-user", "confirmation_code": "synthetic-code"},
        {
            "username": "synthetic-user",
            "confirmation_code": "",
            "new_password": "synthetic-password",
        },
        {
            "username": "synthetic-user",
            "confirmation_code": 123,
            "new_password": "synthetic-password",
        },
        {
            "username": "synthetic-user",
            "confirmation_code": "synthetic-code",
            "new_password": "synthetic-password",
            "extra": "private",
        },
    ],
)
def test_reset_validation_never_echoes_secrets(sessions, payload):
    http, client = sessions
    response = http.post("/v1/auth/reset-password", json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid authentication request."}
    client.confirm_forgot_password.assert_not_called()


@pytest.mark.parametrize(
    "endpoint,method,payload",
    [
        ("forgot-password", "forgot_password", {"username": "synthetic-user"}),
        (
            "reset-password",
            "confirm_forgot_password",
            {
                "username": "synthetic-user",
                "confirmation_code": "synthetic-code",
                "new_password": "synthetic-password",
            },
        ),
    ],
)
def test_reset_timeout_no_retry_and_missing_configuration(
    sessions, monkeypatch, endpoint, method, payload
):
    http, client = sessions
    getattr(client, method).side_effect = ReadTimeoutError(
        endpoint_url="https://test.invalid"
    )
    assert http.post("/v1/auth/" + endpoint, json=payload).status_code == 503
    assert getattr(client, method).call_count == 1
    monkeypatch.setattr(settings, "cognito_app_client_id", None)
    assert http.post("/v1/auth/" + endpoint, json=payload).status_code == 503
    assert getattr(client, method).call_count == 1
