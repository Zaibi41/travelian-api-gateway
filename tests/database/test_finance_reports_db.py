"""Reports with a database (travelian#70): the report, the booking's Fare profit and the dashboard tile agree."""

from datetime import datetime, timezone

from sqlalchemy import text

from app.core.field_visibility import ALL, BackOfficeUser
from app.api.routes.finance import build_report
from app.db import booking_people, finance
from app.db import bookings as store
from app.services import admin_bookings, admin_dashboard, finance_reports
from app.services.admin_dashboard import DashboardQuery
from tests.database.test_finance_foundations import WHO, fresh, pay

ADMIN = BackOfficeUser("11111111-1111-4111-8111-111111111111", "super_admin", ALL)


def test_report_booking_and_dashboard_use_the_same_profit(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)            # selling 46,700 / cost 45,770
    payment = pay(reference, 46_700)
    finance.add_payment_fee(payment.id, WHO, amount_minor=300, reason="Bank charge")
    finance.add_supplier_charge(reference, WHO, amount_minor=45_000, reason="Portal")
    old = fresh(connection, metadata, pnr="OLD999", supplier_total_minor=46_700)
    pay(old, 46_700, "TXN-OLD")
    connection.execute(text("UPDATE bookings SET cost_known = false WHERE reference = :r"), {"r": old})

    report = build_report(ADMIN, None, None, "day", None)
    assert report.totals.bookings == 2 and report.totals.profit_not_known == 1
    assert report.totals.profit.amount_minor == 46_700 - 45_000 - 300 == 1_400
    assert report.totals.sales.amount_minor == 93_400
    assert {m.method for m in report.payments_by_method} == {"bank_transfer"}
    assert [b.booking_reference for b in report.by_booking if b.profit is None] == [old]

    rows = {r.booking_id: r for r in store.list_dashboard_bookings()}
    detail = admin_bookings.build_detail(rows[reference], ALL, booking_people.read(reference))
    assert detail.fare.profit.profit.amount_minor == 1_400 and detail.fare.profit.supplier_charge_basis == "recorded"

    summary = admin_dashboard.build_summary(list(rows.values()), DashboardQuery(period="all"), datetime.now(timezone.utc), 24, True,
                                            profit_loader=finance_reports.profits)
    pkr = next(a for a in summary.business.gross_profit if a.currency == "PKR")
    assert (pkr.amount_minor, pkr.profit_not_known) == (1_400, 1)
