"""Payment slip files in PRIVATE S3 (gateway#56): payment-proof/<uuid>.<ext>, never public.

- The customer's browser uploads straight to S3 with a one-time signed POST (10 minutes). S3 enforces the content
  type and 1 byte .. 5 MB.
- Before a slip is saved, the gateway reads the object's real type and size from S3 (HeadObject) and refuses anything
  else; the browser's word is never trusted.
- Staff open a slip through a signed GET link valid for 5 minutes.
The bucket policy makes only site/ and payment-qr/ public (deploy/aws-setup.sh); payment-proof/ stays private.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from app.core.config import settings
from app.services import assets

logger = logging.getLogger(__name__)

FOLDER = "payment-proof"
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
MAX_BYTES = 5 * 1024 * 1024
LINK_SECONDS = 300


class ProofError(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass(frozen=True)
class SignedProofUpload:
    url: str
    fields: dict[str, str]
    asset_key: str
    max_bytes: int


def valid_key(key: str | None) -> bool:
    if not key or not key.startswith(f"{FOLDER}/") or ".." in key or len(key) > 300:
        return False
    name = key[len(FOLDER) + 1:]
    stem, _, extension = name.rpartition(".")
    return extension in TYPES.values() and len(stem) == 36 and "/" not in stem


def sign_upload(content_type: str, client=None) -> SignedProofUpload:
    extension = TYPES.get(content_type)
    if extension is None:
        raise ProofError(422, "Upload a JPG, PNG, WebP or PDF file.", "invalid_type")
    if not settings.assets_bucket:
        raise ProofError(503, "Slip upload isn't available right now. Send the slip to Travelian on WhatsApp or email.", "uploads_off")
    key = f"{FOLDER}/{uuid4()}.{extension}"
    try:
        post = (client or assets.s3_client()).generate_presigned_post(
            Bucket=settings.assets_bucket, Key=key, Fields={"Content-Type": content_type, "Cache-Control": "private, no-store"},
            Conditions=[{"Content-Type": content_type}, {"Cache-Control": "private, no-store"}, ["content-length-range", 1, MAX_BYTES]],
            ExpiresIn=600,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Slip upload not signed", extra={"error_type": type(error).__name__})
        raise ProofError(503, "Slip upload isn't available right now. Try again in a moment.", "uploads_unavailable") from None
    return SignedProofUpload(url=post["url"], fields=post["fields"], asset_key=key, max_bytes=MAX_BYTES)


def inspect(key: str, client=None) -> tuple[str, int]:
    """The uploaded object's real content type and size, checked against the rules."""
    if not valid_key(key):
        raise ProofError(422, "Upload the slip first, then send it.", "invalid_key")
    try:
        head = (client or assets.s3_client()).head_object(Bucket=settings.assets_bucket, Key=key)
    except Exception as error:  # noqa: BLE001
        code = str(getattr(error, "response", {}).get("Error", {}).get("Code", "")) if hasattr(error, "response") else ""
        if code in ("404", "NoSuchKey", "NotFound") or not hasattr(error, "response"):
            logger.warning("Slip not found in storage", extra={"error_type": type(error).__name__})
            raise ProofError(422, "The slip didn't finish uploading. Upload it again.", "not_uploaded") from None
        # e.g. 403: the server may not read payment-proof/ (IAM s3:GetObject missing). Not the customer's fault.
        logger.error("Slip can't be checked in storage", extra={"error_type": type(error).__name__, "code": code})
        raise ProofError(503, "Slip upload isn't available right now. Send the slip to Travelian on WhatsApp or email.", "storage_unavailable") from None
    kind = (head.get("ContentType") or "").split(";")[0].strip().lower()
    size = int(head.get("ContentLength") or 0)
    if kind not in TYPES or TYPES[kind] != key.rpartition(".")[2]:
        raise ProofError(422, "Upload a JPG, PNG, WebP or PDF file.", "invalid_type")
    if not 0 < size <= MAX_BYTES:
        raise ProofError(422, "The slip must be 5 MB or smaller.", "too_large")
    return kind, size


def signed_link(key: str, client=None) -> str:
    if not valid_key(key):
        raise ProofError(404, "Payment slip not found.", "not_found")
    try:
        return (client or assets.s3_client()).generate_presigned_url(
            "get_object", Params={"Bucket": settings.assets_bucket, "Key": key, "ResponseContentDisposition": "inline"},
            ExpiresIn=LINK_SECONDS)
    except Exception as error:  # noqa: BLE001
        logger.warning("Slip link not signed", extra={"error_type": type(error).__name__})
        raise ProofError(503, "The slip can't be opened right now. Try again in a moment.", "link_unavailable") from None
