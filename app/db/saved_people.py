"""Saved people: one record per real person (customer, traveler or both) with their documents.

Owner scope: every record belongs to Travelian (owner None) or one agency; every function takes the
caller's owner and never reads or changes another owner's people. Matching (docs/pii-storage.md):
- Document key: owner + id_type + document country + normalised number (CNIC digits only; passport
  upper-case letters and digits). One live document per key and owner.
- A document already saved for a person with a different name or date of birth: conflict (409).
- Same name and date of birth (or, for customers, the same email or phone) as an existing person,
  without a matching document: a possible duplicate. Nothing is merged automatically; the caller
  chooses (add to that person, or keep separate), and the choice is logged.
Identity is plain text in the sandbox phase; reads that leave the gateway are masked by the API
layer. Events carry ids and field names only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.core import pii
from app.db import agencies

# Kept identical to the frozen copy in migration 0017.
BACKFILL_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (b.agency_id, lower(trim(c.email)))
           b.agency_id AS owner, lower(trim(c.email)) AS email_normalized, c.first_name, c.last_name, c.phone_code, c.phone, c.email
      FROM booking_customers c JOIN bookings b ON b.id = c.booking_id
     WHERE c.person_id IS NULL AND trim(coalesce(c.email, '')) <> ''
     ORDER BY b.agency_id, lower(trim(c.email)), c.created_at DESC
)
INSERT INTO saved_people (id, owner_agency_id, first_name, last_name, phone_code, phone, phone_normalized, email, email_normalized)
SELECT gen_random_uuid()::text, l.owner, l.first_name, l.last_name, l.phone_code, l.phone,
       nullif(regexp_replace(coalesce(l.phone_code, '') || regexp_replace(coalesce(l.phone, ''), '^0+', ''), '[^0-9]', '', 'g'), ''),
       l.email, l.email_normalized
  FROM latest l
 WHERE NOT EXISTS (SELECT 1 FROM saved_people p WHERE p.email_normalized = l.email_normalized
                     AND p.owner_agency_id IS NOT DISTINCT FROM l.owner AND p.merged_into_id IS NULL);
UPDATE booking_customers c SET person_id = p.id
  FROM bookings b, saved_people p
 WHERE b.id = c.booking_id AND c.person_id IS NULL AND trim(coalesce(c.email, '')) <> ''
   AND p.email_normalized = lower(trim(c.email)) AND p.owner_agency_id IS NOT DISTINCT FROM b.agency_id AND p.merged_into_id IS NULL;
"""

PERSON_FIELDS = ("title", "first_name", "last_name", "date_of_birth", "nationality", "phone_code", "phone", "email")


