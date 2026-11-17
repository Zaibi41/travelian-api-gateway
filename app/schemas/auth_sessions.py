"""Credentials are secret-valued and excluded from model repr output."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(strict=True, min_length=1, max_length=128, repr=False)
    password: SecretStr = Field(min_length=1, max_length=256, repr=False)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: SecretStr = Field(min_length=1, max_length=16384, repr=False)


class TokenResponse(BaseModel):
    access_token: str = Field(min_length=1, repr=False)
    id_token: str = Field(min_length=1, repr=False)
    refresh_token: str | None = Field(default=None, min_length=1, repr=False)
    token_type: Literal["Bearer"]
    expires_in: int = Field(strict=True, gt=0)


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(strict=True, min_length=1, max_length=128, repr=False)


class ResetPasswordRequest(ForgotPasswordRequest):
    confirmation_code: SecretStr = Field(min_length=1, max_length=2048, repr=False)
    new_password: SecretStr = Field(min_length=1, max_length=256, repr=False)


class PasswordResetMessage(BaseModel):
    message: str
