"""Bank Alfalah — Alfa Payment Gateway (APG), page redirection (card, Alfa wallet, Alfalah account on APG's own page).

Contract: "Alfa Payment Gateway Merchant Integration Guide V 1.1" (merchants.bankalfalah.com), plus the field and hash
order of the bank's page-redirection sample (as used by the community packages naeemz/alfapay and react-alfa-payment).
- Handshake: POST {base}/HS/HS/HS, form HS_MerchantId, HS_StoreId, HS_ChannelId (1001: page redirection),
  HS_MerchantHash, HS_MerchantUsername, HS_MerchantPassword, HS_IsRedirectionRequest, HS_ReturnURL,
  HS_TransactionReferenceNumber and HS_RequestHash -> JSON {"success": "true", "AuthToken": ..., "ErrorMessage": ...}.
  Server to server here: Key1 / Key2 never leave the gateway.
- Checkout: the customer's browser posts AuthToken, RequestHash and the order to {base}/SSO/SSO/SSO, APG's own page.
- RequestHash: AES-128-CBC (key = Key1, IV = Key2, PKCS7), Base64, of "Name=value&Name=value…" in the order below,
  without RequestHash itself; values as sent (not URL-encoded). The checkout hash adds AuthToken last.
- Result: APG sends the customer back to the return URL with the order id ("O"), and calls our listener with
  url=…/HS/api/IPN/OrderStatus/{MerchantId}/{StoreId}/{OrderId}. Neither is trusted: the gateway itself asks
  GET {base}/HS/api/IPN/OrderStatus/{MerchantId}/{StoreId}/{OrderId} and records only TransactionStatus "Paid" for this
  merchant, store and order, for the amount the gateway asked for.

Note (APG's design): the checkout form carries the merchant hash, username and password to the customer's browser.
The encryption keys (Key1, Key2) do not.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.config import settings

logger = logging.getLogger(__name__)

HANDSHAKE_PATH = "/HS/HS/HS"
CHECKOUT_PATH = "/SSO/SSO/SSO"
STATUS_PATH = "/HS/api/IPN/OrderStatus"
CHANNEL_PAGE_REDIRECTION = "1001"
TRANSACTION_TYPE_CARD = "3"
LABEL = "Bank Alfalah"
_ORDER_ID = re.compile(r"^[A-Z0-9]{6,40}$")


class AlfalahError(Exception):
    def __init__(self, code: str, message: str = "Online payment isn't available right now."):
        super().__init__(code)
        self.code = code
        self.message = message


def _secret(name: str) -> str:
    value = getattr(settings, name)
    if value is None:
        raise AlfalahError("not_configured")
    text = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    if not text:
        raise AlfalahError("not_configured")
    return text


_REQUIRED = ("alfalah_merchant_id", "alfalah_store_id", "alfalah_merchant_hash", "alfalah_merchant_username",
             "alfalah_merchant_password", "alfalah_key1", "alfalah_key2")


def configured() -> bool:
    try:
        return all(_secret(name) for name in _REQUIRED)
    except AlfalahError:
        return False


def sandbox() -> bool:
    return "sandbox." in settings.alfalah_base_url


def production() -> bool:
    return configured() and not sandbox()


def payments_allowed() -> bool:
    """Offered at checkout on production APG, or on the sandbox with ALFALAH_ALLOW_SANDBOX (test money)."""
    return production() or (configured() and sandbox() and settings.alfalah_allow_sandbox)


def mode() -> str:
    if not configured():
        return "not configured"
    if sandbox():
        return "sandbox (allowed for testing)" if settings.alfalah_allow_sandbox else "sandbox"
    return "production"


def request_hash(pairs: list[tuple[str, str]]) -> str:
    """APG's RequestHash of the fields in this order."""
    key, iv = _secret("alfalah_key1").encode("utf-8"), _secret("alfalah_key2").encode("utf-8")
    if len(key) != 16 or len(iv) != 16:
        raise AlfalahError("bad_keys")
    text = "&".join(f"{name}={value}" for name, value in pairs).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    data = padder.update(text) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("ascii")


def amount_text(amount_minor: int) -> str:
    """Minor units to APG's TransactionAmount: whole rupees ("13997"), or with paisa ("13997.50")."""
    whole, paisa = divmod(amount_minor, 100)
    return str(whole) if paisa == 0 else f"{whole}.{paisa:02d}"


def parse_amount_minor(value: object) -> int | None:
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * 100).to_integral_value())


def new_order_id(reference: str) -> str:
    """Our booking reference (letters and digits only) plus a random suffix: unique per attempt, at most 40 characters."""
    clean = "".join(ch for ch in reference.upper() if ch.isalnum())[:30]
    return f"{clean}{secrets.token_hex(4).upper()}"


def clean_order_id(value: object) -> str | None:
    text = str(value or "").strip().upper()
    return text if _ORDER_ID.fullmatch(text) else None


def order_id_from(params: dict[str, str]) -> str | None:
    """The order id from APG's return (O=…, as a query parameter or in the path) or its listener call (url=…/{order})."""
    lowered = {str(k).lower(): ("" if v is None else str(v)) for k, v in params.items()}
    for key in ("o", "order", "orderid", "transactionreferencenumber"):
        if lowered.get(key):
            return clean_order_id(lowered[key])
    url = lowered.get("url", "")
    if url:
        return clean_order_id(url.rstrip("/").rsplit("/", 1)[-1])
    path = lowered.get("path", "")
    match = re.search(r"(?:^|/)O=([A-Za-z0-9]+)", path)
    return clean_order_id(match.group(1)) if match else None


