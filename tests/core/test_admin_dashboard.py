from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routes import admin_dashboard as routes
from app.core.field_visibility import ALL, BackOfficeUser, require_back_office
from app.core.config import Settings
from app.db import bookings as booking_store
from app.db.bookings import BookingRow, BookingsUnavailable
from app.services import admin_dashboard as figures

PKT = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=PKT)


def booking(n, **overrides):
    values = dict(
        booking_id=f"B{n}", pnr=f"PNR{n}", product="flight", provider="SooperFare", status="ticket_issued",
        payment_status="paid", ticket_status="issued", created_at=NOW - timedelta(hours=1),
        hold_expires_at=None, currency="PKR", total_minor=1_000_000, supplier_cost_minor=900_000,
    )
    values.update(overrides)
    return BookingRow(**values)


# ---------- figures ----------

RECORDS = [
    booking(1, status="confirmed", ticket_status="not_issued", payment_status="paid", created_at=datetime(2025, 1, 10, tzinfo=PKT),
            hold_expires_at=NOW + timedelta(hours=3)),
    booking(2, status="held", ticket_status="not_issued", payment_status="unpaid", hold_expires_at=NOW + timedelta(minutes=90)),
    booking(3, status="held", ticket_status="not_issued", payment_status="unpaid", hold_expires_at=NOW + timedelta(hours=5)),
    booking(4, status="held", ticket_status="not_issued", payment_status="unpaid", hold_expires_at=NOW - timedelta(minutes=5)),
    booking(5),
    booking(6, product="hotel", ticket_status=None),
    booking(7, product="tour"),  # retired product: excluded everywhere
    booking(8, status="cancelled", payment_status="unpaid", ticket_status="not_issued"),
]


def test_needs_action_uses_window_setting_and_ignores_date_filter():
    two = figures.build_summary(RECORDS, figures.DashboardQuery(), NOW, window_hours=2, can_see_margin=True)
    today = figures.build_summary(RECORDS, figures.DashboardQuery(period="today"), NOW, window_hours=2, can_see_margin=True)
    assert two.needs_action == today.needs_action
    assert two.needs_action.pending_issuance == 1
    assert two.needs_action.payment_pending == 1  # B3; B2 expires within 2 hours (listed there); overdue B4 excluded
    assert two.needs_action.expiring_holds == 1 and two.needs_action.expiring_window_hours == 2
    wide = figures.build_summary(RECORDS, figures.DashboardQuery(), NOW, window_hours=24, can_see_margin=True)
    assert wide.needs_action.expiring_holds == 2 and wide.needs_action.payment_pending == 0


def test_business_results_follow_filters_and_exclude_retired_products():
    all_time = figures.build_summary(RECORDS, figures.DashboardQuery(), NOW, 2, True).business
    assert all_time.total_bookings == 7  # tour excluded
    today = figures.build_summary(RECORDS, figures.DashboardQuery(period="today"), NOW, 2, True).business
    assert today.total_bookings == 6
    hotels = figures.build_summary(RECORDS, figures.DashboardQuery(product="hotel"), NOW, 2, True).business
    assert hotels.total_bookings == 1 and hotels.issued_tickets == 0
    other = figures.build_summary(RECORDS, figures.DashboardQuery(provider="airsial"), NOW, 2, True).business
    assert other.total_bookings == 0 and other.total_sales.amount_minor == 0


def test_money_is_paisa_and_gross_profit_is_omitted_unless_exact():
    rows = [booking(1), booking(2, total_minor=500_000, supplier_cost_minor=450_000)]
    business = figures.build_summary(rows, figures.DashboardQuery(), NOW, 2, True).business
    assert business.total_sales.amount_minor == 1_500_000 and business.gross_profit.amount_minor == 150_000
    missing_cost = figures.build_summary(rows + [booking(3, supplier_cost_minor=None)], figures.DashboardQuery(), NOW, 2, True).business
    assert missing_cost.total_sales is not None and missing_cost.gross_profit is None
    foreign = figures.build_summary(rows + [booking(4, currency="USD")], figures.DashboardQuery(), NOW, 2, True).business
    assert foreign.total_sales is None and foreign.gross_profit is None
    hidden = figures.build_summary(rows, figures.DashboardQuery(), NOW, 2, can_see_margin=False).business
    assert hidden.gross_profit is None


