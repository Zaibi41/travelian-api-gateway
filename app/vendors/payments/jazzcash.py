"""JazzCash mobile wallet: SCAFFOLD ONLY (B4, D31), off by default (JAZZCASH_ENABLED=false).

The shape follows JazzCash's hosted "page redirection" checkout as generally published (pp_* fields; a pp_SecureHash
HMAC-SHA256 over the sorted non-empty pp_ values, keyed with the integrity salt). It has NOT been checked against
JazzCash's merchant guide or a sandbox: confirm every field, the hash rule and the callback before enabling. Nothing
here is reachable by customers while the flag is off, and no payment is ever recorded from it until verify() is
confirmed against the real guide.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.config import settings

CHECKOUT_PATH = "/CustomerPortal/transactionmanagement/merchantform/"
PAID = "000"


def enabled() -> bool:
    return bool(settings.jazzcash_enabled and settings.jazzcash_merchant_id and settings.jazzcash_password and settings.jazzcash_integrity_salt)


def secure_hash(fields: dict[str, str]) -> str:
    salt = settings.jazzcash_integrity_salt.get_secret_value()  # type: ignore[union-attr]
    values = [str(fields[key]) for key in sorted(fields) if key.startswith("pp_") and key != "pp_SecureHash" and str(fields[key]) != ""]
    message = "&".join([salt, *values])
    return hmac.new(salt.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


@dataclass(frozen=True)
class Checkout:
    action: str
    fields: dict[str, str]


def checkout(*, reference: str, txn_ref: str, amount_minor: int, return_url: str, now: datetime) -> Checkout:
    if not enabled():
        raise RuntimeError("JazzCash is not enabled")
    fields = {
        "pp_Version": "1.1", "pp_TxnType": "MWALLET", "pp_Language": "EN",
        "pp_MerchantID": settings.jazzcash_merchant_id.get_secret_value(),  # type: ignore[union-attr]
        "pp_Password": settings.jazzcash_password.get_secret_value(),  # type: ignore[union-attr]
        "pp_TxnRefNo": txn_ref, "pp_Amount": str(amount_minor), "pp_TxnCurrency": "PKR",
        "pp_TxnDateTime": now.strftime("%Y%m%d%H%M%S"), "pp_TxnExpiryDateTime": (now + timedelta(minutes=30)).strftime("%Y%m%d%H%M%S"),
        "pp_BillReference": reference, "pp_Description": f"Travelian booking {reference}", "pp_ReturnURL": return_url,
    }
    fields["pp_SecureHash"] = secure_hash(fields)
    return Checkout(action=settings.jazzcash_base_url.rstrip("/") + CHECKOUT_PATH, fields=fields)


def verify(params: dict[str, str]) -> bool:
    """A callback is genuine only when its pp_SecureHash matches. Unconfirmed against the real guide: not used to record
    payments while JazzCash is scaffolded."""
    if not enabled() or not params.get("pp_SecureHash"):
        return False
    return hmac.compare_digest(secure_hash(params), str(params["pp_SecureHash"]).upper())