class PeopleError(Exception):
    """status, a stable code, a message without personal data, and facts for the response."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


@dataclass(frozen=True)
class Document:
    id: str
    person_id: str
    id_type: str
    document_country: str
    document_number: str
    document_expiry: date | None


@dataclass(frozen=True)
class Person:
    id: str
    owner_agency_id: str | None
    title: str | None
    first_name: str
    last_name: str
    date_of_birth: date | None
    nationality: str | None
    phone_code: str | None
    phone: str | None
    email: str | None
    merged_into_id: str | None
    documents: list[Document] = field(default_factory=list)
    last_booking_at: datetime | None = None
    match: str | None = None  # search: document, contact or name


@dataclass(frozen=True)
class DocumentInput:
    id_type: str
    document_country: str
    document_number: str
    document_expiry: date | None = None

    @property
    def normalized(self) -> str:
        return pii.normalize_document(self.document_number, self.id_type, self.document_country)


# ---------- helpers ----------

def _same_name(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").lower().split()) == " ".join((b or "").lower().split())


def same_person(person: Person, first: str, last: str, birth: date | None) -> bool:
    return _same_name(person.first_name, first) and _same_name(person.last_name, last) and (
        person.date_of_birth is None or birth is None or person.date_of_birth == birth)


def _event(connection, person_id: str, actor: str | None, action: str, details: dict[str, Any]) -> None:
    connection.execute(
        text("INSERT INTO saved_person_events (id, person_id, actor_user_id, action, details) VALUES (:id, :p, :a, :action, CAST(:d AS jsonb))"),
        {"id": str(uuid4()), "p": person_id, "a": actor, "action": action, "d": json.dumps(details, default=str)},
    )


def _owner_clause(column: str = "owner_agency_id") -> str:
    return f"{column} IS NOT DISTINCT FROM :owner"


def _documents(connection, person_ids: list[str]) -> dict[str, list[Document]]:
    if not person_ids:
        return {}
    rows = connection.execute(
        text("SELECT id, person_id, id_type, document_country, document_number, document_expiry FROM saved_person_documents"
             " WHERE person_id = ANY(:ids) AND removed_at IS NULL ORDER BY created_at"),
        {"ids": person_ids},
    ).mappings().all()
    found: dict[str, list[Document]] = {}
    for row in rows:
        found.setdefault(row["person_id"], []).append(Document(**row))
    return found


_PERSON_SELECT = """SELECT p.id, p.owner_agency_id, p.title, p.first_name, p.last_name, p.date_of_birth, p.nationality, p.phone_code,
       p.phone, p.email, p.merged_into_id,
       (SELECT max(b.created_at) FROM bookings b WHERE b.id IN (
            SELECT booking_id FROM booking_customers WHERE person_id = p.id
            UNION SELECT booking_id FROM booking_travelers WHERE person_id = p.id)) AS last_booking_at
  FROM saved_people p"""


def _people(connection, where: str, params: dict[str, Any], match: str | None = None) -> list[Person]:
    rows = connection.execute(text(f"{_PERSON_SELECT} WHERE {where}"), params).mappings().all()
    docs = _documents(connection, [row["id"] for row in rows])
    return [Person(**row, documents=docs.get(row["id"], []), match=match) for row in rows]


def load(connection, owner: str | None, person_id: str, lock: bool = False) -> Person:
    if lock:
        connection.execute(text("SELECT 1 FROM saved_people WHERE id = :id FOR UPDATE"), {"id": person_id})
    found = _people(connection, f"p.id = :id AND {_owner_clause('p.owner_agency_id')}", {"id": person_id, "owner": owner})
    if not found:
        raise PeopleError(404, "person_not_found", "Saved person not found.")
    return found[0]


def find_document(connection, owner: str | None, document: DocumentInput) -> tuple[Person, Document] | None:
    row = connection.execute(
        text("SELECT person_id, id FROM saved_person_documents WHERE coalesce(owner_agency_id, '') = coalesce(:owner, '')"
             " AND id_type = :t AND document_country = :c AND document_number_normalized = :n AND removed_at IS NULL"),
        {"owner": owner, "t": document.id_type, "c": document.document_country, "n": document.normalized},
    ).mappings().one_or_none()
    if row is None:
        return None
    person = load(connection, owner, row["person_id"])
    return person, next(doc for doc in person.documents if doc.id == row["id"])


def possible_duplicates(connection, owner: str | None, first: str, last: str, birth: date | None,
                        email: str | None = None, phone: str | None = None, exclude: str | None = None) -> list[tuple[Person, str]]:
    """Live people of this owner with the same name and date of birth, or the same email or phone."""
    found: list[tuple[Person, str]] = []
    if birth is not None:
        for person in _people(connection, f"{_owner_clause('p.owner_agency_id')} AND p.merged_into_id IS NULL AND p.date_of_birth = :dob"
                              " AND lower(p.first_name) = lower(:first) AND lower(p.last_name) = lower(:last)",
                              {"owner": owner, "dob": birth, "first": first.strip(), "last": last.strip()}):
            found.append((person, "same_name_and_birth_date"))
    if email:
        found += [(p, "same_email") for p in _people(connection, f"{_owner_clause('p.owner_agency_id')} AND p.merged_into_id IS NULL"
                                                     " AND p.email_normalized = :email", {"owner": owner, "email": email})]
    if phone:
        found += [(p, "same_phone") for p in _people(connection, f"{_owner_clause('p.owner_agency_id')} AND p.merged_into_id IS NULL"
                                                     " AND p.phone_normalized = :phone", {"owner": owner, "phone": phone})]
    seen, unique = set(), []
    for person, reason in found:
        if person.id not in seen and person.id != exclude:
            seen.add(person.id)
            unique.append((person, reason))
    return unique


def _insert_person(connection, owner: str | None, actor: str | None, values: dict[str, Any]) -> str:
    person_id = str(uuid4())
    connection.execute(
        text("INSERT INTO saved_people (id, owner_agency_id, title, first_name, last_name, date_of_birth, nationality, phone_code, phone,"
             " phone_normalized, email, email_normalized, created_by_user_id) VALUES (:id, :owner, :title, :first_name, :last_name, :date_of_birth,"
             " :nationality, :phone_code, :phone, :phone_n, :email, :email_n, :actor)"),
        {"id": person_id, "owner": owner, "actor": actor, **{k: values.get(k) for k in PERSON_FIELDS},
         "phone_n": pii.normalize_phone(values.get("phone_code"), values.get("phone")), "email_n": pii.normalize_email(values.get("email"))},
    )
    return person_id


def _insert_document(connection, owner: str | None, actor: str | None, person_id: str, document: DocumentInput) -> str:
    document_id = str(uuid4())
    connection.execute(
        text("INSERT INTO saved_person_documents (id, person_id, owner_agency_id, id_type, document_country, document_number,"
             " document_number_normalized, document_expiry, created_by_user_id) VALUES (:id, :p, :owner, :t, :c, :n, :nn, :e, :actor)"),
        {"id": document_id, "p": person_id, "owner": owner, "t": document.id_type, "c": document.document_country,
         "n": document.document_number.strip(), "nn": document.normalized, "e": document.document_expiry, "actor": actor},
    )
    return document_id


def _check_document_free(connection, owner: str | None, document: DocumentInput, first: str, last: str, birth: date | None,
                         person_id: str | None = None) -> tuple[Person, Document] | None:
    """The person already holding this document, if it is the same person; 409 if it's someone else."""
    held = find_document(connection, owner, document)
    if held is None:
        return None
    person, _doc = held
    if person_id is not None and person.id == person_id:
        return held
    if person_id is None and same_person(person, first, last, birth):
        return held
    raise PeopleError(409, "document_belongs_to_another_person", "This document number is already saved for a different person.")


