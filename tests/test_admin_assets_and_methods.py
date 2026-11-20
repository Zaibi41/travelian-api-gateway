"""B3/B4/B13/B16: uploads signed for S3 only, full receiving-account numbers (D40), payment methods (JazzCash off), JazzCash unreachable."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import payment_accounts
from app.main import app
from app.services import assets, public_booking

client = TestClient(app)


def test_receiving_account_number_and_iban_are_kept_in_full():
    # D40: the account customers pay into is never masked; the short form is only for lists.
    assert payment_accounts.clean_number(" 0011-2345 6702 ") == "0011-2345 6702"
    assert payment_accounts.mask_number("0011-2345 6702") == "**** **** 6702"
    assert payment_accounts.clean_iban("pk36 scbl 0000 0011 2345 6702") == "PK36SCBL0000001123456702"
    assert payment_accounts.clean_number("") is None and payment_accounts.clean_iban(None) is None
    for bad in ("123", "12ab5678", "**** 1234"):
        with pytest.raises(payment_accounts.AccountError):
            payment_accounts.clean_number(bad)
    with pytest.raises(payment_accounts.AccountError):
        payment_accounts.clean_iban("1234")


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


def test_customers_see_the_full_account_number_and_iban_to_pay_into():
    accounts = [payment_accounts.Account("a1", "bank_transfer", "HBL main", True, "HBL", "Travelian (Pvt) Ltd", "**** **** 6702", "payment-qr/x.png",
                                         "0011 2345 6702", "PK36HABB0000001123456702"),
                # Old row from before 0024 (only the last 4 digits): a customer couldn't pay into it, so it isn't offered.
                payment_accounts.Account("a3", "bank_transfer", "Old", True, "MCB", "Travelian", "**** **** 1111"),
                payment_accounts.Account("a2", "jazzcash", "Wallet", True)]
    with patch.object(payment_accounts, "list_accounts", lambda include_inactive=False: accounts), \
         patch.object(settings, "bank_transfer_enabled", True), patch.object(settings, "assets_public_base_url", "https://assets.example"), \
         patch("app.api.routes.admin_uploads.site_assets", lambda: {}):
        body = client.get("/v1/public/payment-methods").json()
    assert body["bank_transfer"] is True
    assert body["bank_accounts"] == [{"id": "a1", "bank_name": "HBL", "account_title": "Travelian (Pvt) Ltd", "account_number": "0011 2345 6702",
                                      "iban": "PK36HABB0000001123456702",
                                      "qr_url": "https://assets.example/payment-qr/x.png", "qr_download_url": "/v1/public/payment-qr/a1"}]


def test_umrah_is_hidden_by_default_and_switches_with_a_setting():
    # D39: one server setting, read by the website at load time (no rebuild).
    assert client.get("/v1/public/site-config").json() == {"umrah_enabled": False}
    with patch.object(settings, "umrah_enabled", True):
        assert client.get("/v1/public/site-config").json() == {"umrah_enabled": True}


def test_email_alerts_report_whether_they_are_set_up():
    from app.services import notifications

    with patch.object(settings, "notify_email_enabled", False), patch.object(settings, "notify_emails", "ops@example.invalid"):
        assert notifications.email_configured() is False
    with patch.object(settings, "notify_email_enabled", True), patch.object(settings, "notify_emails", ""):
        assert notifications.email_configured() is False
    with patch.object(settings, "notify_email_enabled", True), patch.object(settings, "notify_emails", "ops@example.invalid"):
        assert notifications.email_configured() is True


# ---------- QR / logo uploads: how S3 is signed (the cause of the failing QR upload, 19 Nov 2026) ----------

@pytest.mark.parametrize("content_type, extension", [("image/png", "png"), ("image/jpeg", "jpg"), ("image/webp", "webp")])
def test_uploads_are_signed_with_signature_v4_on_the_regional_endpoint(monkeypatch, content_type, extension):
    """boto3's default presigned POST is Signature Version 2 on the global endpoint, which S3 refuses for buckets created
    after June 2020. Signed offline here with dummy credentials (no AWS call)."""
    for name, value in {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLEEXAMPLE", "AWS_SECRET_ACCESS_KEY": "dummy", "AWS_CONFIG_FILE": "/dev/null",
                        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null"}.items():
        monkeypatch.setenv(name, value)
    with patch.object(settings, "assets_bucket", "travelian-assets-test"), patch.object(settings, "assets_region", "ap-southeast-1"), \
         patch.object(settings, "assets_public_base_url", "https://travelian-assets-test.s3.ap-southeast-1.amazonaws.com"):
        signed = assets.sign_upload("payment_qr", content_type)
    assert signed.url == "https://travelian-assets-test.s3.ap-southeast-1.amazonaws.com/"
    assert {"x-amz-algorithm", "x-amz-credential", "x-amz-signature", "policy", "key"} <= set(signed.fields)
    assert signed.fields["x-amz-algorithm"] == "AWS4-HMAC-SHA256" and "AWSAccessKeyId" not in signed.fields
    assert signed.fields["Content-Type"] == content_type and signed.asset_key.startswith("payment-qr/") and signed.asset_key.endswith(extension)
    assert signed.public_url == f"https://travelian-assets-test.s3.ap-southeast-1.amazonaws.com/{signed.asset_key}"
    import base64
    import json

    policy = json.loads(base64.b64decode(signed.fields["policy"]))
    # S3 enforces these itself, so a signed POST can't be reused for another key, type, bucket or a bigger file
    # (checked live against the bucket on 19 Nov 2026: other key / type -> AccessDenied, over 2 MB -> EntityTooLarge).
    assert ["content-length-range", 1, 2 * 1024 * 1024] in policy["conditions"]
    assert {"key": signed.asset_key} in policy["conditions"] and {"bucket": "travelian-assets-test"} in policy["conditions"]
    assert {"Content-Type": content_type} in policy["conditions"]


def test_wrong_type_or_s3_down_is_a_clear_error_and_nothing_is_signed():
    with patch.object(settings, "assets_bucket", "b"), patch.object(settings, "assets_public_base_url", "https://b.example"):
        for bad in ("image/gif", "image/svg+xml", "application/pdf"):
            with pytest.raises(assets.AssetsError) as error:
                assets.sign_upload("payment_qr", bad)
            assert error.value.status == 422 and "PNG, JPEG or WebP" in error.value.message
        broken = MagicMock()
        broken.generate_presigned_post.side_effect = RuntimeError("no credentials")
        with pytest.raises(assets.AssetsError) as error:
            assets.sign_upload("payment_qr", "image/png", client=broken)
        assert error.value.status == 503
    with patch.object(settings, "assets_bucket", None):
        with pytest.raises(assets.AssetsError) as error:
            assets.sign_upload("payment_qr", "image/png")
    assert error.value.status == 503 and "S3 bucket" in error.value.message


@pytest.mark.parametrize("purpose, folder", [("site_logo", "site"), ("payment_qr", "payment-qr"), ("payment_proof", "payment-proof")])
def test_every_upload_purpose_is_signed_v4_into_its_own_folder(monkeypatch, purpose, folder):
    """Every feature on the shared signer (B13, D38): Website & CMS > Site logo, Accounts & Ledgers QR images, and
    payment proofs (purpose kept, no screen uses it yet). Same SigV4 signing for each; each only in its own folder."""
    for name, value in {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLEEXAMPLE", "AWS_SECRET_ACCESS_KEY": "dummy", "AWS_CONFIG_FILE": "/dev/null",
                        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null"}.items():
        monkeypatch.setenv(name, value)
    with patch.object(settings, "assets_bucket", "travelian-assets-test"), patch.object(settings, "assets_region", "ap-southeast-1"), \
         patch.object(settings, "assets_public_base_url", "https://travelian-assets-test.s3.ap-southeast-1.amazonaws.com"):
        signed = assets.sign_upload(purpose, "image/png")
    assert signed.fields["x-amz-algorithm"] == "AWS4-HMAC-SHA256" and signed.fields["key"] == signed.asset_key
    assert signed.asset_key.startswith(f"{folder}/") and assets.valid_key(signed.asset_key, purpose)
    assert not any(assets.valid_key(signed.asset_key, other) for other in assets.PURPOSES if other != purpose)


def test_site_logo_route_saves_only_an_uploaded_logo_key():
    """Website & CMS > Site logo: PUT /v1/admin/site-assets/site_logo keeps the key and its public URL; the public site reads it
    back from /v1/public/payment-methods (logo_url)."""
    from app.api.routes import admin_uploads
    from app.core.field_visibility import ALL, BackOfficeUser, require_finance_accounts

    saved = []
    app.dependency_overrides[require_finance_accounts] = lambda: BackOfficeUser("u1", "super_admin", ALL)
    try:
        with patch.object(settings, "assets_public_base_url", "https://assets.example"), \
             patch.object(admin_uploads, "_save", lambda slot, key, url, actor: saved.append((slot, key, url))):
            ok = client.put("/v1/admin/site-assets/site_logo", json={"asset_key": "site/abc.png"})
            wrong_folder = client.put("/v1/admin/site-assets/site_logo", json={"asset_key": "payment-qr/abc.png"})
            with patch.object(settings, "assets_public_base_url", None):
                not_set_up = client.put("/v1/admin/site-assets/site_logo", json={"asset_key": "site/abc.png"})
    finally:
        app.dependency_overrides.clear()
    assert ok.status_code == 200 and ok.json() == {"slot": "site_logo", "asset_key": "site/abc.png", "url": "https://assets.example/site/abc.png"}
    assert wrong_folder.status_code == 422 and not_set_up.status_code == 503
    assert saved == [("site_logo", "site/abc.png", "https://assets.example/site/abc.png")]
    with patch.object(admin_uploads, "site_assets", lambda: {"site_logo": "https://assets.example/site/abc.png"}), \
         patch.object(public_booking, "bank_accounts", lambda: []):
        assert client.get("/v1/public/payment-methods").json()["logo_url"] == "https://assets.example/site/abc.png"
