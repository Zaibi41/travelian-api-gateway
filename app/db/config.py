"""Separate migration configuration, without requiring a database for inventory."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from app.core.config import ENV_FILE


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, extra="ignore", hide_input_in_errors=True
    )

    db_host: str = Field(min_length=1)
    db_port: int = Field(ge=1, le=65535)
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
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