# ---------- endpoints ----------

def search(owner: str | None, q: str, limit: int = 20) -> list[Person]:
    """Exact full document number first, then phone or email, then name prefix. Merged people excluded."""
    q = q.strip()
    results: list[Person] = []
    with agencies.application_engine().begin() as connection:
        live = f"{_owner_clause('p.owner_agency_id')} AND p.merged_into_id IS NULL"
        numbers = {pii.normalize_document(q), pii.normalize_document(q, "NATI", "PAK")} - {""}
        if numbers:
            results += _people(connection, f"{live} AND p.id IN (SELECT person_id FROM saved_person_documents WHERE removed_at IS NULL"
                               " AND coalesce(owner_agency_id, '') = coalesce(:owner, '') AND document_number_normalized = ANY(:numbers))",
                               {"owner": owner, "numbers": list(numbers)}, "document")
        email = pii.normalize_email(q) if "@" in q else None
        digits = "".join(ch for ch in q if ch.isdigit())
        if email:
            results += _people(connection, f"{live} AND p.email_normalized = :email", {"owner": owner, "email": email}, "contact")
        elif len(digits) >= 7 and len(digits) == len(q.replace(" ", "").replace("+", "").replace("-", "")):
            results += _people(connection, f"{live} AND (p.phone_normalized = :phone OR p.phone_normalized LIKE :suffix)",
                               {"owner": owner, "phone": digits.lstrip("0"), "suffix": "%" + digits.lstrip("0")}, "contact")
        words = q.lower().split()
        if words and not email:
            if len(words) == 1:
                where, params = "(lower(p.first_name) LIKE :w OR lower(p.last_name) LIKE :w)", {"w": words[0] + "%"}
            else:
                where = "((lower(p.first_name) LIKE :a AND lower(p.last_name) LIKE :b) OR (lower(p.last_name) LIKE :a AND lower(p.first_name) LIKE :b))"
                params = {"a": words[0] + "%", "b": " ".join(words[1:]) + "%"}
            results += _people(connection, f"{live} AND {where} ORDER BY p.last_name, p.first_name LIMIT :limit", {"owner": owner, **params, "limit": limit}, "name")
    seen, unique = set(), []
    for person in results:
        if person.id not in seen:
            seen.add(person.id)
            unique.append(person)
    return unique[:limit]


