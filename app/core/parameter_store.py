import logging

from pydantic import ValidationError

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)

PARAMETER_SETTINGS = {
    "dhillu_sahi_base_url": "group-ticketing/dhillu-sahi/base-url",
    "dhillu_sahi_email": "group-ticketing/dhillu-sahi/email",
    "dhillu_sahi_password": "group-ticketing/dhillu-sahi/password",
    "dhillu_sahi_sector_group_ids": "group-ticketing/dhillu-sahi/sector-group-ids",
    "skypass_endpoint": "group-ticketing/skypass/endpoint",
    "skypass_client_id": "group-ticketing/skypass/client-id",
    "skypass_client_secret": "group-ticketing/skypass/client-secret",
    # Payments (D): rotated in Parameter Store without a code change; restart the service to pick them up.
    "payfast_merchant_id": "payments/payfast/merchant-id",
    "payfast_secured_key": "payments/payfast/secured-key",
    "jazzcash_merchant_id": "payments/jazzcash/merchant-id",
    "jazzcash_password": "payments/jazzcash/password",
    "jazzcash_integrity_salt": "payments/jazzcash/integrity-salt",
    # gateway#84: identity encryption keys (SecureString).
    "pii_keys": "identity/keys",
    "pii_active_key": "identity/active-key",
    "pii_hash_key": "identity/hash-key",
}


def load_parameter_store_settings() -> None:
    """Load configured vendor settings from SSM, retaining env values for missing parameters."""
    prefix_value = (settings.ssm_parameter_prefix or "").strip().strip("/")
    prefix = f"/{prefix_value}" if prefix_value else ""
    if not prefix:
        return

    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "boto3 is required when SSM_PARAMETER_PREFIX is configured; install requirements.txt"
        ) from exc

    parameter_to_setting = {
        f"{prefix}/{suffix}": setting_name
        for setting_name, suffix in PARAMETER_SETTINGS.items()
    }
    client_options = {
        "config": Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"total_max_attempts": 2, "mode": "standard"},
        )
    }
    if settings.aws_region:
        client_options["region_name"] = settings.aws_region

    try:
        client = boto3.client("ssm", **client_options)
        # SSM returns at most 10 parameters per call.
        names = list(parameter_to_setting)
        response = {"Parameters": [], "InvalidParameters": []}
        for start in range(0, len(names), 10):
            part = client.get_parameters(Names=names[start:start + 10], WithDecryption=True)
            response["Parameters"] += part.get("Parameters", [])
            response["InvalidParameters"] += part.get("InvalidParameters", [])
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "Unable to load vendor settings from SSM Parameter Store",
            extra={"exception_type": type(exc).__name__},
        )
        raise RuntimeError("Unable to load vendor settings from SSM Parameter Store") from exc

    overrides = {
        parameter_to_setting[item["Name"]]: item["Value"]
        for item in response.get("Parameters", [])
        if item.get("Name") in parameter_to_setting and isinstance(item.get("Value"), str)
    }

    for missing_name in response.get("InvalidParameters", []):
        logger.info(
            "SSM parameter not found; retaining environment setting",
            extra={"parameter": missing_name},
        )

    if not overrides:
        return

    try:
        validated_settings = Settings.model_validate(
            {**settings.model_dump(), **overrides}
        )
    except ValidationError as exc:
        logger.error(
            "SSM vendor settings failed validation",
            extra={"exception_type": type(exc).__name__},
        )
        raise RuntimeError("SSM vendor settings failed validation") from exc

    for setting_name in overrides:
        setattr(settings, setting_name, getattr(validated_settings, setting_name))

    logger.info(
        "Loaded vendor settings from SSM Parameter Store",
        extra={"parameter_count": len(overrides)},
    )
