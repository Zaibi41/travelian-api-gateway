"""Hold-save bug (Skypass PNR 4FGYK2-4B4VYB held at the supplier, booking not saved): our record is written
first ('hold_pending'), then the supplier is called, then the record is completed. If completing it fails,
the supplier PNR is kept on a 'hold_unrecorded' record and Skypass seats are released; a supplier failure
leaves nothing pending. The store and the supplier are mocked; no database or supplier is reached."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, resolve_actions
from app.db import group_holds as store
from app.schemas.group_holds import GroupHoldRequest
from app.services import flight_holds, group_holds
from app.services.group_holds import GroupHoldError, HeldSeats, SupplierRefused
from app.services.group_ticketing import group_ticket_id
from app.services.group_ticketing_view import GroupMoney
from app.vendors.group_ticketing.skypass.inventory import SkypassTicket, _normalize_ticket
from tests.group_ticketing.test_live_providers import SKYPASS_ROW

ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser("u-2", "staff", NONE)
PNR = "4FGYK2-4B4VYB"


def stock():
    return [_normalize_ticket(SkypassTicket.model_validate({**SKYPASS_ROW, "airline_pnr": PNR}))]


TICKET_ID = group_ticket_id(stock()[0].tickets[0])
ADULT = {"type": "ADT", "title": "MR", "first_name": "Ali", "last_name": "Raza", "date_of_birth": "1988-03-14", "id_type": "PSPT",
         "document_number": "AB1234567", "document_country": "PAK", "document_expiry": "2031-09-30", "nationality": "PAK"}
REQUEST = GroupHoldRequest(ticket_id=UUID(TICKET_ID), adults=1, passengers=[ADULT],
                           contact={"full_name": "Ali Raza", "phone": "+92 300 1234567", "email": "a@example.invalid"})


class Provider:
    can_release = True

    def __init__(self, hold_error=None, release_error=None):
        self.hold_error, self.release_error, self.calls = hold_error, release_error, []

    async def hold(self, ticket, passengers, contact):
        self.calls.append("hold")
        if self.hold_error:
            raise self.hold_error
        return HeldSeats(PNR, "12116:30345", GroupMoney(amount_minor=160_000_00, currency="PKR"))

    async def release(self, held):
        self.calls.append(("release", held.supplier_ref))
        if self.release_error:
            raise self.release_error


class CheckViolation(Exception):
    class diag:  # noqa: N801 - mirrors psycopg's error.diag
        constraint_name = "ck_bookings_pnr"


class IntegrityError(Exception):
    def __init__(self):
        super().__init__("insert failed")
        self.orig = CheckViolation()


@pytest.fixture
def fakes():
    """The store, recording calls in order. confirm_hold fails like the reported bug."""
    calls = []
    fake = MagicMock()
    fake.create_pending.side_effect = lambda **kw: calls.append(("create_pending", kw)) or ("b-1", "TRV-GRP-AB12CD")
    fake.confirm_hold.side_effect = lambda *a, **kw: calls.append(("confirm_hold", kw)) or (_ for _ in ()).throw(IntegrityError())
    fake.mark_hold_failed.side_effect = lambda *a: calls.append(("mark_hold_failed", a))
    fake.mark_unrecorded.side_effect = lambda *a, **kw: calls.append(("mark_unrecorded", kw))
    fake.mark_release_attempt.side_effect = lambda *a: calls.append(("mark_release_attempt", a))
    fake.mark_unrecorded_released.side_effect = lambda *a: calls.append(("mark_unrecorded_released", a))
    with patch.object(store, "create_pending", fake.create_pending), patch.object(store, "confirm_hold", fake.confirm_hold), \
         patch.object(store, "mark_hold_failed", fake.mark_hold_failed), patch.object(store, "mark_unrecorded", fake.mark_unrecorded), \
         patch.object(store, "mark_release_attempt", fake.mark_release_attempt), \
         patch.object(store, "mark_unrecorded_released", fake.mark_unrecorded_released), \
         patch.object(flight_holds, "resolve_saved", lambda people, owner: (people, {})), \
         patch.object(flight_holds, "_save_people", lambda *a, **kw: []), \
         patch.object(settings, "group_ticketing_markup_percent", Decimal("0")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        yield calls


def hold(provider, viewer=ADMIN):
    with patch.dict(group_holds.PROVIDERS, {"skypass": lambda: provider}):
        return asyncio.run(group_holds.hold(REQUEST, viewer, fetch=AsyncMock(return_value=stock())))


def names(calls):
    return [name for name, _ in calls]


def test_record_is_written_before_the_supplier_is_called(fakes):
    provider = Provider()
    provider.hold = AsyncMock(side_effect=lambda *a: (fakes.append(("supplier_hold", None)), HeldSeats(PNR, "12116:30345", GroupMoney(amount_minor=1, currency="PKR")))[1])
    with pytest.raises(GroupHoldError):
        hold(provider)
    assert names(fakes)[:3] == ["create_pending", "supplier_hold", "confirm_hold"]
    pending = fakes[0][1]
    assert pending["provider"] == "skypass" and pending["selling_total_minor"] == 160_000_00 and pending["customer"] is not None


def test_save_failure_after_supplier_success_keeps_the_pnr_and_releases_skypass(fakes):
    provider = Provider()
    with pytest.raises(GroupHoldError) as raised:
        hold(provider)
    error = raised.value
    unrecorded = dict(fakes)["mark_unrecorded"]
    assert unrecorded["supplier_pnr"] == PNR and unrecorded["supplier_ref"] == "12116:30345" and unrecorded["release_pending"] is True
    assert unrecorded["error"] == "IntegrityError/CheckViolation (ck_bookings_pnr)"
    assert ("release", "12116:30345") in provider.calls
    reference, _, released, code, _, limit = dict(fakes)["mark_release_attempt"]
    assert (reference, released, code, limit) == ("TRV-GRP-AB12CD", True, None, settings.group_hold_release_attempts)
    assert "mark_unrecorded_released" in names(fakes)
    # Supplier access: the real reason, the PNR and our booking reference.
    assert error.status == 500 and error.code == "not_saved"
    assert PNR in error.message and "ck_bookings_pnr" in error.message and "released at the supplier" in error.message
    assert error.extra == {"booking_id": "TRV-GRP-AB12CD", "pnr": PNR}


def test_failed_skypass_release_is_recorded_for_the_job_to_retry(fakes):
    provider = Provider(release_error=RuntimeError("supplier down"))
    with pytest.raises(GroupHoldError) as raised:
        hold(provider)
    _, _, released, code, _, _ = dict(fakes)["mark_release_attempt"]
    assert (released, code) == (False, "RuntimeError")
    assert "mark_unrecorded_released" not in names(fakes)
    assert "Supplier hold not released, release manually" in raised.value.message and PNR in raised.value.message


def test_staff_get_a_short_message_without_supplier_details(fakes):
    with pytest.raises(GroupHoldError) as raised:
        hold(Provider(), viewer=STAFF)
    assert raised.value.message == "The booking couldn't be saved. Ask an admin to check it before trying again."
    assert raised.value.extra == {} and PNR not in raised.value.message
    assert dict(fakes)["mark_unrecorded"]["supplier_pnr"] == PNR  # still kept


def test_provider_without_release_keeps_the_pnr_and_says_it_expires_by_itself(fakes):
    provider = Provider()
    provider.can_release = False
    with pytest.raises(GroupHoldError) as raised:
        hold(provider)
    assert dict(fakes)["mark_unrecorded"]["release_pending"] is False
    assert not any(isinstance(call, tuple) and call[0] == "release" for call in provider.calls)
    assert "Supplier hold not recorded, it expires by itself after 1 hour" in raised.value.message


def test_pnr_survives_in_the_log_when_even_the_record_fails(fakes, caplog):
    with patch.object(store, "mark_unrecorded", MagicMock(side_effect=IntegrityError())), pytest.raises(GroupHoldError) as raised:
        hold(Provider())
    assert any(getattr(r, "pnr", None) == PNR and r.levelname == "CRITICAL" for r in caplog.records)
    assert "note the PNR now" in raised.value.message


@pytest.mark.parametrize("failure, status", [(SupplierRefused("sold out"), 409), (RuntimeError("timeout"), 502)])
def test_supplier_failure_marks_the_record_failed_and_leaves_nothing_pending(fakes, failure, status):
    with pytest.raises(GroupHoldError) as raised:
        hold(Provider(hold_error=failure))
    assert raised.value.status == status
    assert names(fakes) == ["create_pending", "mark_hold_failed"]
    assert dict(fakes)["mark_hold_failed"][2] == ("refused" if status == 409 else "not_confirmed")


def test_record_failure_calls_no_supplier(fakes):
    provider = Provider()
    with patch.object(store, "create_pending", MagicMock(side_effect=IntegrityError())), pytest.raises(GroupHoldError) as raised:
        hold(provider)
    assert provider.calls == [] and raised.value.code == "not_saved" and "Nothing was held" in raised.value.message


def test_unrecorded_hold_view_notice_and_staff_cannot_open_it():
    from datetime import datetime, timezone

    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    booking = store.GroupBooking("b-1", "TRV-GRP-AB12CD", None, "dhillu_sahi", store.HOLD_UNRECORDED, "not_issued", "unpaid", None, "PKR",
                                 0, None, None, [], 1, 0, {}, {}, {"supplier_pnr": "41SDS4"})
    with patch.object(group_holds, "provider_for", lambda vendor: None):
        view = group_holds.build_view(booking, None, ADMIN, now)
        assert (view.status, view.supplier_notice) == ("not_recorded", "Supplier hold not recorded, it expires by itself after 1 hour")
        assert not view.can_record_payments and not view.can_issue_tickets
        assert group_holds.build_view(booking, None, STAFF, now).supplier_notice is None
        with patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): booking), pytest.raises(GroupHoldError) as raised:
            asyncio.run(group_holds.read("TRV-GRP-AB12CD", STAFF))
    assert raised.value.status == 404
