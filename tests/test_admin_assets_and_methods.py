"""B3/B4/B13: uploads signed for S3 only, masked account numbers, payment methods (JazzCash off), JazzCash unreachable."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import payment_accounts
from app.main import app
from app.services import assets, public_booking

client = TestClient(app)


def test_account_numbers_are_only_ever_stored_masked():
    assert payment_accounts.mask_number("PK36 SCBL 0000 0011 2345 6702") == "**** **** 6702"
    assert payment_accounts.mask_number("") is None
    with pytest.raises(payment_accounts.AccountError):
        payment_accounts.mask_number("123")


def test_uploads_are_signed_for_s3_images_only():
    with pytest.raises(assets.AssetsError) as off:
        assets.sign_upload("payment_qr", "image/png")
    assert off.value.status == 503  # no bucket configured: never stored anywhere else
    with pytest.raises(assets.AssetsError) as svg:
        assets.sign_upload("payment_qr", "image/svg+xml")
    assert svg.value.status == 422
    s3 = MagicMock()
    s3.generate_presigned_post.return_value = {"url": "https://bucket.s3.amazonaws.com/", "fields": {"key": "k"}}
    with patch.object(settings, "assets_bucket", "travelian-assets"), patch.object(settings, "assets_public_base_url", "https://assets.example"):
        signed = assets.sign_upload("payment_qr", "image/png", client=s3)
    kwargs = s3.generate_presigned_post.call_args.kwargs
    assert signed.asset_key.startswith("payment-qr/") and signed.asset_key.endswith(".png")
    assert ["content-length-range", 1, assets.MAX_BYTES] in kwargs["Conditions"] and kwargs["Bucket"] == "travelian-assets"
    assert signed.public_url == f"https://assets.example/{signed.asset_key}"
    assert assets.valid_key(signed.asset_key, "payment_qr") and not assets.valid_key("../etc/passwd", "payment_qr")
    assert not assets.valid_key(signed.asset_key, "site_logo")


def test_payment_methods_and_jazzcash_off():
    with patch.object(public_booking, "bank_accounts", lambda: []), patch("app.api.routes.admin_uploads.site_assets", lambda: {}):
        body = client.get("/v1/public/payment-methods").json()
    assert body["jazzcash"] is False and body["bank_transfer"] is False
    assert client.post("/v1/public/bookings/TRV-G-1/payments/jazzcash").status_code == 404
    assert client.post("/v1/public/payments/jazzcash/notify", json={"pp_ResponseCode": "000"}).json() == {"status": "ignored", "verified": False}


def test_bank_transfer_lists_only_active_accounts_with_masked_numbers():
    accounts = [payment_accounts.Account("a1", "bank_transfer", "HBL main", True, "HBL", "Travelian (Pvt) Ltd", "**** **** 6702", "payment-qr/x.png"),
                payment_accounts.Account("a2", "jazzcash", "Wallet", True)]
    with patch.object(payment_accounts, "list_accounts", lambda include_inactive=False: accounts), \
         patch.object(settings, "bank_transfer_enabled", True), patch.object(settings, "assets_public_base_url", "https://assets.example"), \
         patch("app.api.routes.admin_uploads.site_assets", lambda: {}):
        body = client.get("/v1/public/payment-methods").json()
    assert body["bank_transfer"] is True
    assert body["bank_accounts"] == [{"bank_name": "HBL", "account_title": "Travelian (Pvt) Ltd", "account_number_masked": "**** **** 6702",
                                      "qr_url": "https://assets.example/payment-qr/x.png"}]