def get(owner: str | None, person_id: str) -> tuple[Person, list[dict[str, Any]]]:
    with agencies.application_engine().begin() as connection:
        person = load(connection, owner, person_id)
        bookings = connection.execute(
            text("SELECT b.reference AS booking_id, b.created_at, 'customer' AS role FROM booking_customers c JOIN bookings b ON b.id = c.booking_id"
                 " WHERE c.person_id = :p UNION ALL SELECT b.reference, b.created_at, 'traveler' FROM booking_travelers t"
                 " JOIN bookings b ON b.id = t.booking_id WHERE t.person_id = :p ORDER BY created_at DESC"),
            {"p": person_id},
        ).mappings().all()
    return person, [dict(row) for row in bookings]


def _create_in(connection, owner: str | None, actor: str, values: dict[str, Any], document: DocumentInput | None,
               choice: str | None, choice_person_id: str | None, source: dict[str, Any] | None = None) -> tuple[str, str | None, str]:
    """The duplicate rules and checks shared by POST /customers and resolving a hold's needs_choice.
    Returns (person_id, document_id, outcome): created, existing, or added_to_existing."""
    source = source or {}
    if document is not None:
        held = _check_document_free(connection, owner, document, values["first_name"], values["last_name"], values.get("date_of_birth"))
        if held is not None:
            return held[0].id, held[1].id, "existing"
    if choice is None:
        duplicates = possible_duplicates(connection, owner, values["first_name"], values["last_name"], values.get("date_of_birth"),
                                         pii.normalize_email(values.get("email")), pii.normalize_phone(values.get("phone_code"), values.get("phone")))
        if duplicates:
            raise PeopleError(409, "possible_duplicate", "A saved person looks like the same person. Choose: add to that person, or keep separate.",
                              duplicate=duplicates[0][0], reason=duplicates[0][1])
    if choice == "add_to_existing":
        if not choice_person_id:
            raise PeopleError(422, "person_required", "Choose the saved person to add to.")
        target = load(connection, owner, choice_person_id, lock=True)
        if target.merged_into_id:
            raise PeopleError(409, "person_merged", "That person was merged into another record.", merged_into_id=target.merged_into_id)
        document_id = _insert_document(connection, owner, actor, target.id, document) if document is not None else None
        _event(connection, target.id, actor, "duplicate_choice", {"choice": choice, "documents_added": int(document is not None), **source})
        return target.id, document_id, "added_to_existing"
    person_id = _insert_person(connection, owner, actor, values)
    document_id = _insert_document(connection, owner, actor, person_id, document) if document is not None else None
    _event(connection, person_id, actor, "created", {"fields": sorted(k for k in PERSON_FIELDS if values.get(k)), "documents": int(document is not None), **source})
    if choice == "keep_separate" and choice_person_id:
        _event(connection, person_id, actor, "duplicate_choice", {"choice": choice, "other_person_id": choice_person_id, **source})
    return person_id, document_id, "created"


def create(owner: str | None, actor: str, values: dict[str, Any], document: DocumentInput | None,
           choice: str | None = None, choice_person_id: str | None = None) -> tuple[Person, str]:
    """Create a person (and document). Returns (person, outcome): created, existing, or added_to_existing."""
    with agencies.application_engine().begin() as connection:
        person_id, _document_id, outcome = _create_in(connection, owner, actor, values, document, choice, choice_person_id)
        return load(connection, owner, person_id), outcome


def update(owner: str | None, actor: str, person_id: str, changes: dict[str, Any]) -> Person:
    with agencies.application_engine().begin() as connection:
        person = load(connection, owner, person_id, lock=True)
        if person.merged_into_id:
            raise PeopleError(409, "person_merged", "That person was merged into another record.", merged_into_id=person.merged_into_id)
        _apply(connection, person, changes)
        _event(connection, person_id, actor, "edited", {"fields": sorted(changes)})
        return load(connection, owner, person_id)


