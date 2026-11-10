"""Saved customers and travelers (one kind of record: a person). Back office only (super_admin and
staff; agency and customer roles get 403, like the Bookings endpoints), in Travelian's own scope.

Search and detail are masked (there is no reveal for saved people). Create, edit, documents and
resolving a hold's possible duplicate need manage_people; merge needs merge_people. Nothing is
ever merged automatically. Errors carry a stable code and no personal data.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.routes.admin_dashboard import Viewer
from app.core import pii
from app.core.field_visibility import MANAGE_PEOPLE, MERGE_PEOPLE, BackOfficeUser
from app.db import saved_people as store
from app.schemas.saved_people import (
    BookingPersonRole, DocumentInput, DuplicateChoice, MergeRequest, SavedDocument, SavedPersonBooking, SavedPersonCreate, SavedPersonCreated,
    SavedPersonDetail, SavedPersonEvent, SavedPersonSearch, SavedPersonSummary, SavedPersonUpdate,
)

router = APIRouter(
    prefix="/admin/customers",
    tags=["saved people"],
    responses={403: {"description": "Not back office, or missing the permission."}, 404: {"description": "No such person or document."}},
)
PersonId = Annotated[str, Path(min_length=8, max_length=36)]


def owner_of(viewer: BackOfficeUser) -> str | None:
    """Back-office users work in Travelian's own scope (owner None)."""
    return None


def masked_documents(person: store.Person, today: date | None = None) -> list[SavedDocument]:
    today = today or date.today()
    return [SavedDocument(id=d.id, id_type=d.id_type, document_country=d.document_country, document_number_masked=pii.mask_document(pii.normalize_document(d.document_number, d.id_type, d.document_country)),
                          document_expiry_masked=pii.mask_date(d.document_expiry.isoformat()) if d.document_expiry else None,
                          document_validity=pii.document_validity(d.document_expiry, today)) for d in person.documents]


def summary(person: store.Person) -> SavedPersonSummary:
    return SavedPersonSummary(
        id=person.id, title=person.title, first_name=person.first_name, last_name=person.last_name,
        birth_year=person.date_of_birth.year if person.date_of_birth else None, nationality=person.nationality,
        phone_masked=pii.mask_phone(person.phone_code, person.phone), email_masked=pii.mask_email(person.email),
        documents=masked_documents(person), last_booking_at=person.last_booking_at, match=person.match,
    )


def detail(person: store.Person, bookings: list[dict], viewer: BackOfficeUser) -> SavedPersonDetail:
    base = summary(person).model_dump(exclude={"match"})
    return SavedPersonDetail(
        **base, date_of_birth_masked=pii.mask_date(person.date_of_birth.isoformat()) if person.date_of_birth else None,
        merged_into_id=person.merged_into_id, bookings=[SavedPersonBooking(**b) for b in bookings],
        can_manage=viewer.can(MANAGE_PEOPLE), can_merge=viewer.can(MERGE_PEOPLE),
    )


def error(exc: store.PeopleError) -> JSONResponse:
    body = {"detail": exc.message, "code": exc.code}
    for key, value in exc.extra.items():
        body["possible_duplicate" if key == "duplicate" else key] = summary(value).model_dump(mode="json") if isinstance(value, store.Person) else value
    return JSONResponse(body, status_code=exc.status)


