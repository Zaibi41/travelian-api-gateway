"""Admin-uploaded media in S3 (B13, D38): one shared upload capability for every admin screen.

The gateway signs a one-time S3 POST (pre-signed, 10 minutes) for a known purpose; the browser uploads the file straight
to S3; the screen then saves only the returned object key (and its public URL) on its record. Files never touch the
gateway, the server's disk or the repo. S3 itself enforces the content type and the size limit (POST policy conditions).
Needs ASSETS_BUCKET, ASSETS_PUBLIC_BASE_URL and AWS credentials on the server; without them, uploads answer 503.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Purpose -> S3 folder. Images only (no SVG: it can carry script).
PURPOSES = {"site_logo": "site", "payment_qr": "payment-qr", "payment_proof": "payment-proof"}
TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_BYTES = 2 * 1024 * 1024


class AssetsError(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def configured() -> bool:
    return bool(settings.assets_bucket and settings.assets_public_base_url)


def public_url(key: str | None) -> str | None:
    if not key or not settings.assets_public_base_url:
        return None
    return f"{settings.assets_public_base_url.rstrip('/')}/{key}"


def valid_key(key: str | None, purpose: str) -> bool:
    folder = PURPOSES.get(purpose)
    return bool(key and folder and key.startswith(f"{folder}/") and ".." not in key and len(key) <= 300)


@dataclass(frozen=True)
class SignedUpload:
    url: str
    fields: dict[str, str]
    asset_key: str
    public_url: str | None
    max_bytes: int


def s3_client():
    """Signature Version 4 against the bucket's own regional endpoint. boto3's default for a presigned POST is the legacy
    Signature Version 2 on the global endpoint (fields AWSAccessKeyId / signature), which S3 refuses for buckets created
    after June 2020 ("Please use AWS4-HMAC-SHA256"): every admin upload failed in the browser."""
    import boto3
    from botocore.config import Config

    region = settings.assets_region
    return boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))


def sign_upload(purpose: str, content_type: str, client=None) -> SignedUpload:
    if purpose not in PURPOSES:
        raise AssetsError(422, "Unknown upload purpose.", "invalid_purpose")
    extension = TYPES.get(content_type)
    if extension is None:
        raise AssetsError(422, "Upload a PNG, JPEG or WebP image.", "invalid_type")
    if not configured():
        raise AssetsError(503, "Uploads aren't set up yet (S3 bucket and credentials).", "uploads_off")
    key = f"{PURPOSES[purpose]}/{uuid4()}.{extension}"
    try:
        client = client or s3_client()
        post = client.generate_presigned_post(
            Bucket=settings.assets_bucket, Key=key,
            Fields={"Content-Type": content_type, "Cache-Control": "public, max-age=31536000, immutable"},
            Conditions=[{"Content-Type": content_type}, {"Cache-Control": "public, max-age=31536000, immutable"},
                        ["content-length-range", 1, MAX_BYTES]],
            ExpiresIn=600,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Upload not signed", extra={"error_type": type(error).__name__})
        raise AssetsError(503, "Uploads aren't available right now.", "uploads_unavailable") from None
    return SignedUpload(url=post["url"], fields=post["fields"], asset_key=key, public_url=public_url(key), max_bytes=MAX_BYTES)
