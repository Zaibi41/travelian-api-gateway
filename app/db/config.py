"""Separate migration configuration, without requiring a database for inventory."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "travelian"
    db_user: str = "travelian_local"
    db_password: SecretStr | None = None

    def url(self, password: str | None = None) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=password
            if password is not None
            else (self.db_password.get_secret_value() if self.db_password else None),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
