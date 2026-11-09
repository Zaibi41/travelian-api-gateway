import re
from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo's .env, found from this file rather than the working directory, so the
# gateway reads the same settings wherever uvicorn is started from. Real environment
# variables (Lightsail) still take precedence; a missing file is ignored.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Exact browser origin: scheme, host, optional port. No path, no wildcard.
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")


class Settings(BaseSettings):
    app_name: str = "Travelian API"
    api_prefix: str = "/v1"
    vendor_timeout_seconds: float = 8.0
    vendor_max_concurrency: int = 20
    enable_demo_vendor: bool = True
    dhillu_sahi_base_url: str = "https://api.dhilluandsahi.com"
    dhillu_sahi_email: str | None = None
    dhillu_sahi_password: SecretStr | None = None
    dhillu_sahi_timeout_seconds: float = 10.0
    dhillu_sahi_group_sector_id: str | None = None
    skypass_endpoint: str | None = None
    skypass_client_id: SecretStr | None = None
    skypass_client_secret: SecretStr | None = None
    aws_region: str | None = None
    ssm_parameter_prefix: str | None = None
    log_level: str = "INFO"
    cognito_issuer: str | None = None
    cognito_app_client_id: str | None = None
    cognito_agency_claim: str | None = None
    booking_create_groups: list[str] = []
    booking_details_groups: list[str] = []
    agency_admin_read_groups: list[str] = []
    agency_admin_write_groups: list[str] = []
    agency_admin_review_groups: list[str] = []
    integration_search_scope: str | None = None
    integration_booking_create_scope: str | None = None
    integration_booking_details_scope: str | None = None
    # Admin Dashboard (data: gateway `bookings` table).
    dashboard_expiring_hold_hours: int = 2
    dashboard_timezone: str = "Asia/Karachi"
    # Southair (AEROST OTA API, flights). Credentials are read only here, on the server.
    southair_base_url: str | None = None
    southair_username: str | None = None
    southair_password: SecretStr | None = None
    southair_timeout_seconds: float = Field(20.0, gt=0, le=60)
    # Hold (booking) and ticketing are separate switches, both off by default.
    southair_booking_enabled: bool = False
    southair_ticketing_enabled: bool = False
    # Flight selling price = supplier fare + percent + fixed amount per passenger (D23).
    # With both at 0 no selling price is returned and nothing can be confirmed or held.
    flight_markup_percent: Decimal = Field(Decimal("0"), ge=0, le=100)
    flight_markup_fixed_minor: int = Field(0, ge=0)
    # Cap on a revalidated price change (selling total, minor units, increase only). Unset: no
    # cap; a changed price only needs the user's explicit acceptance (spec confirmation #18).
    flight_price_change_limit_minor: int | None = Field(None, ge=0)
    # Group Ticketing selling price = supplier price + percent + fixed amount (D23).
    # With both at 0 no selling price is returned, so the supplier price is never
    # passed off as a selling price.
    group_ticketing_markup_percent: Decimal = Field(Decimal("0"), ge=0, le=100)
    group_ticketing_markup_fixed_minor: int = Field(0, ge=0)
    # Suppliers that send no currency price in this currency.
    group_ticketing_default_currency: str = Field("PKR", pattern=r"^[A-Z]{3}$")
    # Exact portal origins allowed by CORS, e.g. ["http://localhost:8080"].
    cors_allowed_origins: list[str] = []
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    @field_validator("cors_allowed_origins")
    @classmethod
    def exact_origins_only(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            if not _ORIGIN.fullmatch(origin):
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS entries must be exact origins like "
                    "https://admin.example.com (no wildcard, path or trailing slash)."
                )
        return origins

    @field_validator("dashboard_expiring_hold_hours")
    @classmethod
    def positive_window(cls, hours: int) -> int:
        if not 1 <= hours <= 168:
            raise ValueError("DASHBOARD_EXPIRING_HOLD_HOURS must be between 1 and 168.")
        return hours


settings = Settings()
