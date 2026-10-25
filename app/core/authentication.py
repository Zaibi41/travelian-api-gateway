"""Verify Cognito user access tokens; this does not grant booking permissions."""

import asyncio
import re
from functools import lru_cache
from json import JSONDecodeError
from threading import Lock
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import (
    InvalidKeyError,
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
    PyJWKError,
    PyJWKSetError,
)
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings

bearer = HTTPBearer(auto_error=False)
ISSUER_PATTERN = re.compile(
    r"https://(?:issuer-)?cognito-idp\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9_-]+"
)


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(frozen=True)
    subject: str
    issuer: str
    client_id: str
    groups: tuple[str, ...] = Field(default=(), exclude=True, repr=False)
    agency_id: str | None = Field(default=None, exclude=True, repr=False)
    agency_claim: str | None = Field(default=None, exclude=True, repr=False)


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Invalid or missing access token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


class AuthenticatedMachine(BaseModel):
    model_config = ConfigDict(frozen=True)
    issuer: str
    client_id: str
    scopes: tuple[str, ...] = Field(default=(), exclude=True, repr=False)


class CognitoVerifier:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        agency_claim: str | None = None,
        machine: bool = False,
    ):
        self.machine = machine
        self.issuer = issuer
        self.client_id = client_id
        self.agency_claim = agency_claim
        self.keys = PyJWKClient(
            issuer + "/.well-known/jwks.json",
            timeout=3,
            cache_jwk_set=True,
            lifespan=300,
            cache_keys=False,
        )
        # Bound refresh concurrency per process; do not cache keys indefinitely.
        self.lock = Lock()

    def verify(self, token: str) -> AuthenticatedUser | AuthenticatedMachine:
        try:
            if len(token) > 16384:
                raise unauthorized()
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or not header["kid"]
            ):
                raise unauthorized()
            # The URL comes only from trusted configuration, never jku/iss in a token.
            with self.lock:
                key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "require": ["exp", "iat", "iss", "sub", "token_use", "client_id"],
                    # Cognito access tokens identify the app in client_id, not aud.
                    "verify_aud": False,
                },
            )
            if (
                claims["token_use"] != "access"
                or claims["client_id"] != self.client_id
                or not isinstance(claims["sub"], str)
                or not claims["sub"].strip()
                or (claims["sub"] == self.client_id) != self.machine
                or type(claims["exp"]) is not int
                or type(claims["iat"]) is not int
            ):
                raise unauthorized()
            if self.machine:
                scope = claims.get("scope")
                if not isinstance(scope, str) or not scope or len(scope) > 8192:
                    raise unauthorized()
                return AuthenticatedMachine(
                    issuer=self.issuer,
                    client_id=self.client_id,
                    scopes=tuple(scope.split()),
                )
            groups = claims.get("cognito:groups", [])
            agency_id = claims.get(self.agency_claim) if self.agency_claim else None
            if (
                not isinstance(groups, list)
                or any(
                    not isinstance(group, str) or not group.strip() for group in groups
                )
                or (agency_id is not None and not isinstance(agency_id, str))
            ):
                raise unauthorized()
            return AuthenticatedUser(
                subject=claims["sub"],
                issuer=self.issuer,
                client_id=self.client_id,
                groups=tuple(groups),
                agency_id=agency_id,
                agency_claim=self.agency_claim,
            )
        except (
            PyJWKClientConnectionError,
            PyJWKSetError,
            InvalidKeyError,
            JSONDecodeError,
        ):
            raise HTTPException(
                status_code=503,
                detail="Identity verification is temporarily unavailable.",
            ) from None
        except (
            InvalidTokenError,
            PyJWKClientError,
            PyJWKError,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise unauthorized() from None


@lru_cache(maxsize=4)
def get_verifier(
    issuer: str, client_id: str, agency_claim: str | None = None
) -> CognitoVerifier:
    return CognitoVerifier(issuer, client_id, agency_claim)


async def require_authenticated_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthenticatedUser:
    issuer = settings.cognito_issuer
    client_id = settings.cognito_app_client_id
    if (
        not issuer
        or not client_id
        or not ISSUER_PATTERN.fullmatch(issuer)
        or not client_id.isalnum()
    ):
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    if credentials is None or len(request.headers.getlist("authorization")) != 1:
        raise unauthorized()
    # JWKS retrieval and RSA verification must not block the ASGI event loop.
    return await asyncio.to_thread(
        get_verifier(issuer, client_id, settings.cognito_agency_claim).verify,
        credentials.credentials,
    )
