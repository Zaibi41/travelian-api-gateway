from fastapi import APIRouter, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from app.core import cognito_sessions
from app.schemas.auth_sessions import (
    ForgotPasswordRequest,
    LoginRequest,
    PasswordResetMessage,
    RefreshRequest,
    ResetPasswordRequest,
    TokenResponse,
)

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class PrivateAuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # Standard validation errors may echo passwords or refresh tokens.
                raise HTTPException(
                    422, "Invalid authentication request.", headers=NO_STORE
                ) from None
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), **NO_STORE}
                raise
            response.headers.update(NO_STORE)
            return response

        return private_handler


router = APIRouter(prefix="/auth", tags=["identity"], route_class=PrivateAuthRoute)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    return await run_in_threadpool(
        cognito_sessions.login, body.username, body.password.get_secret_value()
    )


@router.post("/refresh", response_model=TokenResponse, response_model_exclude_none=True)
async def refresh(body: RefreshRequest):
    return await run_in_threadpool(
        cognito_sessions.refresh, body.refresh_token.get_secret_value()
    )


@router.post("/logout", status_code=204)
async def logout(body: RefreshRequest):
    await run_in_threadpool(
        cognito_sessions.logout, body.refresh_token.get_secret_value()
    )
    return Response(status_code=204)


@router.post("/forgot-password", response_model=PasswordResetMessage)
async def forgot_password(body: ForgotPasswordRequest):
    return await run_in_threadpool(cognito_sessions.forgot_password, body.username)


@router.post("/reset-password", response_model=PasswordResetMessage)
async def reset_password(body: ResetPasswordRequest):
    return await run_in_threadpool(
        cognito_sessions.reset_password,
        body.username,
        body.confirmation_code.get_secret_value(),
        body.new_password.get_secret_value(),
    )
