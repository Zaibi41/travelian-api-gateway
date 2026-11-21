"""Reports (travelian#70) without a database: grouping, totals with unknown cost, xlsx output, permissions."""

import io
import zipfile
from datetime import date, datetime, timezone

import pytest

import app.core.field_visibility as fv
from app.db.bookings import BookingRow
from app.main import app
from app.services import finance_reports as reports, xlsx
from app.services.finance_profit import ProfitInputs, calculate
from tests.core.test_finance import _client


def row(ref, total, cost, created, status="ticket_issued", cost_known=True, currency="PKR"):
    return BookingRow(booking_id=ref, pnr=None, product="flight", provider="southair", status=status, payment_status="paid", ticket_status="issued",
                      created_at=created, hold_expires_at=None, currency=currency, total_minor=total, supplier_cost_minor=cost, cost_known=cost_known)


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


def test_sales_grouped_by_pkt_day_week_month_and_unknown_cost_counted_apart():
    late = datetime(2026, 11, 20, 20, 30, tzinfo=timezone.utc)   # 21 Nov in Pakistan
    rows = [row("A", 100_000, 90_000, late), row("B", 50_000, 50_000, late, cost_known=False),
            row("C", 70_000, 60_000, datetime(2026, 11, 2, 6, tzinfo=timezone.utc)), row("D", 1, 1, late, status="cancelled")]
    sales = reports.sales_in(rows, date(2026, 11, 1), date(2026, 11, 30))
    assert [r.booking_id for r in sales] == ["A", "B", "C"]
    profits = {r.booking_id: reports.profit_for(r, reports.Extras()) for r in sales}
    days = dict(reports.by_period(sales, profits, "day"))
    assert set(days) == {date(2026, 11, 21), date(2026, 11, 2)}
    day21 = days[date(2026, 11, 21)]
    assert (day21.bookings, day21.sales_minor, day21.profit_minor, day21.profit_not_known) == (2, 150_000, 10_000, 1)
    months = dict(reports.by_period(sales, profits, "month"))
    assert months[date(2026, 11, 1)].profit_minor == 20_000
    weeks = dict(reports.by_period(sales, profits, "week"))
    assert date(2026, 11, 16) in weeks  # Monday of 21 Nov


def test_booking_profit_uses_the_shared_formula():
    extras = reports.Extras(charges=[85_000], fees=[2_000], card_fees=3_000, refunds_paid=[1_000])
    p = reports.profit_for(row("A", 100_000, 90_000, datetime.now(timezone.utc)), extras)
    expected = calculate(ProfitInputs(100_000, 90_000, True, 3_000, [85_000], [2_000], [1_000]))
    assert p.profit_minor == expected.profit_minor == 15_000


def test_receivables_are_open_bookings_with_money_due():
    rows = [row("A", 100_000, 0, None, status="held"), row("B", 100_000, 0, None, status="cancelled"), row("C", 100_000, 0, None, status="held")]
    assert [(r.booking_id, due) for r, due in reports.receivables(rows, {"A": 40_000, "C": 100_000})] == [("A", 60_000)]


def test_xlsx_is_a_valid_workbook_with_numbers_and_safe_text():
    data = xlsx.workbook({"Summary": [["Booking", "Profit (Rs)"], ["=HYPERLINK(1)", xlsx.rupees(-2_050)]]})
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        assert "<v>-20.50</v>" in sheet and "<f>" not in sheet and 't="inlineStr"' in sheet
        assert "xl/workbook.xml" in z.namelist()


@pytest.mark.parametrize("role,granted", [("staff", {fv.SEE_COST_AND_MARGIN}), ("agency_owner", {fv.FINANCE}), ("customer", set())])
def test_reports_need_finance(monkeypatch, role, granted):
    client = _client(monkeypatch, role, granted)
    for path in ("/v1/admin/finance/reports", "/v1/admin/finance/reports.xlsx", "/v1/admin/finance/reports.csv"):
        assert client.get(path).status_code == 403


def test_finance_without_cost_gets_no_profit_or_supplier_figures(monkeypatch):
    from app.api.routes import finance as routes
    from app.db import bookings, finance, finance_payments

    late = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(bookings, "list_dashboard_bookings", lambda: [row("A", 100_000, 90_000, late)])
    monkeypatch.setattr(reports, "load_extras", lambda refs: {r: reports.Extras() for r in refs})
    monkeypatch.setattr(reports, "paid_by_booking", lambda refs: {"A": 100_000})
    monkeypatch.setattr(reports, "payments_by_method", lambda start, end: [("payfast", "PayFast", 1, 100_000, 3_000)])
    monkeypatch.setattr(reports, "supplier_spend", lambda start, end: [(date(2026, 11, 20), "southair", 90_000, 0)])
    monkeypatch.setattr(finance_payments, "refunds_due", lambda: [])
    monkeypatch.setattr(finance, "list_refunds", lambda *a: ([], 0))
    staff = _client(monkeypatch, "staff", {fv.FINANCE}).get("/v1/admin/finance/reports").json()
    assert staff["totals"]["sales"]["amount_minor"] == 100_000 and staff["totals"].get("profit") is None
    assert staff.get("by_booking") is None and staff.get("supplier_spend") is None
    app.dependency_overrides.clear()
    admin = _client(monkeypatch, "super_admin").get("/v1/admin/finance/reports").json()
    assert admin["totals"]["profit"]["amount_minor"] == 10_000 and admin["supplier_spend"][0]["supplier"] == "Southair"
    assert routes is not None
    app.dependency_overrides.clear()
    csv = _client(monkeypatch, "staff", {fv.FINANCE}).get("/v1/admin/finance/reports.csv?section=profit_by_booking")
    assert csv.status_code == 403
