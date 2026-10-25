"""Machine authentication is opt-in and never grants a human identity."""

import asyncio
import re
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.core.authentication import (
    ISSUER_PATTERN,
    AuthenticatedMachine,
    CognitoVerifier,
    bearer,
    unauthorized,
)
from app.core.config import settings
from app.db.agency_integrations import lookup


@lru_cache(maxsize=32)
def machine_verifier(issuer, client_id):
    return CognitoVerifier(issuer, client_id, machine=True)


async def require_machine(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    issuer = settings.cognito_issuer
    if not issuer or not ISSUER_PATTERN.fullmatch(issuer):
        raise HTTPException(503, "Integration authentication is not configured.")
    if (
        credentials is None
        or len(request.headers.getlist("authorization")) != 1
        or len(credentials.credentials) > 16384
    ):
        raise unauthorized()
    try:
        # Unverified client_id is only a bounded lookup hint; no authorization
        # occurs until the configured issuer's signature and exact claims pass.
        hint = jwt.decode(credentials.credentials, options={"verify_signature": False})
        client_id = hint.get("client_id")
        if (
            not isinstance(client_id, str)
            or not re.fullmatch(r"[a-z0-9]{1,128}", client_id)
            or client_id == settings.cognito_app_client_id
        ):
            raise unauthorized()
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise unauthorized() from None
    # Bound the verifier cache to registered, enabled clients only.
    await asyncio.to_thread(lookup, issuer, client_id)
    machine = await asyncio.to_thread(
        machine_verifier(issuer, client_id).verify, credentials.credentials
    )
    if not isinstance(machine, AuthenticatedMachine):
        raise unauthorized()
    return machine


async def authorize_machine(machine: AuthenticatedMachine, required_scope: str | None):
    if not required_scope or not re.fullmatch(r"[^\s/]+/[^\s]+", required_scope):
        raise HTTPException(503, "Integration permissions are not configured.")
    assignment = await asyncio.to_thread(lookup, machine.issuer, machine.client_id)
    if (
        required_scope not in machine.scopes
        or required_scope not in assignment["scopes"]
    ):
        raise HTTPException(403, "Integration access denied.")
    return assignment["agency_id"]
