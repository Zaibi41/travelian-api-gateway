"""Public-client Cognito session calls; no AWS credentials or supplier access."""

import re
from functools import lru_cache

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.auth_sessions import TokenResponse


def configuration():
    match = re.fullmatch(
        r"https://cognito-idp\.([a-z0-9-]+)\.amazonaws\.com/([A-Za-z0-9_-]+)",
        settings.cognito_issuer or "",
    )
    client_id = settings.cognito_app_client_id
    if not match or not client_id or not re.fullmatch(r"[a-z0-9]{1,128}", client_id):
        raise HTTPException(503, "Authentication is not configured.")
    return match.group(1), client_id


@lru_cache(maxsize=4)
def public_client(region):
    return boto3.client(
        "cognito-idp",
        region_name=region,
        config=Config(
            signature_version=UNSIGNED,
            connect_timeout=3,
            read_timeout=5,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


def call(operation, **kwargs):
    region, client_id = configuration()
    try:
        return getattr(public_client(region), operation)(ClientId=client_id, **kwargs)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if operation == "forgot_password" and code in (
            "UserNotFoundException",
            "InvalidParameterException",
            "NotAuthorizedException",
            "UserNotConfirmedException",
        ):
            # Same outward acknowledgement as a real delivery; never expose
            # account existence, verification status or delivery destination.
            return {}
        if operation == "confirm_forgot_password" and code in (
            "CodeMismatchException",
            "ExpiredCodeException",
            "UserNotFoundException",
            "NotAuthorizedException",
            "UserNotConfirmedException",
            "InvalidParameterException",
        ):
            raise HTTPException(
                400, "Invalid or expired password reset request."
            ) from None
        if operation == "confirm_forgot_password" and code in (
            "InvalidPasswordException",
            "PasswordHistoryPolicyViolationException",
        ):
            raise HTTPException(
                400, "New password does not meet the password policy."
            ) from None
        if code in (
            "NotAuthorizedException",
            "UserNotFoundException",
            "UserNotConfirmedException",
            "PasswordResetRequiredException",
        ):
            raise HTTPException(401, "Authentication failed.") from None
        if code in ("TooManyRequestsException", "LimitExceededException"):
            raise HTTPException(
                429, "Too many authentication requests.", headers={"Retry-After": "5"}
            ) from None
        raise HTTPException(503, "Authentication service unavailable.") from None
    except BotoCoreError:
        raise HTTPException(503, "Authentication service unavailable.") from None


def tokens(response, require_refresh=False):
    if not isinstance(response, dict):
        raise HTTPException(503, "Authentication service unavailable.")
    if response.get("ChallengeName"):
        # Never pretend MFA/password-change challenges are a successful login.
        # Session material is deliberately withheld until a challenge flow exists.
        raise HTTPException(
            409,
            "Additional sign-in steps required. Use the configured Cognito sign-in flow.",
        )
    result = response.get("AuthenticationResult", {})
    if not isinstance(result, dict):
        raise HTTPException(503, "Authentication service unavailable.")
    try:
        parsed = TokenResponse(
            access_token=result.get("AccessToken"),
            id_token=result.get("IdToken"),
            refresh_token=result.get("RefreshToken"),
            token_type=result.get("TokenType"),
            expires_in=result.get("ExpiresIn"),
        )
        if require_refresh and not parsed.refresh_token:
            raise ValueError("Missing refresh token")
        return parsed
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(503, "Authentication service unavailable.") from None


def login(username, password):
    return tokens(
        call(
            "initiate_auth",
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        ),
        require_refresh=True,
    )


def refresh(refresh_token):
    return tokens(
        call(
            "initiate_auth",
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
    )


def logout(refresh_token):
    call("revoke_token", Token=refresh_token)


def forgot_password(username):
    call("forgot_password", Username=username)
    return {
        "message": "If the account is eligible, a password reset code has been sent."
    }


def reset_password(username, confirmation_code, new_password):
    call(
        "confirm_forgot_password",
        Username=username,
        ConfirmationCode=confirmation_code,
        Password=new_password,
    )
    return {"message": "Password reset successfully. Sign in with your new password."}
