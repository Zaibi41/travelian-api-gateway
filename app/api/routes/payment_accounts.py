"""Travelian's receiving accounts, for the Payments tab's "Received into" choice. Back office only.
Managed with app.cli.payment_accounts (no admin screen yet)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BackOfficeUser, require_back_office
from app.db import payment_accounts as store
from app.schemas.admin_bookings import PaymentAccount, PaymentAccountList

router = APIRouter(prefix="/admin/payment-accounts", tags=["admin bookings"],
                   responses={403: {"description": "Caller is not back office."}, 503: {"description": "Database not available."}})


@router.get("", response_model=PaymentAccountList)
async def list_payment_accounts(viewer: Annotated[BackOfficeUser, Depends(require_back_office)]) -> PaymentAccountList:
    """The active receiving accounts, by method then name."""
    accounts = await run_in_threadpool(store.list_accounts)
    return PaymentAccountList(accounts=[PaymentAccount(id=a.id, method=a.method, label=a.label) for a in accounts])
