from pydantic import SecretStr

from app.core.config import settings


def credentials_configured() -> bool:
    password = settings.dhillu_sahi_password
    return bool(settings.dhillu_sahi_email and password and password.get_secret_value())


def password_secret() -> SecretStr | None:
    return settings.dhillu_sahi_password
