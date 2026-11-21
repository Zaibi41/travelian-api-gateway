"""Staff notifications (holds, booking requests, restricted-airline requests, red alerts). Back-office roles only."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BackOfficeUser, require_back_office
from app.services import notifications
from app.services.admin_bookings import airline_of

router = APIRouter(prefix="/admin/notifications", tags=["admin notifications"])


class NotificationView(BaseModel):
    id: str
    kind: str
    reference: str | None = None
    title: str
    details: dict[str, Any]
    emailed: bool
    created_at: datetime
    urgent: bool = Field(False, description="A red alert: a customer paid by card and has no ticket. Shown first, in red.")
    airline_code: str | None = Field(None, description="The booking's first airline (IATA), for the logo (#75).")
    airline_name: str | None = None
    flight_number: str | None = None


class NotificationList(BaseModel):
    items: list[NotificationView]
    email_enabled: bool = Field(description="Alerts are also emailed to staff. False until email is set up on the server "
                                            "(deploy/aws-setup.sh: SES sender and recipients); alerts still appear here.")


@router.get("", response_model=NotificationList)
async def list_notifications(viewer: Annotated[BackOfficeUser, Depends(require_back_office)],
                             limit: Annotated[int, Query(ge=1, le=200)] = 50) -> NotificationList:
    """Newest first."""
    rows = await run_in_threadpool(notifications.recent, limit)
    items = []
    for row in rows:
        details = dict(row["details"] or {})
        if not viewer.visibility.supplier_names:
            details.pop("supplier", None)  # supplier names only for viewers allowed to see them
        itinerary = row.pop("itinerary", None)
        items.append(NotificationView(**{**row, "details": details}, urgent=row["kind"] in notifications.URGENT_KINDS,
                                      **airline_of(itinerary if isinstance(itinerary, list) else None)))
    return NotificationList(items=items, email_enabled=notifications.email_configured())