def _apply(connection, person: Person, changes: dict[str, Any]) -> None:
    if not changes:
        return
    values = {k: getattr(person, k) for k in PERSON_FIELDS} | changes
    connection.execute(
        text("UPDATE saved_people SET title = :title, first_name = :first_name, last_name = :last_name, date_of_birth = :date_of_birth,"
             " nationality = :nationality, phone_code = :phone_code, phone = :phone, phone_normalized = :phone_n, email = :email,"
             " email_normalized = :email_n, updated_at = now(), revision = revision + 1 WHERE id = :id"),
        {**values, "id": person.id, "phone_n": pii.normalize_phone(values["phone_code"], values["phone"]), "email_n": pii.normalize_email(values["email"])},
    )


def add_document(owner: str | None, actor: str, person_id: str, document: DocumentInput) -> Person:
    with agencies.application_engine().begin() as connection:
        person = load(connection, owner, person_id, lock=True)
        if person.merged_into_id:
            raise PeopleError(409, "person_merged", "That person was merged into another record.", merged_into_id=person.merged_into_id)
        if _check_document_free(connection, owner, document, person.first_name, person.last_name, person.date_of_birth, person_id=person_id):
            raise PeopleError(409, "document_already_saved", "This document is already saved for this person.")
        document_id = _insert_document(connection, owner, actor, person_id, document)
        _event(connection, person_id, actor, "document_added", {"document_id": document_id, "id_type": document.id_type})
        return load(connection, owner, person_id)


def remove_document(owner: str | None, actor: str, person_id: str, document_id: str) -> Person:
    with agencies.application_engine().begin() as connection:
        load(connection, owner, person_id, lock=True)
        removed = connection.execute(
            text("UPDATE saved_person_documents SET removed_at = now() WHERE id = :d AND person_id = :p AND removed_at IS NULL"),
            {"d": document_id, "p": person_id},
        ).rowcount
        if not removed:
            raise PeopleError(404, "document_not_found", "Document not found for this person.")
        _event(connection, person_id, actor, "document_removed", {"document_id": document_id})
        return load(connection, owner, person_id)


def merge(owner: str | None, actor: str, source_id: str, target_id: str, reason: str) -> Person:
    """Move the source's documents and booking links to the target; the source is marked merged. Logged on both."""
    if source_id == target_id:
        raise PeopleError(422, "same_person", "Choose two different people to merge.")
    with agencies.application_engine().begin() as connection:
        source, target = load(connection, owner, source_id, lock=True), load(connection, owner, target_id, lock=True)
        if source.merged_into_id or target.merged_into_id:
            raise PeopleError(409, "person_merged", "One of these people was already merged.")
        moved = 0
        for doc in source.documents:
            duplicate = next((d for d in target.documents if (d.id_type, d.document_country) == (doc.id_type, doc.document_country)
                              and pii.normalize_document(d.document_number, d.id_type, d.document_country)
                              == pii.normalize_document(doc.document_number, doc.id_type, doc.document_country)), None)
            if duplicate:
                connection.execute(text("UPDATE saved_person_documents SET removed_at = now() WHERE id = :d"), {"d": doc.id})
                connection.execute(text("UPDATE booking_travelers SET document_id = :new WHERE document_id = :old"), {"new": duplicate.id, "old": doc.id})
            else:
                connection.execute(text("UPDATE saved_person_documents SET person_id = :t WHERE id = :d"), {"t": target_id, "d": doc.id})
                moved += 1
        links = sum(connection.execute(text(f"UPDATE {table} SET person_id = :t WHERE person_id = :s"), {"t": target_id, "s": source_id}).rowcount
                    for table in ("booking_customers", "booking_travelers"))
        connection.execute(text("UPDATE saved_people SET merged_into_id = :t, updated_at = now(), revision = revision + 1 WHERE id = :s"),
                           {"t": target_id, "s": source_id})
        _event(connection, source_id, actor, "merged_into", {"other_person_id": target_id, "reason": reason})
        _event(connection, target_id, actor, "merged_from", {"other_person_id": source_id, "reason": reason, "documents_moved": moved, "booking_links_moved": links})
        return load(connection, owner, target_id)


def events(owner: str | None, person_id: str) -> list[dict[str, Any]]:
    with agencies.application_engine().begin() as connection:
        load(connection, owner, person_id)
        rows = connection.execute(text("SELECT action, actor_user_id, details, created_at FROM saved_person_events WHERE person_id = :p ORDER BY created_at"),
                                  {"p": person_id}).mappings().all()
    return [dict(row) for row in rows]


