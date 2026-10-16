from pydantic import SecretStr

from app.core.config import settings


def credentials_configured() -> bool:
    return bool(settings.dhillu_sahi_email and settings.dhillu_sahi_password)


def password_secret() -> SecretStr | None:
    if not settings.dhillu_sahi_password:
        return None
    return SecretStr(settings.dhillu_sahi_password)