def test_periods_use_local_calendar():
    start, end, exclusive = figures.period_range("yesterday", NOW)
    assert start == datetime(2026, 11, 4, tzinfo=PKT) and end == datetime(2026, 11, 5, tzinfo=PKT) and exclusive
    start, end, exclusive = figures.period_range("last-month", NOW)
    assert start == datetime(2026, 10, 1, tzinfo=PKT) and end == datetime(2026, 11, 1, tzinfo=PKT)
    assert figures.period_range("last-three-months", datetime(2026, 5, 31, 9, tzinfo=PKT))[0] == datetime(2026, 2, 28, tzinfo=PKT)
    assert figures.period_range("all", NOW) is None


def test_booking_report_has_every_day_of_a_bounded_period():
    rows = [booking(1), booking(2, created_at=NOW - timedelta(days=3), status="held", ticket_status="not_issued")]
    report = figures.build_booking_report(rows, "last-week", NOW)
    assert report.granularity == "day" and report.timezone == "Asia/Karachi"
    assert [bucket.start for bucket in report.buckets][0] == date(2026, 10, 30) and len(report.buckets) == 7
    assert report.buckets[-1].bookings == 1 and report.buckets[-1].issued == 1
    assert sum(bucket.bookings for bucket in report.buckets) == 2
    assert figures.build_booking_report([], "today", NOW).buckets == []
    assert figures.report_granularity(90) == "week" and figures.report_granularity(400) == "month"


def test_attention_queue_dedupes_sorts_and_limits():
    queue = figures.build_attention_queue(RECORDS, figures.DashboardQuery(), NOW, window_hours=2, limit=2)
    assert queue.total == 3
    assert [(item.type, item.booking_id) for item in queue.items] == [("hold_expiring", "B2"), ("pending_issuance", "B1")]  # soonest due first


def test_filter_options():
    options = figures.build_filter_options(RECORDS + [booking(9, provider="AirSial")])
    assert [o.value for o in options.products] == ["flight", "hotel", "umrah"]
    assert [(o.value, o.label) for o in options.providers] == [("airsial", "AirSial"), ("sooperfare", "SooperFare")]


# ---------- routes ----------

def client(monkeypatch, bookings=None, admin=True, viewer=None):
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    if admin:
        user = viewer or BackOfficeUser(user_id="u-admin", role="super_admin", visibility=ALL)
        app.dependency_overrides[require_back_office] = lambda: user
    else:
        def deny():
            raise HTTPException(403, "Back-office access denied.")
        app.dependency_overrides[require_back_office] = deny

    def read():
        if bookings is None:
            raise BookingsUnavailable()
        return bookings

    monkeypatch.setattr(booking_store, "list_dashboard_bookings", read)
    monkeypatch.setattr(routes, "_now", lambda: NOW)
    return TestClient(app)