def backfill(connection) -> dict[str, int]:
    """Idempotent: saved customers for booking customers with an email and no link yet (same SQL as migration 0017)."""
    before = connection.scalar(text("SELECT count(*) FROM saved_people"))
    connection.execute(text(BACKFILL_SQL))
    after = connection.scalar(text("SELECT count(*) FROM saved_people"))
    linked = connection.scalar(text("SELECT count(*) FROM booking_customers WHERE person_id IS NOT NULL"))
    return {"created": after - before, "linked_booking_customers": linked}


# ---------- resolving a hold's needs_choice from the booking's own copy ----------

def booking_copy(connection, owner: str | None, reference: str, role: str, position: int) -> tuple[str, dict[str, Any], DocumentInput | None]:
    """(booking id, person values, document) read from the booking's own customer or traveler row, locked.
    404 when the booking isn't in the caller's scope or has no such row; 409 when the row is already linked."""
    booking_id = connection.scalar(text(f"SELECT id FROM bookings WHERE reference = :r AND {_owner_clause('agency_id')}"),
                                   {"r": reference, "owner": owner})
    if booking_id is None:
        raise PeopleError(404, "booking_not_found", "Booking not found.")
    if role == "customer":
        row = connection.execute(
            text("SELECT first_name, last_name, phone_code, phone, email, person_id FROM booking_customers WHERE booking_id = :b FOR UPDATE"),
            {"b": booking_id}).mappings().one_or_none() if position == 0 else None
    else:
        row = connection.execute(
            text("SELECT title, first_name, last_name, date_of_birth, nationality, id_type, document_country, document_number, document_expiry,"
                 " person_id FROM booking_travelers WHERE booking_id = :b AND position = :p FOR UPDATE"),
            {"b": booking_id, "p": position}).mappings().one_or_none()
    if row is None:
        raise PeopleError(404, "booking_person_not_found", f"This booking has no {role} at that position.")
    if row["person_id"] is not None:
        raise PeopleError(409, "already_linked", f"This booking's {role} is already linked to a saved person.", person_id=row["person_id"])
    values = {k: row[k] for k in PERSON_FIELDS if k in row}
    document = None
    if role == "traveler" and row["document_number"]:
        document = DocumentInput(row["id_type"], row["document_country"], row["document_number"], row["document_expiry"])
    return booking_id, values, document


def resolve_from_booking(owner: str | None, actor: str, reference: str, role: str, position: int, choice: str,
                         choice_person_id: str | None, validate=None) -> tuple[Person, str]:
    """Apply the duplicate choice for one person of a booking, using the booking's own copy (the browser
    sends no identity values), then link the booking row. Same rules and checks as create()."""
    with agencies.application_engine().begin() as connection:
        booking_id, values, document = booking_copy(connection, owner, reference, role, position)
        if validate is not None:
            validate(values, document)
        source = {"source": "booking", "booking_id": reference, "role": role, **({"position": position} if role == "traveler" else {})}
        person_id, document_id, outcome = _create_in(connection, owner, actor, values, document, choice, choice_person_id, source)
        if role == "traveler":
            _link_traveler(connection, booking_id, position, person_id, document_id)
        else:
            connection.execute(text("UPDATE booking_customers SET person_id = :p WHERE booking_id = :b"), {"p": person_id, "b": booking_id})
        _event(connection, person_id, actor, "linked_to_booking", source)
        return load(connection, owner, person_id), outcome


# ---------- holds ----------

@dataclass
class SavedTraveler:
    """A saved person's identity, loaded on the server for a hold (never sent to the browser)."""

    person_id: str
    document_id: str | None
    title: str | None
    first_name: str
    last_name: str
    date_of_birth: date | None
    nationality: str | None
    id_type: str | None
    document_number: str | None
    document_country: str | None
    document_expiry: date | None


