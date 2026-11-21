"""Statement CSV reading (travelian#71): exact paisa, explicit date formats, no guessing."""

from datetime import date

import pytest

from app.services import statement_csv as csvs


@pytest.mark.parametrize("raw,minor", [("1,234.50", 123_450), ("Rs 1,234", 123_400), ("PKR 1234.5", 123_450), ("(1,234.50)", -123_450),
                                       ("-20.05", -2_005), ("46700", 4_670_000), ("500.00 CR", 50_000), ("", None), ("-", None)])
def test_amounts_are_exact_paisa(raw, minor):
    assert csvs.parse_amount(raw) == minor


@pytest.mark.parametrize("raw", ["12.345", "1e5", "abc", "1.2.3"])
def test_bad_amounts_are_refused(raw):
    with pytest.raises(csvs.StatementError):
        csvs.parse_amount(raw)


def test_dates_follow_the_chosen_format():
    assert csvs.parse_date("03/04/2026", "DD/MM/YYYY") == date(2026, 4, 3)
    assert csvs.parse_date("03/04/2026", "MM/DD/YYYY") == date(2026, 3, 4)
    assert csvs.parse_date("21-Nov-2026", "DD-MMM-YYYY") == date(2026, 11, 21)
    with pytest.raises(csvs.StatementError):
        csvs.parse_date("2026-11-21", "DD/MM/YYYY")


def test_mapping_reads_lines_and_reports_bad_rows():
    header, rows = csvs.read("﻿Txn ID;Date;Gross;Fee;Net\nT1;21/11/2026;48,101.00;1,250.00;46,851.00\nT2;bad;100;;\n;;;;\n".encode())
    assert header == ["Txn ID", "Date", "Gross", "Fee", "Net"] and len(rows) == 2
    lines, problems = csvs.lines(header, rows, {"reference": "Txn ID", "date": "Date", "amount": "Gross", "fee": "Fee", "net": "Net"}, "DD/MM/YYYY")
    assert [(l.reference, l.amount_minor, l.fee_minor, l.net_minor) for l in lines] == [("T1", 4_810_100, 125_000, 4_685_100)]
    assert problems == ['Row 3: "bad" is not a date in DD/MM/YYYY']
    with pytest.raises(csvs.StatementError, match="aren't in the file"):
        csvs.lines(header, rows, {"amount": "Amount"}, "DD/MM/YYYY")


def test_bank_credits_only():
    header, rows = csvs.read(b"Date,Narration,Debit,Credit\n21/11/2026,IBFT TRV-AB12CD,,46700\n21/11/2026,Charges,250,\n")
    lines, problems = csvs.lines(header, rows, {"date": "Date", "description": "Narration", "debit": "Debit", "credit": "Credit"}, "DD/MM/YYYY",
                                 credits_only=True)
    assert problems == [] and [(l.description, l.amount_minor) for l in lines] == [("IBFT TRV-AB12CD", 4_670_000)]


def test_reconciliation_routes_need_finance(monkeypatch):
    import app.core.field_visibility as fv
    from app.main import app
    from tests.core.test_finance import _client

    try:
        client = _client(monkeypatch, "staff", {fv.RECORD_PAYMENTS, fv.SEE_COST_AND_MARGIN})
        assert client.get("/v1/admin/finance/reconciliation/mismatches").status_code == 403
        assert client.post("/v1/admin/finance/reconciliation/preview", json={"file_name": "a.csv", "content": "a,b\n1,2"}).status_code == 403
        app.dependency_overrides.clear()
        finance_only = _client(monkeypatch, "staff", {fv.FINANCE})
        assert finance_only.post("/v1/admin/finance/reconciliation/supplier-check").status_code == 403
        preview = finance_only.post("/v1/admin/finance/reconciliation/preview", json={"file_name": "a.csv", "content": "Txn,Gross\nT1,100\n"}).json()
        assert preview["columns"] == ["Txn", "Gross"] and preview["rows"] == 1
    finally:
        app.dependency_overrides.clear()