def _credentials() -> dict[str, str]:
    return {"merchant_id": _secret("alfalah_merchant_id"), "store_id": _secret("alfalah_store_id"),
            "merchant_hash": _secret("alfalah_merchant_hash"), "username": _secret("alfalah_merchant_username"),
            "password": _secret("alfalah_merchant_password")}


async def handshake(order_id: str, return_url: str, client: httpx.AsyncClient | None = None) -> str:
    """The AuthToken for this order (server to server)."""
    c = _credentials()
    pairs = [
        ("HS_MerchantId", c["merchant_id"]), ("HS_StoreId", c["store_id"]), ("HS_ChannelId", CHANNEL_PAGE_REDIRECTION),
        ("HS_MerchantHash", c["merchant_hash"]), ("HS_MerchantUsername", c["username"]),
        ("HS_MerchantPassword", c["password"]), ("HS_IsRedirectionRequest", "0"), ("HS_ReturnURL", return_url),
        ("HS_TransactionReferenceNumber", order_id),
    ]
    form = dict(pairs) | {"HS_RequestHash": request_hash(pairs)}
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.alfalah_timeout_seconds)
    try:
        response = await client.post(settings.alfalah_base_url + HANDSHAKE_PATH, data=form,
                                     headers={"User-Agent": "Travelian/1.0", "Accept": "application/json"})
    except httpx.HTTPError as error:
        logger.warning("Alfalah handshake failed", extra={"error_type": type(error).__name__})
        raise AlfalahError("unreachable") from None
    finally:
        if own:
            await client.aclose()
    body = _json(response)
    token = body.get("AuthToken") if isinstance(body, dict) else None
    if response.status_code != 200 or str(body.get("success") if isinstance(body, dict) else "").lower() != "true" or not token:
        logger.warning("Alfalah handshake refused", extra={"status": response.status_code,
                                                           "error": (body.get("ErrorMessage") if isinstance(body, dict) else None)})
        raise AlfalahError("handshake_refused")
    return str(token)


@dataclass(frozen=True)
class CheckoutForm:
    action: str
    fields: dict[str, str]


def checkout_form(*, auth_token: str, order_id: str, amount_minor: int, return_url: str) -> CheckoutForm:
    """The form the customer's browser posts to APG's page (card payment)."""
    c = _credentials()
    pairs = [
        ("ChannelId", CHANNEL_PAGE_REDIRECTION), ("Currency", "PKR"), ("IsBIN", "0"), ("ReturnURL", return_url),
        ("MerchantId", c["merchant_id"]), ("StoreId", c["store_id"]), ("MerchantHash", c["merchant_hash"]),
        ("MerchantUsername", c["username"]), ("MerchantPassword", c["password"]),
        ("TransactionTypeId", TRANSACTION_TYPE_CARD), ("TransactionReferenceNumber", order_id),
        ("TransactionAmount", amount_text(amount_minor)),
    ]
    fields = {"AuthToken": auth_token, "RequestHash": request_hash([*pairs, ("AuthToken", auth_token)]), **dict(pairs)}
    return CheckoutForm(action=settings.alfalah_base_url + CHECKOUT_PATH, fields=fields)


@dataclass(frozen=True)
class Result:
    order_id: str
    paid: bool
    status: str
    transaction_id: str | None
    amount_minor: int | None
    message: str | None


def _json(response: httpx.Response) -> object:
    """APG answers JSON, sometimes JSON inside a JSON string."""
    try:
        body: object = response.json()
    except ValueError:
        return {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return {}
    return body


async def order_status(order_id: str, client: httpx.AsyncClient | None = None) -> Result | None:
    """Ask APG for this order's status. None when APG can't be reached or the answer isn't for this merchant and order."""
    order = clean_order_id(order_id)
    if order is None or not configured():
        return None
    c = _credentials()
    url = f"{settings.alfalah_base_url}{STATUS_PATH}/{quote(c['merchant_id'], safe='')}/{quote(c['store_id'], safe='')}/{order}"
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.alfalah_timeout_seconds)
    try:
        response = await client.get(url, headers={"User-Agent": "Travelian/1.0", "Accept": "application/json"})
    except httpx.HTTPError as error:
        logger.warning("Alfalah status request failed", extra={"error_type": type(error).__name__})
        return None
    finally:
        if own:
            await client.aclose()
    body = _json(response)
    if response.status_code != 200 or not isinstance(body, dict):
        logger.warning("Alfalah status refused", extra={"status": response.status_code})
        return None
    given = {str(k).lower(): ("" if v is None else str(v)) for k, v in body.items()}
    if (given.get("transactionreferencenumber", "").strip().upper() != order
            or given.get("merchantid", "").strip() != c["merchant_id"] or given.get("storeid", "").strip() != c["store_id"]):
        logger.warning("Alfalah status for another merchant, store or order ignored")
        return None
    status = given.get("transactionstatus", "").strip()
    return Result(order_id=order, paid=status.lower() == "paid", status=status or "unknown",
                  transaction_id=(given.get("transactionid") or None),
                  amount_minor=parse_amount_minor(given["transactionamount"]) if given.get("transactionamount") else None,
                  message=(given.get("description") or None))
