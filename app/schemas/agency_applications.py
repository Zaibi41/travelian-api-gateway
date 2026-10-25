"""Applicant-controlled fields exclude identity, status and permissions."""

import unicodedata
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)


def clean_contact_name(value: str) -> str:
    value = value.strip()
    if not value or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError(
            "Contact name must be nonblank and contain no control characters."
        )
    return value


ContactName = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=200),
    AfterValidator(clean_contact_name),
]


def plain_email(value):
    if (
        not isinstance(value, str)
        or len(value) > 254
        or any(unicodedata.category(c).startswith("C") or c in "<>" for c in value)
    ):
        raise ValueError("Contact email must be a plain email address.")
    return value


ContactEmail = Annotated[EmailStr, Field(max_length=254), BeforeValidator(plain_email)]
ContactPhone = Annotated[
    str, Field(strict=True, pattern=r"^\+[1-9][0-9]{1,14}$", max_length=16)
]


class AgencyApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agency_name: str = Field(strict=True, min_length=1, max_length=200)
    contact_name: ContactName
    contact_email: ContactEmail
    contact_phone: ContactPhone

    @field_validator("agency_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError(
                "Agency name must be nonblank and contain no control characters."
            )
        return value


class AgencyApplicationResponse(BaseModel):
    agency_id: str
    agency_name: str
    application_status: Literal[
        "pending_review", "more_information_required", "approved", "rejected"
    ]
    agency_status: Literal["suspended", "active"]
    submitted_at: datetime

    revision: int
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
