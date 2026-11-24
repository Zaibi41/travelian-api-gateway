"""Final ticket documents for bookings ticketed outside Travelian (travelian#202), in PRIVATE S3: ticket-documents/<uuid>.<ext>.

Same rules as payment slips (app.services.payment_proof):
- Staff's browser uploads straight to S3 with a one-time signed POST (10 minutes). S3 enforces the content type and size.
- Before the key is saved on a booking, the gateway reads the object's real type and size from S3 (HeadObject).
- Staff open the document through a signed GET link valid for 5 minutes.
The bucket policy makes only site/ and payment-qr/ public (deploy/aws-setup.sh); ticket-documents/ stays private.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from app.core.config import settings
from app.services import assets

logger = logging.getLogger(__name__)

FOLDER = "ticket-documents"
TYPES = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}
MAX_BYTES = 10 * 1024 * 1024
LINK_SECONDS = 300
UNAVAILABLE = "Ticket document upload isn't available right now. Mark as issued without it and attach it later."


class DocumentError(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass(frozen=True)
class SignedDocumentUpload:
    url: str
    fields: dict[str, str]
    asset_key: str
    max_bytes: int


def valid_key(key: str | None) -> bool:
    if not key or not key.startswith(f"{FOLDER}/") or ".." in key or len(key) > 300:
        return False
    stem, _, extension = key[len(FOLDER) + 1:].rpartition(".")
    return extension in TYPES.values() and len(stem) == 36 and "/" not in stem


def sign_upload(content_type: str, client=None) -> SignedDocumentUpload:
    extension = TYPES.get(content_type)
    if extension is None:
        raise DocumentError(422, "Upload the ticket as a PDF, JPG or PNG file.", "invalid_type")
    if not settings.assets_bucket:
        raise DocumentError(503, UNAVAILABLE, "uploads_off")
    key = f"{FOLDER}/{uuid4()}.{extension}"
    try:
        post = (client or assets.s3_client()).generate_presigned_post(
            Bucket=settings.assets_bucket, Key=key, Fields={"Content-Type": content_type, "Cache-Control": "private, no-store"},
            Conditions=[{"Content-Type": content_type}, {"Cache-Control": "private, no-store"}, ["content-length-range", 1, MAX_BYTES]],
            ExpiresIn=600,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Ticket document upload not signed", extra={"error_type": type(error).__name__})
        raise DocumentError(503, UNAVAILABLE, "uploads_unavailable") from None
    return SignedDocumentUpload(url=post["url"], fields=post["fields"], asset_key=key, max_bytes=MAX_BYTES)


def inspect(key: str, client=None) -> str:
    """The uploaded object's real content type, checked against the rules."""
    if not valid_key(key):
        raise DocumentError(422, "Upload the ticket document first.", "invalid_key")
    try:
        head = (client or assets.s3_client()).head_object(Bucket=settings.assets_bucket, Key=key)
    except Exception as error:  # noqa: BLE001
        code = str(getattr(error, "response", {}).get("Error", {}).get("Code", "")) if hasattr(error, "response") else ""
        if code in ("404", "NoSuchKey", "NotFound") or not hasattr(error, "response"):
            raise DocumentError(422, "The ticket document didn't finish uploading. Upload it again.", "not_uploaded") from None
        logger.error("Ticket document can't be checked in storage", extra={"error_type": type(error).__name__, "code": code})
        raise DocumentError(503, UNAVAILABLE, "storage_unavailable") from None
    kind = (head.get("ContentType") or "").split(";")[0].strip().lower()
    size = int(head.get("ContentLength") or 0)
    if kind not in TYPES or TYPES[kind] != key.rpartition(".")[2]:
        raise DocumentError(422, "Upload the ticket as a PDF, JPG or PNG file.", "invalid_type")
    if not 0 < size <= MAX_BYTES:
        raise DocumentError(422, "The ticket document must be 10 MB or smaller.", "too_large")
    return kind


def signed_link(key: str, client=None) -> str:
    if not valid_key(key):
        raise DocumentError(404, "No ticket document on this booking.", "not_found")
    try:
        return (client or assets.s3_client()).generate_presigned_url(
            "get_object", Params={"Bucket": settings.assets_bucket, "Key": key, "ResponseContentDisposition": "inline"},
            ExpiresIn=LINK_SECONDS)
    except Exception as error:  # noqa: BLE001
        logger.warning("Ticket document link not signed", extra={"error_type": type(error).__name__})
        raise DocumentError(503, "The ticket document can't be opened right now. Try again in a moment.", "link_unavailable") from None
