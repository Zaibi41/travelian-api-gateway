import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserRecord(BaseModel):
    user_id: str
    cognito_issuer: str
    cognito_sub: str
    role_id: int
    role: Literal["customer", "agency_owner", "super_admin", "staff"]
    active: bool
    revision: int
    created_at: datetime
    agency_id: str | None = None


class AdminChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)
    reason: str = Field(strict=True, min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value):
        value = value.strip()
        if not value or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError(
                "A nonblank reason without control characters is required."
            )
        return value


class UserRoleChange(AdminChange):
    role_id: Literal[1, 2, 3, 4]

    @field_validator("role_id", mode="before")
    @classmethod
    def strict_role(cls, value):
        if type(value) is not int:
            raise ValueError("Role ID must be an integer.")
        return value


class UserStatusChange(AdminChange):
    active: bool = Field(strict=True)


class RoleRecord(BaseModel):
    id: int
    name: str


class UserAdminEvent(BaseModel):
    id: str
    actor_id: str
    action: str
    reason: str
    previous_role_id: int
    new_role_id: int
    previous_active: bool
    new_active: bool
    revision: int
    created_at: datetime


class AdminOverview(BaseModel):
    user_coverage: Literal["local_profiles"] = "local_profiles"
    total_users: int
    active_users: int
    customers: int
    agency_owners: int
    super_admins: int
    total_agencies: int
    active_agencies: int
    suspended_agencies: int
    pending_applications: int
    rejected_applications: int