def load_for_hold(owner: str | None, person_id: str, document_id: str | None) -> SavedTraveler:
    with agencies.application_engine().begin() as connection:
        person = load(connection, owner, person_id)
    if person.merged_into_id:
        raise PeopleError(409, "person_merged", "That saved person was merged into another record.", merged_into_id=person.merged_into_id)
    if document_id is not None:
        document = next((d for d in person.documents if d.id == document_id), None)
        if document is None:
            raise PeopleError(422, "document_not_found", "That document isn't saved for this person.")
    elif len(person.documents) == 1:
        document = person.documents[0]
    else:
        raise PeopleError(422, "document_required", "Choose which saved document to use." if person.documents
                          else "This saved person has no document. Add one first.")
    return SavedTraveler(person.id, document.id, person.title, person.first_name, person.last_name, person.date_of_birth, person.nationality,
                         document.id_type, document.document_number, document.document_country, document.document_expiry)


def load_contact(owner: str | None, person_id: str) -> Person:
    with agencies.application_engine().begin() as connection:
        person = load(connection, owner, person_id)
    if person.merged_into_id:
        raise PeopleError(409, "person_merged", "That saved person was merged into another record.", merged_into_id=person.merged_into_id)
    return person


@dataclass
class HoldTraveler:
    position: int
    first_name: str
    last_name: str
    title: str | None
    date_of_birth: date | None
    nationality: str | None
    document: DocumentInput | None
    person_id: str | None = None      # already linked (traveler_id)
    document_id: str | None = None
    save: bool = True


@dataclass
class HoldCustomer:
    first_name: str
    last_name: str
    phone_code: str
    phone: str
    email: str
    person_id: str | None = None      # contact.customer_id
    save: bool = True


@dataclass
class SaveOutcome:
    role: str                          # customer or traveler
    position: int | None
    status: str                        # saved, linked, updated, needs_choice, conflict, not_saved, skipped
    person_id: str | None = None
    document_id: str | None = None
    duplicate: Person | None = None
    reason: str | None = None
    code: str | None = None


def _link_traveler(connection, booking_id: str, position: int, person_id: str, document_id: str | None) -> None:
    connection.execute(text("UPDATE booking_travelers SET person_id = :p, document_id = :d WHERE booking_id = :b AND position = :pos"),
                       {"p": person_id, "d": document_id, "b": booking_id, "pos": position})


def _save_traveler(connection, owner: str | None, actor: str, reference: str, booking_id: str, traveler: HoldTraveler) -> SaveOutcome:
    if traveler.person_id:
        return SaveOutcome("traveler", traveler.position, "linked", traveler.person_id, traveler.document_id)
    values = {"title": traveler.title, "first_name": traveler.first_name, "last_name": traveler.last_name,
              "date_of_birth": traveler.date_of_birth, "nationality": traveler.nationality}
    if traveler.document is not None:
        held = find_document(connection, owner, traveler.document)
        if held is not None:
            person, document = held
            if not same_person(person, traveler.first_name, traveler.last_name, traveler.date_of_birth):
                return SaveOutcome("traveler", traveler.position, "conflict", code="document_belongs_to_another_person")
            if traveler.document.document_expiry and traveler.document.document_expiry != document.document_expiry:
                connection.execute(text("UPDATE saved_person_documents SET document_expiry = :e WHERE id = :d"),
                                   {"e": traveler.document.document_expiry, "d": document.id})
            _link_traveler(connection, booking_id, traveler.position, person.id, document.id)
            _event(connection, person.id, actor, "linked_to_booking", {"booking_id": reference, "position": traveler.position, "role": "traveler"})
            return SaveOutcome("traveler", traveler.position, "linked", person.id, document.id)
    duplicates = possible_duplicates(connection, owner, traveler.first_name, traveler.last_name, traveler.date_of_birth)
    if duplicates:
        return SaveOutcome("traveler", traveler.position, "needs_choice", duplicate=duplicates[0][0], reason=duplicates[0][1], code="possible_duplicate")
    person_id = _insert_person(connection, owner, actor, values)
    document_id = _insert_document(connection, owner, actor, person_id, traveler.document) if traveler.document else None
    _link_traveler(connection, booking_id, traveler.position, person_id, document_id)
    _event(connection, person_id, actor, "created", {"source": "hold", "booking_id": reference, "position": traveler.position,
                                                     "documents": int(document_id is not None)})
    return SaveOutcome("traveler", traveler.position, "saved", person_id, document_id)


