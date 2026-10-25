from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.agency_applications import (
    AgencyApplicationRequest,
    ContactEmail,
    ContactName,
    ContactPhone,
)


class AgencyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)
    agency_name: str | None = Field(
        default=None, strict=True, min_length=1, max_length=200
    )
    contact_name: ContactName | None = None
    contact_email: ContactEmail | None = None
    contact_phone: ContactPhone | None = None

    @field_validator("agency_name")
    @classmethod
    def valid_name(cls, value):
        return AgencyApplicationRequest.valid_name(value) if value is not None else None

    @model_validator(mode="after")
    def validate_changes(self):
        fields = self.model_fields_set - {"expected_revision"}
        if not fields or any(getattr(self, name) is None for name in fields):
            raise ValueError(
                "Supply at least one profile field; explicit null values are not allowed."
            )
        return self


class AgencyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)
    reason: str = Field(strict=True, min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value):
        return AgencyApplicationRequest.valid_name(value)


class AgencyEvent(BaseModel):
    action: str
    reason: str | None
    revision: int
    created_at: datetime