def test_summary_route_returns_paisa_and_omits_inexact_fields(monkeypatch):
    response = client(monkeypatch, RECORDS).get("/v1/admin/dashboard/summary", params={"period": "all"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "gateway_bookings"
    assert body["needs_action"]["expiring_window_hours"] == 2
    # Sales: B1 (paid, confirmed), B5 (issued), B6 (hotel). Tour and unpaid/cancelled excluded.
    assert body["business"]["total_sales"] == {"amount_minor": 3_000_000, "currency": "PKR"}
    missing = client(monkeypatch, [booking(1, supplier_cost_minor=None)]).get("/v1/admin/dashboard/summary").json()
    assert "gross_profit" not in missing["business"]


def test_routes_validate_input(monkeypatch):
    api = client(monkeypatch, RECORDS)
    assert api.get("/v1/admin/dashboard/summary", params={"period": "forever"}).status_code == 422
    assert api.get("/v1/admin/dashboard/summary", params={"product": "visa"}).status_code == 422
    assert api.get("/v1/admin/attention-queue", params={"limit": 0}).status_code == 422
    assert api.get("/v1/admin/attention-queue", params={"limit": 3}).json()["total"] == 3


def test_routes_fail_closed(monkeypatch):
    assert client(monkeypatch, None).get("/v1/admin/dashboard/summary").status_code == 503
    for path in ("/v1/admin/dashboard/summary", "/v1/admin/dashboard/booking-report",
                 "/v1/admin/attention-queue", "/v1/admin/dashboard/filter-options"):
        assert client(monkeypatch, RECORDS, admin=False).get(path).status_code == 403


def test_cors_origins_must_be_exact():
    assert Settings(cors_allowed_origins=["http://localhost:8080"]).cors_allowed_origins == ["http://localhost:8080"]
    for bad in (["*"], ["https://*.travelian.pk"], ["http://localhost:8080/"], ["https://travelian.pk/admin"]):
        with pytest.raises(ValidationError):
            Settings(cors_allowed_origins=bad)
    with pytest.raises(ValidationError):
        Settings(dashboard_expiring_hold_hours=0)


def test_summary_flags_sample_data_from_imports(monkeypatch):
    real = client(monkeypatch, [booking(1)]).get("/v1/admin/dashboard/summary").json()
    assert real["contains_sample_data"] is False
    mixed = client(monkeypatch, [booking(1), booking(2, import_source="legacy_test_csv")]).get(
        "/v1/admin/dashboard/summary", params={"period": "today", "product": "hotel"}
    ).json()
    assert mixed["contains_sample_data"] is True  # regardless of filters


def test_bookings_read_failure_logs_error_type_only(monkeypatch, caplog):
    import logging
    from sqlalchemy.exc import ProgrammingError

    class Boom(Exception):
        pass

    class Engine:
        def begin(self):
            raise ProgrammingError("SELECT secret FROM bookings WHERE pnr='AB12CD'", {"pnr": "AB12CD"}, Boom())

    monkeypatch.setattr(booking_store.agencies, "application_engine", lambda: Engine())
    with caplog.at_level(logging.WARNING, logger="app.db.bookings"), pytest.raises(BookingsUnavailable):
        booking_store.list_dashboard_bookings()
    assert "Bookings read failed: ProgrammingError/Boom" in caplog.text
    assert "AB12CD" not in caplog.text and "SELECT" not in caplog.text


# ---------- only bookings still ahead of their due time (never past ones) ----------

def _pending(n, due):
    return booking(n, status="held", payment_status="paid", ticket_status="not_issued", hold_expires_at=due)


@pytest.mark.parametrize("due, listed", [(NOW + timedelta(hours=4), True), (NOW - timedelta(minutes=1), False), (NOW, False), (None, False)])
def test_pending_issuance_only_while_due_is_ahead(due, listed):
    rows = [_pending(20, due)]
    queue = figures.build_attention_queue(rows, figures.DashboardQuery(), NOW, window_hours=2, limit=10)
    cards = figures.build_summary(rows, figures.DashboardQuery(), NOW, 2, True).needs_action
    assert (queue.total == 1) is listed and cards.pending_issuance == queue.total


def test_past_due_is_hidden_whatever_the_status_and_compared_in_utc():
    # 765A6A-style: held, paid, not issued, hold expired yesterday (stored in Asia/Karachi; compared as instants).
    past = datetime(2026, 11, 4, 1, 36, tzinfo=PKT)
    utc_now = NOW.astimezone(ZoneInfo("UTC"))
    rows = [_pending(21, past), booking(22, status="confirmed", ticket_status="requested", hold_expires_at=past),
            booking(23, status="held", payment_status="unpaid", ticket_status="not_issued", hold_expires_at=past)]
    assert figures.build_attention_queue(rows, figures.DashboardQuery(), utc_now, 2, 10).total == 0
    assert figures.build_summary(rows, figures.DashboardQuery(), utc_now, 2, True).needs_action.model_dump(exclude={"expiring_window_hours"}) == {
        "pending_issuance": 0, "payment_pending": 0, "expiring_holds": 0}


def test_each_card_count_equals_its_rows_in_the_queue_soonest_first():
    rows = [_pending(30, NOW + timedelta(hours=6)), _pending(31, NOW + timedelta(minutes=30)), _pending(32, NOW - timedelta(hours=1)),
            booking(33, status="held", payment_status="unpaid", ticket_status="not_issued", hold_expires_at=NOW + timedelta(hours=8)),
            booking(34, status="held", payment_status="unpaid", ticket_status="not_issued", hold_expires_at=NOW + timedelta(minutes=10)),
            booking(35, status="held", payment_status="unpaid", ticket_status="not_issued", hold_expires_at=NOW - timedelta(minutes=10))]
    queue = figures.build_attention_queue(rows, figures.DashboardQuery(), NOW, window_hours=2, limit=50)
    cards = figures.build_summary(rows, figures.DashboardQuery(), NOW, 2, True).needs_action
    kinds = [item.type for item in queue.items]
    assert cards.pending_issuance == kinds.count("pending_issuance") == 1  # B30 (B31 expires within 2 hours, B32 is past)
    assert cards.payment_pending == kinds.count("payment_pending") == 1  # B33
    assert cards.expiring_holds == kinds.count("hold_expiring") == 2  # B34, B31
    assert [item.booking_id for item in queue.items] == ["B34", "B31", "B30", "B33"]
    assert all(item.due_at > NOW for item in queue.items)
