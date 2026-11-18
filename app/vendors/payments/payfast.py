"""PayFast (Pakistan) hosted checkout.

Contract: the PayFast Postman collection in the frontend repo (docs/postman/PayFast.postman_collection.json, not
committed: it holds a merchant ID) and the Merchant Integration Guide v2.3 as cited by the paygate_pk library.
- Token: POST {base}/Ecommerce/api/Transaction/GetAccessToken, form MERCHANT_ID, SECURED_KEY, BASKET_ID, TXNAMT,
  CURRENCY_CODE -> JSON with ACCESS_TOKEN. Server to server: the secured key never leaves the gateway.
- Checkout: the customer's browser posts a form to {base}/Ecommerce/api/Transaction/PostTransaction with the
  token and the order (fields below). SIGNATURE and VERSION are free strings, not cryptographic.
- Result: PayFast sends basket_id, err_code ("000" = paid), transaction_id, transaction_amount and validation_hash
  to SUCCESS_URL / FAILURE_URL (the customer's browser) and to CHECKOUT_URL (its server notification).
  validation_hash = SHA-256 hex of "basket_id|secured_key|merchant_id|err_code". Only the gateway can compute it.
The amount is not in the hash: callers also compare transaction_amount with what the gateway asked for.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

TOKEN_PATH = "/Ecommerce/api/Transaction/GetAccessToken"
POST_PATH = "/Ecommerce/api/Transaction/PostTransaction"
PAID = "000"


class PayFastError(Exception):
    def __init__(self, code: str, message: str = "Online payment isn't available right now."):
        super().__init__(code)
        self.code = code
        self.message = message


def configured() -> bool:
    return bool(settings.payfast_merchant_id and settings.payfast_secured_key)


def sandbox() -> bool:
    return "ipguat." in settings.payfast_base_url


def _merchant_id() -> str:
    if not configured():
        raise PayFastError("not_configured")
    return settings.payfast_merchant_id.get_secret_value()  # type: ignore[union-attr]


def _secured_key() -> str:
    if not configured():
        raise PayFastError("not_configured")
    return settings.payfast_secured_key.get_secret_value()  # type: ignore[union-attr]


def amount_text(amount_minor: int) -> str:
    """Minor units to PayFast's TXNAMT: rupees with two decimals ("13997.00")."""
    whole, paisa = divmod(amount_minor, 100)
    return f"{whole}.{paisa:02d}"


def parse_amount_minor(value: object) -> int | None:
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * 100).to_integral_value())


def new_basket_id(reference: str) -> str:
    """Our booking reference plus a random suffix: unique per attempt, at most 48 characters, letters/digits/dash."""
    clean = "".join(ch for ch in reference.upper() if ch.isalnum() or ch == "-")[:30]
    return f"{clean}-{secrets.token_hex(4).upper()}"


async def access_token(basket_id: str, amount_minor: int, currency: str = "PKR", client: httpx.AsyncClient | None = None) -> str:
    form = {"MERCHANT_ID": _merchant_id(), "SECURED_KEY": _secured_key(), "BASKET_ID": basket_id,
            "TXNAMT": amount_text(amount_minor), "CURRENCY_CODE": currency}
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.payfast_timeout_seconds)
    try:
        response = await client.post(settings.payfast_base_url + TOKEN_PATH, data=form,
                                     headers={"User-Agent": "Travelian/1.0", "Accept": "application/json"})
    except httpx.HTTPError as error:
        logger.warning("PayFast token request failed", extra={"error_type": type(error).__name__})
        raise PayFastError("unreachable") from None
    finally:
        if own:
            await client.aclose()
    try:
        body = response.json()
    except ValueError:
        body = None
    token = body.get("ACCESS_TOKEN") if isinstance(body, dict) else None
    if response.status_code != 200 or not isinstance(token, str) or not token:
        logger.warning("PayFast token refused", extra={"status": response.status_code})
        raise PayFastError("token_refused")
    return token


@dataclass(frozen=True)
class CheckoutForm:
    action: str
    fields: dict[str, str]


def checkout_form(*, token: str, basket_id: str, amount_minor: int, currency: str, description: str, mobile: str,
                  email: str, success_url: str, failure_url: str, notify_url: str, order_date: datetime) -> CheckoutForm:
    """The form the customer's browser posts to PayFast. Holds no secret (the token is single-use for this basket)."""
    digits = "".join(ch for ch in mobile if ch.isdigit())
    if digits.startswith("92"):
        digits = "0" + digits[2:]
    fields = {
        "MERCHANT_ID": _merchant_id(),
        "MERCHANT_NAME": settings.payfast_merchant_name,
        "TOKEN": token,
        "PROCCODE": "00",
        "TXNAMT": amount_text(amount_minor),
        "BASKET_ID": basket_id,
        "ORDER_DATE": order_date.strftime("%Y-%m-%d"),
        "CUSTOMER_MOBILE_NO": digits[:15],
        "CUSTOMER_EMAIL_ADDRESS": email,
        "SIGNATURE": secrets.token_hex(8),
        "VERSION": "TRAVELIAN-1.0",
        "TXNDESC": description[:100],
        "SUCCESS_URL": success_url,
        "FAILURE_URL": failure_url,
        "CHECKOUT_URL": notify_url,
        "CURRENCY_CODE": currency,
        "TRAN_TYPE": "ECOMM_PURCHASE",
    }
    return CheckoutForm(action=settings.payfast_base_url + POST_PATH, fields=fields)


def expected_hash(basket_id: str, err_code: str) -> str:
    data = f"{basket_id}|{_secured_key()}|{_merchant_id()}|{err_code}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Result:
    basket_id: str
    err_code: str
    paid: bool
    transaction_id: str | None
    amount_minor: int | None
    message: str | None


def verify(params: dict[str, str]) -> Result | None:
    """PayFast's result if its validation_hash is genuine, else None. Keys are matched case-insensitively."""
    if not configured():
        return None
    lowered = {str(k).lower(): ("" if v is None else str(v)) for k, v in params.items()}
    basket_id, err_code, given = lowered.get("basket_id", ""), lowered.get("err_code", ""), lowered.get("validation_hash", "")
    if not basket_id or not err_code or not given:
        return None
    if not hmac.compare_digest(expected_hash(basket_id, err_code).lower(), given.strip().lower()):
        return None
    return Result(basket_id=basket_id, err_code=err_code, paid=err_code == PAID,
                  transaction_id=(lowered.get("transaction_id") or None),
                  amount_minor=parse_amount_minor(lowered.get("transaction_amount")) if lowered.get("transaction_amount") else None,
                  message=(lowered.get("err_msg") or None))
