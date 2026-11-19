"""Travelian's receiving accounts (where customer payments arrive).

- GET /admin/payment-accounts: the active accounts, for the Payments tab's "Received into" choice.
  Back office.
- /admin/finance/payment-accounts: Finance > Accounts & Ledgers, "Bank and cash book" tab: every
  account (active and inactive), add, deactivate. FINANCE_ACCOUNT_ROLES only (403 otherwise).
  Same validation and store as app.cli.payment_accounts. Accounts are never deleted.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BackOfficeUser, require_back_office, require_finance_accounts
from app.db import payment_accounts as store
from app.schemas.admin_bookings import (
    AddReceivingAccountRequest, PaymentAccount, PaymentAccountList, ReceivingAccount, ReceivingAccountList,
    UpdateReceivingAccountRequest,
)

router = APIRouter(prefix="/admin/payment-accounts", tags=["admin bookings"],
                   responses={403: {"description": "Caller is not back office."}, 503: {"description": "Database not available."}})

finance_router = APIRouter(prefix="/admin/finance/payment-accounts", tags=["admin finance"],
                           responses={403: {"description": "Caller is not Super Admin (or Finance, once that role exists)."},
                                      503: {"description": "Database not available."}})


@router.get("", response_model=PaymentAccountList)
async def list_payment_accounts(viewer: Annotated[BackOfficeUser, Depends(require_back_office)]) -> PaymentAccountList:
    """The active receiving accounts, by method then name."""
    accounts = await run_in_threadpool(store.list_accounts)
    return PaymentAccountList(accounts=[PaymentAccount(id=a.id, method=a.method, label=a.label) for a in accounts])


def _view(account: store.Account) -> ReceivingAccount:
    from app.services import assets

    return ReceivingAccount(id=account.id, method=account.method, label=account.label, active=account.active,
                            bank_name=account.bank_name, account_title=account.account_title,
                            account_number_masked=account.account_number_masked, account_number=account.account_number, iban=account.iban,
                            qr_asset_key=account.qr_asset_key,
                            qr_url=assets.public_url(account.qr_asset_key))


def _qr_key(key: str | None) -> str | None:
    from app.services import assets

    if key and not assets.valid_key(key, "payment_qr"):
        raise HTTPException(422, "Upload the QR image first (Accounts & Ledgers), then save the account.")
    return key


def _refused(error: store.AccountError) -> HTTPException:
    return HTTPException(error.status, error.message)


@finance_router.get("", response_model=ReceivingAccountList)
async def list_all_payment_accounts(viewer: Annotated[BackOfficeUser, Depends(require_finance_accounts)]) -> ReceivingAccountList:
    """Every receiving account, active and inactive, by method then name."""
    accounts = await run_in_threadpool(store.list_accounts, True)
    return ReceivingAccountList(accounts=[_view(a) for a in accounts])


@finance_router.post("", response_model=ReceivingAccount, status_code=201, responses={
    409: {"description": "An active account with this name and method already exists."},
    422: {"description": "The account name must be 2 to 120 characters."},
})
async def add_payment_account(body: AddReceivingAccountRequest,
                              viewer: Annotated[BackOfficeUser, Depends(require_finance_accounts)]) -> ReceivingAccount:
    """Add an active receiving account. Extra spaces in the name are removed."""
    try:
        account = await run_in_threadpool(lambda: store.add(body.method, body.label, viewer.user_id, bank_name=body.bank_name,
                                                            account_title=body.account_title, account_number=body.account_number,
                                                            qr_asset_key=_qr_key(body.qr_asset_key), iban=body.iban))
    except store.AccountError as error:
        raise _refused(error) from None
    return _view(account)


@finance_router.post("/{account_id}/deactivate", response_model=ReceivingAccount, responses={
    404: {"description": "No active account with that id."},
})
async def deactivate_payment_account(account_id: str,
                                     viewer: Annotated[BackOfficeUser, Depends(require_finance_accounts)]) -> ReceivingAccount:
    """Stop offering the account for new payments. Payments already recorded keep it."""
    try:
        account = await run_in_threadpool(store.deactivate, account_id)
    except store.AccountError as error:
        raise _refused(error) from None
    return _view(account)


@finance_router.patch("/{account_id}", response_model=ReceivingAccount, responses={404: {"description": "No account with that id."}})
async def update_payment_account(account_id: str, body: UpdateReceivingAccountRequest,
                                 viewer: Annotated[BackOfficeUser, Depends(require_finance_accounts)]) -> ReceivingAccount:
    """Bank name, account title, full account number, IBAN and QR image. Fields left out keep their value; remove_qr clears
    the QR image."""
    try:
        account = await run_in_threadpool(lambda: store.update_details(
            account_id, bank_name=body.bank_name, account_title=body.account_title, account_number=body.account_number,
            qr_asset_key=_qr_key(body.qr_asset_key), iban=body.iban, remove_qr=body.remove_qr))
    except store.AccountError as error:
        raise _refused(error) from None
    return _view(account)


@finance_router.post("/{account_id}/activate", response_model=ReceivingAccount, responses={404: {"description": "No inactive account with that id."}})
async def activate_payment_account(account_id: str,
                                   viewer: Annotated[BackOfficeUser, Depends(require_finance_accounts)]) -> ReceivingAccount:
    """Offer the account again (the Active toggle)."""
    try:
        account = await run_in_threadpool(store.reactivate, account_id)
    except store.AccountError as error:
        raise _refused(error) from None
    return _view(account)
