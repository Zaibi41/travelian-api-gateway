"""Admin announcements (travelian#92): short messages shown in every staff member's admin top bar.

- GET /admin/announcements: the live ones (active, started, not ended), newest first. Every back-office role.
- GET /admin/announcements/all: every announcement not deleted, with its status. Super Admin only.
- POST /admin/announcements: post one. Super Admin only.
- POST /admin/announcements/{id}/end: take it off the top bar now. Super Admin only.
- DELETE /admin/announcements/{id}: remove it from the top bar and the list (the record and its audit trail are kept).
  Super Admin only.

Agency and customer roles get 403: announcements are for staff (issue #92 default: not shown).
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BackOfficeUser, require_back_office, require_super_admin
from app.db import admin_announcements as store

router = APIRouter(prefix="/admin/announcements", tags=["admin announcements"],
                   responses={403: {"description": "Caller may not do this (reading: back office only; changes: Super Admin only)."},
                              503: {"description": "Database not available."}})


class AnnouncementView(BaseModel):
    id: str
    message: str = Field(description="Plain text, 2 to 240 characters.")
    created_at: datetime
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class AnnouncementList(BaseModel):
    items: list[AnnouncementView]


class ManagedAnnouncement(AnnouncementView):
    status: Literal["active", "scheduled", "ended"] = Field(
        description="active: in the top bar now; scheduled: its start time is ahead; ended: ended by hand or its end time passed.")
    ended_at: datetime | None = None


class ManagedAnnouncementList(BaseModel):
    items: list[ManagedAnnouncement]


class DeletedAnnouncement(BaseModel):
    id: str
    deleted: Literal[True] = True


class CreateAnnouncementRequest(BaseModel):
    message: str = Field(max_length=2000, description="Plain text. Extra spaces and line breaks are removed; < and > are refused.")
    starts_at: datetime | None = Field(None, description="Optional; with a time zone. Shown from then on.")
    ends_at: datetime | None = Field(None, description="Optional; with a time zone, in the future and after starts_at.")


def _view(item: store.Announcement) -> AnnouncementView:
    return AnnouncementView(id=item.id, message=item.message, created_at=item.created_at, starts_at=item.starts_at, ends_at=item.ends_at)


def _managed(item: store.Announcement) -> ManagedAnnouncement:
    return ManagedAnnouncement(**_view(item).model_dump(), status=item.status, ended_at=item.ended_at)


def _refused(error: store.AnnouncementError) -> HTTPException:
    return HTTPException(error.status, error.message)


@router.get("", response_model=AnnouncementList)
async def list_announcements(viewer: Annotated[BackOfficeUser, Depends(require_back_office)]) -> AnnouncementList:
    """What the admin top bar shows, newest first. Empty list when there are none."""
    items = await run_in_threadpool(store.list_live)
    return AnnouncementList(items=[_view(item) for item in items])


@router.get("/all", response_model=ManagedAnnouncementList)
async def list_all_announcements(viewer: Annotated[BackOfficeUser, Depends(require_super_admin)]) -> ManagedAnnouncementList:
    """The Announcements page: every announcement not deleted, newest first."""
    items = await run_in_threadpool(store.list_all)
    return ManagedAnnouncementList(items=[_managed(item) for item in items])


@router.post("", response_model=ManagedAnnouncement, status_code=201, responses={
    422: {"description": "Message not 2 to 240 characters of plain text, or the start/end times don't make sense."},
})
async def create_announcement(body: CreateAnnouncementRequest,
                              viewer: Annotated[BackOfficeUser, Depends(require_super_admin)]) -> ManagedAnnouncement:
    """Post an announcement. All staff see it in the top bar within a minute (the page checks every minute)."""
    try:
        item = await run_in_threadpool(lambda: store.create(body.message, viewer.user_id, viewer.role,
                                                            starts_at=body.starts_at, ends_at=body.ends_at))
    except store.AnnouncementError as error:
        raise _refused(error) from None
    return _managed(item)


@router.post("/{announcement_id}/end", response_model=ManagedAnnouncement, responses={
    404: {"description": "No active announcement with that id."},
})
async def end_announcement(announcement_id: str,
                           viewer: Annotated[BackOfficeUser, Depends(require_super_admin)]) -> ManagedAnnouncement:
    """Take it off the top bar now. It stays in the list as ended."""
    try:
        item = await run_in_threadpool(store.end, announcement_id, viewer.user_id, viewer.role)
    except store.AnnouncementError as error:
        raise _refused(error) from None
    return _managed(item)


@router.delete("/{announcement_id}", response_model=DeletedAnnouncement, responses={404: {"description": "No announcement with that id."}})
async def delete_announcement(announcement_id: str, viewer: Annotated[BackOfficeUser, Depends(require_super_admin)]) -> DeletedAnnouncement:
    """Remove it from the top bar and the list."""
    try:
        await run_in_threadpool(store.delete, announcement_id, viewer.user_id, viewer.role)
    except store.AnnouncementError as error:
        raise _refused(error) from None
    return DeletedAnnouncement(id=announcement_id)
