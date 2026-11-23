"""Download QR: the checkout account's QR image as a file through the gateway (S3 has no CORS for the website)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import payment_accounts
from app.main import app
from app.services import public_booking as service

UBL = payment_accounts.Account(id="acc-1", method="bank_transfer", label="UBL Travelian", active=True, bank_name="United Bank Limited - UBL",
                               account_title="Travelian Private Limited", account_number="1937359762048", qr_asset_key="payment-qr/ubl.jpg")
NO_QR = payment_accounts.Account(id="acc-2", method="bank_transfer", label="Other", active=True, account_title="T", account_number="1")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def s3(status=200, kind="image/jpeg", body=PNG):
    def handler(request):
        assert request.url.host.endswith("amazonaws.com") and request.url.path == "/payment-qr/ubl.jpg"
        return httpx.Response(status, headers={"content-type": kind}, content=body)
    return lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)


@pytest.fixture(autouse=True)
def accounts(monkeypatch):
    monkeypatch.setattr(settings, "assets_public_base_url", "https://bucket.s3.ap-southeast-1.amazonaws.com")
    monkeypatch.setattr(payment_accounts, "list_accounts", lambda include_inactive=False: [UBL, NO_QR])


def test_the_checkout_account_lists_its_download_path():
    accounts = service.bank_accounts()
    assert accounts[0].qr_download_url == "/v1/public/payment-qr/acc-1" and accounts[1].qr_download_url is None


def test_the_qr_downloads_as_a_named_file():
    content, kind, name = asyncio.run(service.payment_qr("acc-1", client_factory=s3()))
    assert content == PNG and kind == "image/jpeg" and name == "travelian-united-bank-limited-ubl-qr.jpg"


@pytest.mark.parametrize("account_id, factory, status", [
    ("acc-2", s3(), 404),                        # no QR on that account
    ("nope", s3(), 404),                         # not a checkout account
    ("acc-1", s3(status=403), 502),
    ("acc-1", s3(kind="text/html", body=b"<html>"), 502),   # never anything but an image
    ("acc-1", s3(body=b"0" * (service.QR_MAX_BYTES + 1)), 502),
])
def test_refusals(account_id, factory, status):
    with pytest.raises(service.PublicError) as error:
        asyncio.run(service.payment_qr(account_id, client_factory=factory))
    assert error.value.status == status


def test_the_route_sends_an_attachment():
    async def fake(account_id):
        return PNG, "image/png", "travelian-ubl-qr.png"

    with patch.object(service, "payment_qr", fake):
        response = TestClient(app).get("/v1/public/payment-qr/acc-1")
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-disposition"] == 'attachment; filename="travelian-ubl-qr.png"'
    assert response.headers["content-type"] == "image/png" and response.headers["x-content-type-options"] == "nosniff"


def test_checkout_accounts_carry_the_bank_logo_only_when_uploaded(monkeypatch):
    """travelian#76: the uploaded logo's public URL; no logo -> None (the website shows the bank's initials)."""
    from dataclasses import replace

    monkeypatch.setattr(payment_accounts, "list_accounts", lambda include_inactive=False: [replace(UBL, logo_asset_key="site/bank-logo/ubl.png"), NO_QR])
    accounts = service.bank_accounts()
    assert accounts[0].logo_url == "https://bucket.s3.ap-southeast-1.amazonaws.com/site/bank-logo/ubl.png"
    assert accounts[1].logo_url is None


def test_bank_logo_is_its_own_upload_purpose():
    from app.services import assets

    assert assets.PURPOSES["bank_logo"] == "site/bank-logo"
    assert assets.valid_key("site/bank-logo/abc.png", "bank_logo")
    for bad in ("payment-qr/abc.png", "site/logo.png", "site/bank-logo/../x.png", "bank-logo/abc.png"):
        assert not assets.valid_key(bad, "bank_logo")


def test_admin_routes_refuse_a_logo_key_that_was_not_uploaded_as_a_logo():
    from fastapi import HTTPException

    from app.api.routes import payment_accounts as routes

    assert routes._logo_key(None) is None and routes._logo_key("site/bank-logo/x.webp") == "site/bank-logo/x.webp"
    with pytest.raises(HTTPException) as refused:
        routes._logo_key("payment-qr/x.png")
    assert refused.value.status_code == 422


def test_every_upload_folder_is_writable_and_public_where_needed():
    """Each upload purpose's folder is covered by the gateway's IAM PutObject policy in deploy/aws-setup.sh, and the
    images shown on the website (site logo, QR, bank logo) are under a public-read prefix (travelian#76)."""
    from pathlib import Path

    from app.services import assets

    setup = Path(__file__).resolve().parents[1].joinpath("deploy", "aws-setup.sh").read_text()
    put = setup[setup.index("s3:PutObject"):].split("]}", 1)[0]
    public = setup[:setup.index("s3:PutObject")]
    for purpose, folder in assets.PURPOSES.items():
        top = folder.split("/")[0]
        assert f"$BUCKET/{top}/*" in put, purpose
        if purpose != "payment_proof":
            assert f"$BUCKET/{top}/*" in public, purpose
