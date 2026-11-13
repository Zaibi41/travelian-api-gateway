"""Travelian's receiving accounts (migration 0019): where customer payments arrive. Staff choose
one when recording a payment; only active accounts of the payment's method can be chosen."""

from __future__ import annotations

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


class AccountError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


_SELECT = "SELECT id, method, label, active FROM payment_accounts"


def list_accounts(include_inactive: bool = False) -> list[Account]:
    where = "" if include_inactive else " WHERE active"
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(f"{_SELECT}{where} ORDER BY method, lower(label)")).mappings().all()
    return [Account(**row) for row in rows]


def add(method: str, label: str, actor_user_id: str | None = None) -> Account:
    label = " ".join(label.split())
    if method not in METHODS:
        raise AccountError(f"Method must be one of: {', '.join(METHODS)}.")
    if not 2 <= len(label) <= 120:
        raise AccountError("The account name must be 2 to 120 characters.")
    account = Account(str(uuid4()), method, label, True)
    try:
        with agencies.application_engine().begin() as connection:
            connection.execute(text("INSERT INTO payment_accounts (id, method, label, created_by_user_id) VALUES (:id, :m, :l, :a)"),
                               {"id": account.id, "m": method, "l": label, "a": actor_user_id})
    except IntegrityError:
        raise AccountError("An active account with this name and method already exists.") from None
    return account


def deactivate(account_id: str) -> None:
    with agencies.application_engine().begin() as connection:
        changed = connection.execute(text("UPDATE payment_accounts SET active = false, deactivated_at = now() WHERE id = :id AND active"),
                                     {"id": account_id}).rowcount
    if not changed:
        raise AccountError("No active account with that id.")


def active_for_payment(connection, account_id: str, method: str) -> Account | None:
    """The account a payment is being recorded into, if it is active and of the payment's method."""
    row = connection.execute(text(f"{_SELECT} WHERE id = :id AND active AND method = :m"), {"id": account_id, "m": method}).mappings().one_or_none()
    return Account(**row) if row else None
