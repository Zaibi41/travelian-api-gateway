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
    # Dhillu & Sahi holds (add-booking holds seats unpaid; the supplier cancels them by itself; no cancel call).
    # Token and agent id are optional: without them the gateway logs in with the email and password and uses the
    # login's token and agent_id. When both an agent id here and the login's are present they must match.
    dhillu_sahi_token: SecretStr | None = None
    dhillu_sahi_agent_id: int | None = Field(None, gt=0)
    group_dhillu_sahi_hold_enabled: bool = True
    # Our Dhillu & Sahi hold time: hold time + this many minutes. agent-booking-detail has no expiry field (checked on
    # booking 33697, 14 Nov 2026), so this is an estimate; other suppliers keep GROUP_HOLD_MINUTES.
    dhillu_sahi_hold_default_minutes: int = Field(45, ge=1, le=1440)
    # Al Haider (Group Ticketing): a permanent access token sent as a Bearer header (no login call).
    al_haider_base_url: str | None = None
    al_haider_access_token: SecretStr | None = None
    # Al Haider holds (create/booking): off until Al Haider confirms that an unpaid booking expires by itself.
    # agency_info comes from the token's own profile (GET /api/user), not from settings.
    group_al_haider_hold_enabled: bool = False
    skypass_endpoint: str | None = None
    skypass_client_id: SecretStr | None = None
    skypass_client_secret: SecretStr | None = None
    aws_region: str | None = None
    ssm_parameter_prefix: str | None = None
    # Staff notifications (holds, booking requests). Recorded in the database always; emailed through Amazon SES when
    # NOTIFY_EMAIL_ENABLED and the server has AWS credentials. NOTIFY_EMAILS: comma-separated staff addresses.
    notify_email_enabled: bool = False
    notify_emails: str = ""
    notify_email_from: str = "Travelian <noreply@travelian.pk>"
    # gateway#59: SNS topic ARNs (comma-separated) allowed to post SES bounces and complaints to /v1/webhooks/ses.
    # Empty (default): every message is refused.
    ses_sns_topic_arns: str = ""
    ses_region: str = "us-east-1"
    # Airlines never held automatically (D32): IATA codes, comma-separated. Read from Parameter Store
    # (<SSM_PARAMETER_PREFIX>/flights/restricted-airlines) when configured, refreshed every few minutes; else this value.
    restricted_airlines: str = ""
    # Payment methods on the public checkout. JazzCash is scaffolded only (D31) until merchant credentials exist.
    bank_transfer_enabled: bool = False
    jazzcash_enabled: bool = False
    jazzcash_merchant_id: SecretStr | None = None
    jazzcash_password: SecretStr | None = None
    jazzcash_integrity_salt: SecretStr | None = None
    jazzcash_base_url: str = "https://sandbox.jazzcash.com.pk"
    # Admin-uploaded media (D38): S3 bucket and the public base URL its objects are served from.
    assets_bucket: str | None = None
    assets_region: str = "ap-southeast-1"
    assets_public_base_url: str | None = None
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
    # SooperFare (airline aggregator: Airblue, AirSial, PIA, Fly Jinnah, ...). Calls go out from the static IP it whitelists.
    sooperfare_base_url: str | None = None
    sooperfare_username: str | None = None
    sooperfare_password: SecretStr | None = None
    sooperfare_timeout_seconds: float = Field(30.0, gt=0, le=90)
    # Hold (reservation) and ticketing (booking confirm) are separate switches, both off by default.
    sooperfare_booking_enabled: bool = False
    sooperfare_ticketing_enabled: bool = False
    # Off until the owner approves the first real confirm: POST /v1/booking/confirm issues the ticket and charges
    # Travelian's SooperFare balance. Off: only the exact request is shown (supplier-confirm with preview).
    sooperfare_confirm_enabled: bool = False
    # Issuing stops this many seconds before the hold's time limit.
    ticketing_deadline_margin_seconds: int = Field(120, ge=0, le=3600)
    # Flight selling price = supplier fare + percent + fixed amount per passenger (D23).
    # With both at 0 no selling price is returned and nothing can be confirmed or held.
    flight_markup_percent: Decimal = Field(Decimal("0"), ge=0, le=100)
    flight_markup_fixed_minor: int = Field(0, ge=0)
    # SooperFare's platform fee per booking (minor units), used for the search price before SooperFare quotes the real
    # fee. The fee is passed on to the customer, shared across the adults, so a booking is never sold below its cost.
    sooperfare_platform_fee_minor: int = Field(2000, ge=0)
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
    # RateHawk (ETG B2B API v3 + Content API v1, hotels). HTTP Basic: key id + API key, server only.
    ratehawk_endpoint: str | None = None
    ratehawk_key_id: str | None = None
    ratehawk_api_key: SecretStr | None = None
    ratehawk_timeout_seconds: float = Field(30.0, gt=0, le=120)
    # Search defaults. The sandbox rejects PKR (invalid_params, checked 17 Nov 2026), so USD.
    ratehawk_currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    ratehawk_residency: str = Field("pk", pattern=r"^[a-z]{2}$")
    ratehawk_language: str = Field("en", pattern=r"^[a-z]{2}$")
    # Calls that change supplier state (booking finish, cancel, order groups, profiles): off by default.
    ratehawk_write_enabled: bool = False
    # Hotel selling price = supplier price + this percent. Unset (or empty) counts as 0.
    hotel_markup_percent: Decimal | None = Field(None, ge=0, le=100)
    # Exact portal origins allowed by CORS, e.g. ["http://localhost:8080"].
    cors_allowed_origins: list[str] = []
    # Public website booking (guests, no login). Off until turned on per product.
    public_group_booking_enabled: bool = False
    # Website flights: off -> a request for staff (no supplier call, no payment). On -> the fare is held at the supplier
    # (SOUTHAIR_/SOOPERFARE_BOOKING_ENABLED per provider) and paid by card (PayFast, auto-issued when ticketing is on,
    # D48), bank transfer or JazzCash (staff verify and issue, D49). Restricted airlines always stay requests (D32).
    public_flight_booking_enabled: bool = False
    # A card-paid website flight still not issued (or stuck mid-issue, e.g. after a restart) this long after its last change
    # gets one automatic retry, or staff are alerted (app.services.flight_payments.recover).
    flight_issuance_stuck_minutes: int = Field(10, ge=2, le=240)
    # D39: Umrah is hidden on the website (nav, search tab, links, URLs) until this is turned on. Nothing is deleted.
    umrah_enabled: bool = False
    # Where PayFast sends the customer back (the public website) and its server notification (this gateway).
    public_site_url: str = Field("https://travelian.pk", pattern=r"^https?://[^/]+$")
    public_api_url: str = Field("https://api.travelian.pk", pattern=r"^https?://[^/]+$")
    # gateway#62: the site whose /airline-logos/index.json (the website's one logo manifest) the PDF reads. Empty: public_site_url.
    airline_logos_site_url: str | None = Field(None, pattern=r"^https?://[^/]+$")
    # Public search answers are cached this long (seconds), so visitors don't each call the suppliers.
    public_search_cache_seconds: int = Field(120, ge=0, le=3600)
    # PayFast (Pakistan) hosted checkout. Sandbox host: https://ipguat.apps.net.pk.
    # PayFast on UAT (ipguat.apps.net.pk) is test money. Off (default): card / 1Link are not offered and nothing is issued
    # automatically. On: everything runs on UAT as on production, for end-to-end testing before production credentials.
    payfast_allow_uat: bool = False
    payfast_merchant_id: SecretStr | None = None
    payfast_secured_key: SecretStr | None = None
    payfast_merchant_name: str = "Travelian"
    payfast_base_url: str = Field("https://ipguat.apps.net.pk", pattern=r"^https://[^/]+$")
    payfast_timeout_seconds: float = Field(20, gt=0, le=60)
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    @field_validator("group_ticketing_markup_percent", "group_ticketing_markup_fixed_minor", "hotel_markup_percent", mode="before")
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