def refuse(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": message, "code": code}, status_code=status)


def _document(body: DocumentInput) -> store.DocumentInput:
    return store.DocumentInput(body.id_type, body.document_country, body.document_number, body.document_expiry)


async def _detail(viewer: BackOfficeUser, person_id: str) -> SavedPersonDetail:
    person, bookings = await run_in_threadpool(store.get, owner_of(viewer), person_id)
    return detail(person, bookings, viewer)


@router.get("", response_model=SavedPersonSearch, response_model_exclude_none=True,
            responses={422: {"description": "Search needs at least 3 characters."}})
async def search_people(viewer: Viewer, q: Annotated[str, Query(min_length=3, max_length=64, description="Full document number, phone, email, or name prefix.")],
                        limit: Annotated[int, Query(ge=1, le=20)] = 20):
    """Exact full document number first, then phone or email, then name prefix. Masked values only."""
    people = await run_in_threadpool(store.search, owner_of(viewer), q, limit)
    return SavedPersonSearch(results=[summary(person) for person in people], can_manage=viewer.can(MANAGE_PEOPLE), can_merge=viewer.can(MERGE_PEOPLE))


@router.get("/{person_id}", response_model=SavedPersonDetail, response_model_exclude_none=True)
async def get_person(viewer: Viewer, person_id: PersonId):
    try:
        return await _detail(viewer, person_id)
    except store.PeopleError as exc:
        return error(exc)


@router.post("", response_model=SavedPersonCreated, response_model_exclude_none=True, status_code=201,
             responses={409: {"description": "possible_duplicate (choose with duplicate_choice) or document_belongs_to_another_person."}})
async def create_person(viewer: Viewer, body: SavedPersonCreate, response: Response):
    if not viewer.can(MANAGE_PEOPLE):
        return refuse(403, "not_allowed", "Saving people requires the manage people permission.")
    values = body.model_dump(exclude={"document", "duplicate_choice"})
    choice = body.duplicate_choice
    try:
        person, outcome = await run_in_threadpool(store.create, owner_of(viewer), viewer.user_id, values,
                                                  _document(body.document) if body.document else None,
                                                  choice.choice if choice else None, choice.person_id if choice else None)
        if outcome != "created":
            response.status_code = 200
        return SavedPersonCreated(person=await _detail(viewer, person.id), outcome=outcome)
    except store.PeopleError as exc:
        return error(exc)


@router.patch("/{person_id}", response_model=SavedPersonDetail, response_model_exclude_none=True)
async def edit_person(viewer: Viewer, person_id: PersonId, body: SavedPersonUpdate):
    """Edit a saved person. Past bookings keep their own copy and never change."""
    if not viewer.can(MANAGE_PEOPLE):
        return refuse(403, "not_allowed", "Editing people requires the manage people permission.")
    try:
        await run_in_threadpool(store.update, owner_of(viewer), viewer.user_id, person_id, body.model_dump(exclude_unset=True))
        return await _detail(viewer, person_id)
    except store.PeopleError as exc:
        return error(exc)


@router.post("/{person_id}/documents", response_model=SavedPersonDetail, response_model_exclude_none=True, status_code=201)
async def add_document(viewer: Viewer, person_id: PersonId, body: DocumentInput):
    if not viewer.can(MANAGE_PEOPLE):
        return refuse(403, "not_allowed", "Changing documents requires the manage people permission.")
    try:
        await run_in_threadpool(store.add_document, owner_of(viewer), viewer.user_id, person_id, _document(body))
        return await _detail(viewer, person_id)
    except store.PeopleError as exc:
        return error(exc)


@router.delete("/{person_id}/documents/{document_id}", response_model=SavedPersonDetail, response_model_exclude_none=True)
async def remove_document(viewer: Viewer, person_id: PersonId, document_id: Annotated[str, Path(min_length=8, max_length=36)]):
    """Remove a document from the person (kept for past booking links, no longer used or matched)."""
    if not viewer.can(MANAGE_PEOPLE):
        return refuse(403, "not_allowed", "Changing documents requires the manage people permission.")
    try:
        await run_in_threadpool(store.remove_document, owner_of(viewer), viewer.user_id, person_id, document_id)
        return await _detail(viewer, person_id)
    except store.PeopleError as exc:
        return error(exc)


@router.post("/{person_id}/merge", response_model=SavedPersonDetail, response_model_exclude_none=True)
async def merge_person(viewer: Viewer, person_id: PersonId, body: MergeRequest):
    """Merge this person into another: documents and booking links move; this record is marked merged. Logged."""
    if not viewer.can(MERGE_PEOPLE):
        return refuse(403, "not_allowed", "Merging people requires the merge people permission.")
    try:
        await run_in_threadpool(store.merge, owner_of(viewer), viewer.user_id, person_id, body.merge_into_person_id, body.reason.strip())
        return await _detail(viewer, body.merge_into_person_id)
    except store.PeopleError as exc:
        return error(exc)


@router.get("/{person_id}/events", response_model=list[SavedPersonEvent])
async def person_events(viewer: Viewer, person_id: PersonId):
    """Audit trail for a saved person (ids and field names only)."""
    try:
        rows = await run_in_threadpool(store.events, owner_of(viewer), person_id)
    except store.PeopleError as exc:
        return error(exc)
    return [SavedPersonEvent(action=r["action"], actor_user_id=r["actor_user_id"], at=r["created_at"], details=r["details"]) for r in rows]


# ---------- a hold's possible duplicate, resolved from the booking's own copy ----------

booking_router = APIRouter(
    prefix="/admin/bookings",
    tags=["saved people"],
    responses={403: {"description": "Not back office, or missing manage_people."},
               404: {"description": "No such booking (or not in the caller's scope), or no such customer or traveler."}},
)


def _validate_booking_copy(values: dict, document: store.DocumentInput | None) -> None:
    """The same checks as POST /customers, applied to the values read from the booking."""
    try:
        SavedPersonCreate.model_validate({**{k: v for k, v in values.items() if v not in (None, "")},
                                          "document": document.__dict__ if document else None})
    except ValidationError:
        raise store.PeopleError(422, "booking_values_invalid", "This booking's saved details don't pass the checks for a saved person.") from None


@booking_router.post(
    "/{booking_id}/saved-people/{role}/{position}/resolve", response_model=SavedPersonCreated, response_model_exclude_none=True,
    responses={409: {"description": "already_linked, document_belongs_to_another_person or person_merged."},
               422: {"description": "person_required (add_to_existing without person_id) or booking_values_invalid."}},
)
async def resolve_booking_person(viewer: Viewer, body: DuplicateChoice, role: BookingPersonRole,
                                 booking_id: Annotated[str, Path(max_length=64)], position: Annotated[int, Path(ge=0, le=8)],
                                 response: Response):
    """Resolve a hold's needs_choice. The values come from the booking's own customer or traveler row (the
    customer is position 0); the browser sends only the choice. Links the booking row and is logged."""
    if not viewer.can(MANAGE_PEOPLE):
        return refuse(403, "not_allowed", "Saving people requires the manage people permission.")
    try:
        person, outcome = await run_in_threadpool(store.resolve_from_booking, owner_of(viewer), viewer.user_id, booking_id, role, position,
                                                  body.choice, body.person_id, _validate_booking_copy)
        if outcome == "created":
            response.status_code = 201
        return SavedPersonCreated(person=await _detail(viewer, person.id), outcome=outcome)
    except store.PeopleError as exc:
        return error(exc)
