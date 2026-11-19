"""Travelian's receiving accounts (migration 0019): where customer payments arrive. Staff choose
one when recording a payment; only active accounts of the payment's method can be chosen.

The account number and IBAN are stored and shown in full (migration 0024, D40): this is Travelian's own business
account that customers pay into, so masking never applies to it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import agencies

METHODS = ("bank_transfer", "jazzcash", "1link")


@dataclass(frozen=True)
class Account:
    id: str
    method: str
    label: str
    active: bool
    bank_name: str | None = None
    account_title: str | None = None
    account_number_masked: str | None = None
    qr_asset_key: str | None = None  # S3 object key of the QR image (the file is in S3, D30/D38)
    account_number: str | None = None  # full, as customers need it to pay (D40)
    iban: str | None = None


def clean_number(number: str | None) -> str | None:
    """The full account number: digits, with the spaces or dashes the bank prints kept between them."""
    value = " ".join((number or "").split()).strip(" -")
    if not value:
        return None
    if not re.fullmatch(r"[0-9][0-9 -]{4,38}[0-9]", value) or sum(ch.isdigit() for ch in value) < 6:
        raise AccountError("Enter the full account number: 6 to 40 digits (spaces or dashes allowed).")
    return value


def clean_iban(iban: str | None) -> str | None:
    """IBAN without spaces, upper case (Pakistan: PK + 2 check digits + 4-letter bank code + 16 digits = 24 characters)."""
    value = "".join((iban or "").split()).upper()
    if not value:
        return None
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", value):
        raise AccountError("Enter a valid IBAN, for example PK36SCBL0000001123456702.")
    return value


def mask_number(number: str | None) -> str | None:
    """The short form for lists ("**** **** 1234"); the full number is kept beside it (D40)."""
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if not digits:
        return None
    if len(digits) < 6:
        raise AccountError("Enter the full account number: 6 to 40 digits (spaces or dashes allowed).")
    return "**** **** " + digits[-4:]


class AccountError(Exception):
    """A refused change. `status` is the HTTP status the admin routes answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


_COLUMNS = "id, method, label, active, bank_name, account_title, account_number_masked, qr_asset_key, account_number, iban"
_SELECT = f"SELECT {_COLUMNS} FROM payment_accounts"


def list_accounts(include_inactive: bool = False) -> list[Account]:
    where = "" if include_inactive else " WHERE active"
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(f"{_SELECT}{where} ORDER BY method, lower(label)")).mappings().all()
    return [Account(**row) for row in rows]


def _clean(value: str | None, field: str, limit: int = 120) -> str | None:
    value = " ".join((value or "").split())
    if value and not 2 <= len(value) <= limit:
        raise AccountError(f"The {field} must be 2 to {limit} characters.")
    return value or None


def add(method: str, label: str, actor_user_id: str | None = None, *, bank_name: str | None = None, account_title: str | None = None,
        account_number: str | None = None, qr_asset_key: str | None = None, iban: str | None = None) -> Account:
    label = " ".join(label.split())
    if method not in METHODS:
        raise AccountError(f"Method must be one of: {', '.join(METHODS)}.")
    if not 2 <= len(label) <= 120:
        raise AccountError("The account name must be 2 to 120 characters.")
    number = clean_number(account_number)
    account = Account(str(uuid4()), method, label, True, _clean(bank_name, "bank name"), _clean(account_title, "account title"),
                      mask_number(number), qr_asset_key or None, number, clean_iban(iban))
    try:
        with agencies.application_engine().begin() as connection:
            connection.execute(text("INSERT INTO payment_accounts (id, method, label, created_by_user_id, bank_name, account_title,"
                                    " account_number_masked, qr_asset_key, account_number, iban)"
                                    " VALUES (:id, :m, :l, :a, :bank, :title, :masked, :qr, :number, :iban)"),
                               {"id": account.id, "m": method, "l": label, "a": actor_user_id, "bank": account.bank_name,
                                "title": account.account_title, "masked": account.account_number_masked, "qr": account.qr_asset_key,
                                "number": account.account_number, "iban": account.iban})
    except IntegrityError:
        raise AccountError("An active account with this name and method already exists.", 409) from None
    return account


def deactivate(account_id: str) -> Account:
    """Stop offering the account for new payments. Past payments keep it (booking_payments.account_id
    and received_into are untouched); accounts are never deleted."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("UPDATE payment_accounts SET active = false, deactivated_at = now() WHERE id = :id AND active "
                                      f"RETURNING {_COLUMNS}"), {"id": account_id}).mappings().one_or_none()
    if not row:
        raise AccountError("No active account with that id.", 404)
    return Account(**row)


def reactivate(account_id: str) -> Account:
    try:
        with agencies.application_engine().begin() as connection:
            row = connection.execute(text("UPDATE payment_accounts SET active = true, deactivated_at = NULL WHERE id = :id AND NOT active "
                                          f"RETURNING {_COLUMNS}"), {"id": account_id}).mappings().one_or_none()
    except IntegrityError:
        raise AccountError("An active account with this name and method already exists.", 409) from None
    if not row:
        raise AccountError("No inactive account with that id.", 404)
    return Account(**row)


def update_details(account_id: str, *, bank_name: str | None, account_title: str | None, account_number: str | None,
                   qr_asset_key: str | None, iban: str | None = None) -> Account:
    """Bank name, title, full number, IBAN and QR image key. A field left out (None) keeps its value."""
    number = clean_number(account_number) if account_number else None
    values = {"bank": _clean(bank_name, "bank name"), "title": _clean(account_title, "account title"),
              "masked": mask_number(number) if number else None, "qr": qr_asset_key or None, "number": number,
              "iban": clean_iban(iban) if iban else None}
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "UPDATE payment_accounts SET bank_name = COALESCE(:bank, bank_name), account_title = COALESCE(:title, account_title),"
            " account_number_masked = COALESCE(:masked, account_number_masked), qr_asset_key = COALESCE(:qr, qr_asset_key),"
            " account_number = COALESCE(:number, account_number), iban = COALESCE(:iban, iban)"
            f" WHERE id = :id RETURNING {_COLUMNS}"), {**values, "id": account_id}).mappings().one_or_none()
    if not row:
        raise AccountError("No account with that id.", 404)
    return Account(**row)


def active_for_payment(connection, account_id: str, method: str) -> Account | None:
    """The account a payment is being recorded into, if it is active and of the payment's method."""
    row = connection.execute(text(f"{_SELECT} WHERE id = :id AND active AND method = :m"), {"id": account_id, "m": method}).mappings().one_or_none()
    return Account(**row) if row else None
