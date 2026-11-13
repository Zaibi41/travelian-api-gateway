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
    """A refused change. `status` is the HTTP status the admin routes answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


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
        raise AccountError("An active account with this name and method already exists.", 409) from None
    return account


def deactivate(account_id: str) -> Account:
    """Stop offering the account for new payments. Past payments keep it (booking_payments.account_id
    and received_into are untouched); accounts are never deleted."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("UPDATE payment_accounts SET active = false, deactivated_at = now() WHERE id = :id AND active "
                                      "RETURNING id, method, label, active"), {"id": account_id}).mappings().one_or_none()
    if not row:
        raise AccountError("No active account with that id.", 404)
    return Account(**row)


def active_for_payment(connection, account_id: str, method: str) -> Account | None:
    """The account a payment is being recorded into, if it is active and of the payment's method."""
    row = connection.execute(text(f"{_SELECT} WHERE id = :id AND active AND method = :m"), {"id": account_id, "m": method}).mappings().one_or_none()
    return Account(**row) if row else None
