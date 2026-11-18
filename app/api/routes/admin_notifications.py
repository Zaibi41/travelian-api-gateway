"""Staff notifications (holds, booking requests, restricted-airline requests). Back-office roles only."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BackOfficeUser, require_back_office
from app.services import notifications

router = APIRouter(prefix="/admin/notifications", tags=["admin notifications"])


class NotificationView(BaseModel):
    id: str
    kind: str
    reference: str | None = None
    title: str
    details: dict[str, Any]
    emailed: bool
    created_at: datetime


class NotificationList(BaseModel):
    items: list[NotificationView]


@router.get("", response_model=NotificationList)
async def list_notifications(viewer: Annotated[BackOfficeUser, Depends(require_back_office)],
                             limit: Annotated[int, Query(ge=1, le=200)] = 50) -> NotificationList:
    """Newest first."""
    rows = await run_in_threadpool(notifications.recent, limit)
    return NotificationList(items=[NotificationView(**row) for row in rows])
