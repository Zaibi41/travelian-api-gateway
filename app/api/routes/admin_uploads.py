"""Admin uploads (B13, D38): one signed S3 upload for every admin screen, and site media slots (e.g. the site logo)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BackOfficeUser, require_finance_accounts
from app.db import agencies
from app.services import assets

router = APIRouter(prefix="/admin", tags=["admin uploads"])
Uploader = Annotated[BackOfficeUser, Depends(require_finance_accounts)]  # Super Admin / Admin (Finance & settings)


class SignUploadRequest(BaseModel):
    purpose: Literal["site_logo", "payment_qr", "payment_proof"]
    content_type: Literal["image/png", "image/jpeg", "image/webp"]


class SignedUploadView(BaseModel):
    url: str = Field(description="POST the file here as multipart form data: `fields` first, then `file`.")
    fields: dict[str, str]
    asset_key: str = Field(description="Save this key on the record (account QR, site logo). Never a file or local path.")
    public_url: str | None = None
    max_bytes: int


@router.post("/uploads", response_model=SignedUploadView, responses={503: {"description": "S3 not set up."}})
async def sign_upload(body: SignUploadRequest, viewer: Uploader) -> SignedUploadView:
    try:
        signed = await run_in_threadpool(assets.sign_upload, body.purpose, body.content_type)
    except assets.AssetsError as error:
        raise HTTPException(error.status, error.message) from None
    return SignedUploadView(url=signed.url, fields=signed.fields, asset_key=signed.asset_key, public_url=signed.public_url,
                            max_bytes=signed.max_bytes)


SLOTS = {"site_logo": "site_logo"}


class SiteAssetRequest(BaseModel):
    asset_key: str = Field(min_length=5, max_length=300)


class SiteAssetView(BaseModel):
    slot: str
    asset_key: str
    url: str


def _save(slot: str, key: str, url: str, actor: str | None) -> None:
    with agencies.application_engine().begin() as connection:
        connection.execute(text("INSERT INTO site_assets (slot, asset_key, url, updated_by_user_id) VALUES (:s, :k, :u, :a)"
                                " ON CONFLICT (slot) DO UPDATE SET asset_key = :k, url = :u, updated_by_user_id = :a, updated_at = now()"),
                           {"s": slot, "k": key, "u": url, "a": actor})


def site_assets() -> dict[str, str]:
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text("SELECT slot, url FROM site_assets")).mappings().all()
    return {row["slot"]: row["url"] for row in rows}


@router.put("/site-assets/{slot}", response_model=SiteAssetView)
async def set_site_asset(slot: Literal["site_logo"], body: SiteAssetRequest, viewer: Uploader) -> SiteAssetView:
    """Website & CMS: point a site slot (the logo) at an uploaded image."""
    if not assets.valid_key(body.asset_key, slot):
        raise HTTPException(422, "Upload the image first, then save it.")
    url = assets.public_url(body.asset_key)
    if url is None:
        raise HTTPException(503, "Uploads aren't set up yet (S3 bucket and credentials).")
    await run_in_threadpool(_save, slot, body.asset_key, url, viewer.user_id)
    return SiteAssetView(slot=slot, asset_key=body.asset_key, url=url)
