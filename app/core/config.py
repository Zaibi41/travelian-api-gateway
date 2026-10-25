from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
