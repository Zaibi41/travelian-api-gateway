"""gateway#109 against a disposable database: the expiry job and reads leave a paid-in-full group hold held (waiting for
the final PNR, which staff can still record), turn a part-paid one into paid_not_ticketed (staff alerted), and expire an
unpaid one as before. Also the owner's repair command for bookings expired before the fix. Fake provider only."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.cli import repair_paid_group_expiry as repair
from app.db import booking_people
from app.db import bookings as booking_store
from app.db import group_holds as store
from app.core.field_visibility import ALL
from app.services import admin_bookings, group_hold_expiry, group_holds
from app.services.group_holds import NO_RELEASE_SOURCES
from tests.database.test_group_hold_expiry import fake, place_at, run  # noqa: F401 - fixture
from tests.database.test_booking_people import ACTOR
from tests.database.test_group_holds import ADMIN, ADULT_1, ADULT_2, request, tickets

TOTAL = 210_000_00  # 2 adults x (100,000 + 5%)


def pay(reference, amount):
    from app.db import booking_payments, payment_accounts
    account = next((a for a in payment_accounts.list_accounts() if a.active and a.method == "bank_transfer"), None) \
        or payment_accounts.add("bank_transfer", "Meezan Bank, Travelian current account")
    return booking_payments.record(reference, amount_minor=amount, currency="PKR", method="bank_transfer", account_id=account.id,
                                   payer_reference=f"TXN-{reference}", received_at=datetime.now(timezone.utc), actor_user_id=ACTOR)


_SURNAMES = iter(["Raza", "Khan", "Malik", "Butt", "Sheikh", "Qureshi", "Ahmed", "Iqbal", "Hussain", "Chaudhry"] * 3)


def placed(fake_provider, paid: int):
    """A hold made 25 minutes ago (30-minute hold time) with `paid` recorded while it was open. Each hold has its own
    travelers: the double-booking check refuses the same passengers on the same route and date."""
    surname = next(_SURNAMES)
    travelers = [{**ADULT_1, "last_name": surname}, {**ADULT_2, "last_name": surname}]
    view = place_at(datetime.now(timezone.utc) - timedelta(minutes=25), request(passengers=travelers))
    if paid:
        pay(view.booking_id, paid)
    return view.booking_id


def later():
    return datetime.now(timezone.utc) + timedelta(minutes=10)  # past the time limit


def stored(database, reference):
    connection, _, _ = database
    return connection.execute(text("SELECT booking_status, supplier_response -> 'release' ->> 'status' FROM bookings WHERE reference = :r"),
                              {"r": reference}).one()


def actions(database, reference):
    connection, _, _ = database
    return connection.execute(text("SELECT action FROM booking_events e JOIN bookings b ON b.id = e.booking_id WHERE b.reference = :r"
                                   " ORDER BY e.created_at, e.id"), {"r": reference}).scalars().all()


def notified(request, reference):
    """Staff notifications recorded for the booking (tests/conftest.py keeps them instead of writing them; no email)."""
    return [kind for kind, ref, _ in request.node.notifications if ref == reference]


def test_paid_hold_past_its_time_is_not_expired_and_the_final_pnr_can_still_be_recorded(fake, database, request):
    reference = placed(fake, TOTAL)
    outcome = run(group_hold_expiry.run_once(later()))
    assert reference not in outcome["expired"] and reference not in outcome["paid_not_ticketed"]
    assert tuple(stored(database, reference)) == ("held", None)
    assert "hold_expired" not in actions(database, reference) and "paid_not_ticketed" not in notified(request, reference)
    view = run(group_holds.read(reference, ADMIN))  # the read path doesn't expire it either
    assert view.status == "held" and view.ticketing_status == "awaiting_final_pnr" and view.can_edit_tickets
    # The final-PNR tracker and the Bookings detail still see it as paid, waiting for the final PNR.
    row = admin_bookings.find_booking(booking_store.list_dashboard_bookings(), reference)
    assert row.status == "held" and row.payment_status == "paid" and not row.final_pnr
    detail = admin_bookings.build_detail(row, ALL, booking_people.read(reference), can_issue_tickets=True)
    assert detail.group_ticketing_status == "awaiting_final_pnr" and detail.can_edit_group_tickets
    # Staff record the final PNR after the time limit: confirmed.
    confirmed = run(group_holds.record_tickets(reference, ADMIN, *tickets()))
    assert confirmed.status == "confirmed" and confirmed.final_pnr == "K9QX2L"
    assert run(group_hold_expiry.run_once(later()))["expired"] == []


def test_unpaid_hold_past_its_time_still_expires(fake, database):
    reference = placed(fake, 0)
    outcome = run(group_hold_expiry.run_once(later()))
    assert outcome["expired"] == [reference] and outcome["paid_not_ticketed"] == []
    assert stored(database, reference)[0] == "expired"
    view = run(group_holds.read(reference, ADMIN))
    assert view.status == "expired" and not view.can_edit_tickets and view.ticketing_status is None


def test_part_paid_hold_past_its_time_becomes_paid_not_ticketed_and_staff_are_alerted(fake, database, request):
    reference = placed(fake, 50_000_00)
    outcome = run(group_hold_expiry.run_once(later()))
    assert outcome["paid_not_ticketed"] == [reference] and outcome["expired"] == []
    status, release = stored(database, reference)
    assert status == "paid_not_ticketed" and release == ("not_available" if fake_vendor_has_no_release() else "pending")
    assert "hold_expired" in actions(database, reference)
    assert notified(request, reference)[-1] == "paid_not_ticketed"
    view = run(group_holds.read(reference, ADMIN))
    assert view.status == "paid_not_ticketed" and not view.can_record_payments and not view.can_edit_tickets
    # Once only: the next run changes nothing and alerts nobody again.
    again = run(group_hold_expiry.run_once(later()))
    assert again["paid_not_ticketed"] == [] and notified(request, reference).count("paid_not_ticketed") == 1


def test_reading_a_part_paid_hold_past_its_time_records_paid_not_ticketed(fake, database):
    reference = placed(fake, 50_000_00)
    booking = store.expire_if_over(reference, later(), frozenset(NO_RELEASE_SOURCES))
    assert booking.booking_status == "paid_not_ticketed" and stored(database, reference)[0] == "paid_not_ticketed"


def fake_vendor_has_no_release() -> bool:
    from tests.database.test_group_holds import VENDOR
    return VENDOR in NO_RELEASE_SOURCES


# ---------- the owner's repair command ----------

def expire_the_old_way(database, reference, release_status="not_available"):
    """What the job did before the fix: expired whatever was paid (with its release state and log entry)."""
    connection, _, _ = database
    booking = store.load(reference)
    with store.agencies.application_engine().begin() as conn:
        store._expire(conn, booking, later(), frozenset(NO_RELEASE_SOURCES))
        conn.execute(text("UPDATE bookings SET supplier_response = jsonb_set(supplier_response, '{release,status}', to_jsonb(CAST(:s AS text)))"
                          " WHERE reference = :r"), {"s": release_status, "r": reference})


def test_repair_dry_run_lists_and_changes_nothing_then_apply_corrects_once(fake, database):
    full = placed(fake, TOTAL)
    released = placed(fake, TOTAL)
    part = placed(fake, 50_000_00)
    unpaid = placed(fake, 0)
    for reference in (full, part, unpaid):
        expire_the_old_way(database, reference)
    expire_the_old_way(database, released, "released")
    staff_released = placed(fake, TOTAL)  # a staff release (hold_released) is a staff decision: never touched
    connection, _, _ = database
    with store.agencies.application_engine().begin() as conn:
        booking = store.load(staff_released)
        conn.execute(text("UPDATE bookings SET booking_status = 'expired' WHERE id = :b"), {"b": booking.id})
        store._event(conn, booking.id, None, "hold_expired", {})
        store._event(conn, booking.id, ADMIN.user_id, "hold_released", {})

    planned = {item.reference: item.to for item in repair.plan()}
    assert planned == {full: "held", released: "paid_not_ticketed", part: "paid_not_ticketed"}
    assert all(stored(database, reference)[0] == "expired" for reference in (full, released, part, unpaid, staff_released))  # dry run

    assert repair.main([]) == 0  # the default is the dry run
    assert stored(database, full)[0] == "expired"

    done = {item.reference: item.to for item in repair.apply()}
    assert done == planned
    assert tuple(stored(database, full)) == ("held", None)  # the pending release is dropped: the seats stay
    assert stored(database, released)[0] == "paid_not_ticketed" and stored(database, part)[0] == "paid_not_ticketed"
    assert stored(database, unpaid)[0] == "expired" and stored(database, staff_released)[0] == "expired"
    assert actions(database, full)[-1] == "hold_expiry_corrected"
    corrected = connection.execute(text("SELECT details FROM booking_events e JOIN bookings b ON b.id = e.booking_id"
                                        " WHERE b.reference = :r AND e.action = 'hold_expiry_corrected'"), {"r": full}).scalar_one()
    assert corrected["previous_status"] == "expired" and corrected["status"] == "held" and corrected["release"]["status"] == "not_available"

    view = run(group_holds.read(full, ADMIN))
    assert view.status == "held" and view.ticketing_status == "awaiting_final_pnr" and view.can_edit_tickets
    assert run(group_hold_expiry.run_once(later()))["expired"] == []  # the job leaves it alone now
    assert repair.apply() == [] and repair.plan() == []  # idempotent


@pytest.mark.parametrize("argv", [[], ["--apply"]])
def test_repair_with_nothing_to_do(fake, database, argv, capsys):
    assert repair.main(argv) == 0
    assert "0 booking(s)" in capsys.readouterr().out
