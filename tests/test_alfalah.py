"""Bank Alfalah (Alfa Payment Gateway): request hash, handshake, checkout form, order status checks, and that a payment
is recorded only from the gateway's own status inquiry ("Paid", this merchant and order). Mocked APG; no database."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.main import app
from app.services import public_booking
from app.vendors.payments import alfalah

client = TestClient(app)
KEY1, KEY2 = "AbCdEfGh12345678", "ZyXwVuTs87654321"
CREDENTIALS = {"alfalah_merchant_id": "12345", "alfalah_store_id": "024321", "alfalah_merchant_hash": "hash==",
               "alfalah_merchant_username": "user", "alfalah_merchant_password": "pass", "alfalah_key1": KEY1, "alfalah_key2": KEY2}


@pytest.fixture
def alfalah_on():
    patches = [patch.object(settings, name, SecretStr(value)) for name, value in CREDENTIALS.items()]
    patches.append(patch.object(settings, "alfalah_base_url", "https://sandbox.bankalfalah.com"))
    patches.append(patch.object(settings, "alfalah_allow_sandbox", True))
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


@pytest.fixture(autouse=True)
def fresh_limits():
    public_booking.PAY_LIMIT._hits.clear()
    yield


def decrypt(hash_text: str) -> str:
    decryptor = Cipher(algorithms.AES(KEY1.encode()), modes.CBC(KEY2.encode())).decryptor()
    data = decryptor.update(base64.b64decode(hash_text)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(data) + unpadder.finalize()).decode()


def status_body(order: str, status: str = "Paid", amount: str = "13997", merchant: str = "12345", store: str = "024321") -> str:
    # APG answers JSON inside a JSON string.
    return json.dumps(json.dumps({"ResponseCode": "00", "Description": "Success", "MerchantId": merchant, "StoreId": store,
                                  "TransactionReferenceNumber": order, "TransactionId": "1263781929", "TransactionAmount": amount,
                                  "TransactionStatus": status}))


# ---------- vendor ----------

def test_off_without_credentials():
    assert not alfalah.configured() and not alfalah.payments_allowed()
    assert alfalah.mode() == "not configured"


def test_sandbox_needs_the_switch(alfalah_on):
    assert alfalah.configured() and alfalah.sandbox() and alfalah.payments_allowed()
    with patch.object(settings, "alfalah_allow_sandbox", False):
        assert not alfalah.payments_allowed()
    with patch.object(settings, "alfalah_base_url", "https://payments.bankalfalah.com"), patch.object(settings, "alfalah_allow_sandbox", False):
        assert alfalah.production() and alfalah.payments_allowed()


def test_request_hash_is_aes_cbc_of_the_fields_in_order(alfalah_on):
    pairs = [("HS_MerchantId", "12345"), ("HS_StoreId", "024321"), ("HS_ReturnURL", "https://travelian.pk/payment/alfalah")]
    assert decrypt(alfalah.request_hash(pairs)) == "HS_MerchantId=12345&HS_StoreId=024321&HS_ReturnURL=https://travelian.pk/payment/alfalah"
    with patch.object(settings, "alfalah_key1", SecretStr("short")):
        with pytest.raises(alfalah.AlfalahError):
            alfalah.request_hash(pairs)


def test_amounts_and_order_ids():
    assert alfalah.amount_text(1399700) == "13997" and alfalah.amount_text(1399750) == "13997.50"
    assert alfalah.parse_amount_minor("13,997.5") == 1399750 and alfalah.parse_amount_minor("x") is None
    order = alfalah.new_order_id("TRV-F-2026-ABCDEF")
    assert order.startswith("TRVF2026ABCDEF") and order.isalnum() and len(order) <= 40
    assert alfalah.order_id_from({"O": order}) == order
    assert alfalah.order_id_from({"url": f"https://sandbox.bankalfalah.com/HS/api/IPN/OrderStatus/12345/024321/{order}"}) == order
    assert alfalah.order_id_from({"path": f"/payment/alfalah/TS=P/RC=00/RD=/O={order}"}) == order
    assert alfalah.order_id_from({"O": "../../etc"}) is None and alfalah.order_id_from({}) is None


def test_handshake_and_checkout_form(alfalah_on):
    async def run():
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["form"] = dict(httpx.QueryParams(request.content.decode()))
            return httpx.Response(200, json={"success": "true", "AuthToken": "TOKEN/abc=", "ReturnURL": "x", "ErrorMessage": ""})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            token = await alfalah.handshake("TRV1ABCD", "https://travelian.pk/payment/alfalah", client=http)
        assert token == "TOKEN/abc=" and seen["url"] == "https://sandbox.bankalfalah.com/HS/HS/HS"
        assert seen["form"]["HS_ChannelId"] == "1001" and seen["form"]["HS_TransactionReferenceNumber"] == "TRV1ABCD"
        assert decrypt(seen["form"]["HS_RequestHash"]) == (
            "HS_MerchantId=12345&HS_StoreId=024321&HS_ChannelId=1001&HS_MerchantHash=hash==&HS_MerchantUsername=user"
            "&HS_MerchantPassword=pass&HS_IsRedirectionRequest=0&HS_ReturnURL=https://travelian.pk/payment/alfalah"
            "&HS_TransactionReferenceNumber=TRV1ABCD")

        form = alfalah.checkout_form(auth_token=token, order_id="TRV1ABCD", amount_minor=1399700, return_url="https://travelian.pk/payment/alfalah")
        assert form.action == "https://sandbox.bankalfalah.com/SSO/SSO/SSO"
        assert form.fields["TransactionAmount"] == "13997" and form.fields["TransactionTypeId"] == "3" and form.fields["AuthToken"] == token
        assert decrypt(form.fields["RequestHash"]).endswith("&TransactionAmount=13997&AuthToken=TOKEN/abc=")
        assert KEY1 not in json.dumps(form.fields) and KEY2 not in json.dumps(form.fields)  # the keys never reach the browser
    asyncio.run(run())


def test_handshake_refused(alfalah_on):
    async def run():
        refuse = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": "false", "AuthToken": None, "ErrorMessage": "Invalid Request"}))
        async with httpx.AsyncClient(transport=refuse) as http:
            with pytest.raises(alfalah.AlfalahError):
                await alfalah.handshake("TRV1ABCD", "https://travelian.pk/payment/alfalah", client=http)
    asyncio.run(run())


def test_order_status_only_for_this_merchant_and_order(alfalah_on):
    async def run():
        async def status(body: str):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body,
                                                                                               headers={"content-type": "application/json"}))) as http:
                return await alfalah.order_status("TRV1ABCD", client=http)

        paid = await status(status_body("TRV1ABCD"))
        assert paid.paid and paid.amount_minor == 1399700 and paid.transaction_id == "1263781929"
        assert (await status(status_body("TRV1ABCD", status="Failed"))).paid is False
        assert await status(status_body("OTHER123")) is None  # another order
        assert await status(status_body("TRV1ABCD", merchant="999")) is None  # another merchant
        assert await status(status_body("TRV1ABCD", store="000001")) is None  # another store
    asyncio.run(run())


# ---------- service and routes ----------

def test_return_records_only_what_apg_confirms(alfalah_on):
    recorded = []

    def record(basket, **kw):
        recorded.append((basket, kw))
        return public_booking.public_bookings.Outcome("TRV-F-1", "paid", "paid", "flight")

    paid = alfalah.Result(order_id="TRV1ABCD", paid=True, status="Paid", transaction_id="T1", amount_minor=1399700, message=None)
    with patch.object(public_booking.public_bookings, "record_result", record), \
            patch.object(alfalah, "order_status", AsyncMock(return_value=paid)), \
            patch("app.services.flight_payments.start_after_verified_card_payment") as issue:
        answer = client.post("/v1/public/payments/alfalah/return", json={"O": "TRV1ABCD", "RC": "00", "TS": "P"})
        notify = client.post("/v1/public/payments/alfalah/notify?url=https://evil.example/HS/api/IPN/OrderStatus/1/2/TRV1ABCD")
    assert answer.json() == {"reference": "TRV-F-1", "result": "paid"} and notify.json() == {"status": "ok"}
    basket, kw = recorded[0]
    assert basket == "TRV1ABCD" and kw["provider"] == "alfalah" and kw["amount_minor"] == 1399700 and kw["test_money"] is True
    issue.assert_not_called()  # sandbox money never starts automatic issuing


def test_not_paid_or_unverified_changes_nothing(alfalah_on):
    unpaid = alfalah.Result(order_id="TRV1ABCD", paid=False, status="Pending", transaction_id=None, amount_minor=None, message=None)
    with patch.object(public_booking.public_bookings, "record_result") as record, \
            patch.object(public_booking.public_bookings, "attempt_by_basket", lambda b: None), \
            patch.object(alfalah, "order_status", AsyncMock(side_effect=[unpaid, None])):
        not_paid = client.post("/v1/public/payments/alfalah/return", json={"O": "TRV1ABCD"})
        unverified = client.post("/v1/public/payments/alfalah/return", json={"O": "TRV1ABCD"})
        no_order = client.post("/v1/public/payments/alfalah/return", json={"RC": "00"})
    record.assert_not_called()
    assert not_paid.json()["result"] == "not_paid"
    assert unverified.json()["result"] == "not_verified" and no_order.json()["result"] == "not_verified"


def test_payment_methods_offer_alfalah_only_when_allowed(alfalah_on):
    assert client.get("/v1/public/payment-methods").json()["alfalah"] is True
    with patch.object(settings, "alfalah_allow_sandbox", False):
        assert client.get("/v1/public/payment-methods").json()["alfalah"] is False