def _save_customer(connection, owner: str | None, actor: str, reference: str, booking_id: str, customer: HoldCustomer,
                   travelers: list[tuple[HoldTraveler, SaveOutcome]]) -> SaveOutcome:
    contact = {"phone_code": customer.phone_code, "phone": customer.phone, "email": customer.email}
    if customer.person_id:
        person = load(connection, owner, customer.person_id, lock=True)
        changed = {k: v for k, v in contact.items()
                   if (pii.normalize_email(v) != pii.normalize_email(getattr(person, k)) if k == "email" else (v or None) != getattr(person, k))}
        _apply(connection, person, changed)
        connection.execute(text("UPDATE booking_customers SET person_id = :p WHERE booking_id = :b"), {"p": person.id, "b": booking_id})
        _event(connection, person.id, actor, "linked_to_booking", {"booking_id": reference, "role": "customer", "fields_updated": sorted(changed)})
        return SaveOutcome("customer", None, "updated" if changed else "linked", person.id)
    matches = possible_duplicates(connection, owner, customer.first_name, customer.last_name, None,
                                  pii.normalize_email(customer.email), pii.normalize_phone(customer.phone_code, customer.phone))
    same = [p for p, _ in matches if _same_name(p.first_name, customer.first_name) and _same_name(p.last_name, customer.last_name)]
    if matches and not same:
        return SaveOutcome("customer", None, "needs_choice", duplicate=matches[0][0], reason=matches[0][1], code="possible_duplicate")
    person_id = same[0].id if same else None
    if person_id is None:  # the customer may be one of this booking's travelers (same name, saved just now)
        person_id = next((outcome.person_id for traveler, outcome in travelers if outcome.status in ("saved", "linked") and outcome.person_id
                          and _same_name(traveler.first_name, customer.first_name) and _same_name(traveler.last_name, customer.last_name)), None)
    if person_id is None:
        person_id = _insert_person(connection, owner, actor, {"first_name": customer.first_name, "last_name": customer.last_name, **contact})
        status = "saved"
        _event(connection, person_id, actor, "created", {"source": "hold", "booking_id": reference, "role": "customer"})
    else:
        person = load(connection, owner, person_id, lock=True)
        missing = {k: v for k, v in contact.items() if v and not getattr(person, k)}
        _apply(connection, person, missing)
        status = "linked"
        _event(connection, person_id, actor, "linked_to_booking", {"booking_id": reference, "role": "customer", "fields_updated": sorted(missing)})
    connection.execute(text("UPDATE booking_customers SET person_id = :p WHERE booking_id = :b"), {"p": person_id, "b": booking_id})
    return SaveOutcome("customer", None, status, person_id)


def save_from_hold(owner: str | None, actor: str, reference: str, customer: HoldCustomer, travelers: list[HoldTraveler]) -> list[SaveOutcome]:
    """After a hold is saved: create or link saved people. Each person in its own savepoint, so one
    problem never undoes the others; the caller never lets this fail the hold."""
    outcomes: list[SaveOutcome] = []
    with agencies.application_engine().begin() as connection:
        booking_id = connection.scalar(text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference})
        pairs: list[tuple[HoldTraveler, SaveOutcome]] = []
        for traveler in travelers:
            if not traveler.save and not traveler.person_id:
                outcome = SaveOutcome("traveler", traveler.position, "skipped")
            else:
                try:
                    with connection.begin_nested():
                        outcome = _save_traveler(connection, owner, actor, reference, booking_id, traveler)
                except Exception:  # noqa: BLE001 - saving must never fail the hold
                    outcome = SaveOutcome("traveler", traveler.position, "not_saved", code="save_failed")
            pairs.append((traveler, outcome))
            outcomes.append(outcome)
        if customer.save or customer.person_id:
            try:
                with connection.begin_nested():
                    outcomes.insert(0, _save_customer(connection, owner, actor, reference, booking_id, customer, pairs))
            except Exception:  # noqa: BLE001
                outcomes.insert(0, SaveOutcome("customer", None, "not_saved", code="save_failed"))
        else:
            outcomes.insert(0, SaveOutcome("customer", None, "skipped"))
    return outcomes
