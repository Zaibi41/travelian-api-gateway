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
    # Sector group ids for the listing call (my_sector_id), comma-separated; one listing call per id, merged.
    # No default and no ids in code: unset or empty means the Dhillu & Sahi listing is not configured.
    dhillu_sahi_sector_group_ids: str | None = None
    # Dhillu & Sahi holds (add-booking holds seats unpaid; the supplier ends them after 1 hour; no cancel call).
    # Token and agent id are optional: without them the gateway logs in with the email and password and uses the
    # login's token and agent_id. When both an agent id here and the login's are present they must match.
    dhillu_sahi_token: SecretStr | None = None
    dhillu_sahi_agent_id: int | None = Field(None, gt=0)
    group_dhillu_sahi_hold_enabled: bool = True
    # Al Haider (Group Ticketing): a permanent access token sent as a Bearer header (no login call).
    al_haider_base_url: str | None = None
    al_haider_access_token: SecretStr | None = None
    # Al Haider holds (create/booking): off until Al Haider confirms that an unpaid booking expires by itself.
    # The agency_info sent with each booking is Travelian's own agency (all four are needed to hold).
    group_al_haider_hold_enabled: bool = False
    al_haider_agent_name: str | None = None
    al_haider_agency_name: str | None = None
    al_haider_agent_email: str | None = None
    al_haider_agent_mobile: str | None = None
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
    # Issuing stops this many seconds before the hold's time limit.
    ticketing_deadline_margin_seconds: int = Field(120, ge=0, le=3600)
    # Flight selling price = supplier fare + percent + fixed amount per passenger (D23).
    # With both at 0 no selling price is returned and nothing can be confirmed or held.
    flight_markup_percent: Decimal = Field(Decimal("0"), ge=0, le=100)
    flight_markup_fixed_minor: int = Field(0, ge=0)
    # Cap on a revalidated price change (selling total, minor units, increase only). Unset: no
    # cap; a changed price only needs the user's explicit acceptance (spec confirmation #18).
    flight_price_change_limit_minor: int | None = Field(None, ge=0)
    # Group Ticketing selling price = listing price + percent + fixed amount (D23).
    # Unset (or empty) counts as 0: the fare sells at the listing price per adult.
    group_ticketing_markup_percent: Decimal | None = Field(None, ge=0, le=100)
    group_ticketing_markup_fixed_minor: int | None = Field(None, ge=0)
    # Suppliers that send no currency price in this currency.
    group_ticketing_default_currency: str = Field("PKR", pattern=r"^[A-Z]{3}$")
    # Group holds: Travelian's own hold time (suppliers hold longer; this is the portal's limit).
    # Expiry = hold creation + this many minutes, kept on the booking; no supplier expiry is used.
    group_hold_minutes: int = Field(30, ge=1, le=1440)
    # The expiry job: how often it runs, and how many times a failed supplier release is retried.
    group_hold_job_interval_seconds: int = Field(30, ge=5, le=3600)
    group_hold_release_attempts: int = Field(5, ge=1, le=50)
    # Exact portal origins allowed by CORS, e.g. ["http://localhost:8080"].
    cors_allowed_origins: list[str] = []
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    @field_validator("group_ticketing_markup_percent", "group_ticketing_markup_fixed_minor", mode="before")
    @classmethod
    def empty_markup_is_unset(cls, value):
        """GROUP_TICKETING_MARKUP_*= (empty, as in .env.example) means not set (markup 0)."""
        return None if isinstance(value, str) and not value.strip() else value

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
