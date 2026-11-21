"""Statement CSV reading (travelian#71): columns chosen by staff, amounts read exactly into integer paisa.

No column names are assumed: the import says which column holds what (reference, amount, fee, ...). Amounts accept
"1,234.50", "Rs 1,234", "PKR 1234.5", "(1,234.50)" and "-1234.50" (negative); never through floats. Dates use the
format staff choose (Pakistani statements are usually day first).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 20_000
DATE_FORMATS = {"DD/MM/YYYY": ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"],
                "MM/DD/YYYY": ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S"],
                "YYYY-MM-DD": ["%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"],
                "DD-MMM-YYYY": ["%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %B %Y", "%d-%b-%Y %H:%M"]}
FIELDS = ("reference", "description", "date", "amount", "credit", "debit", "fee", "net", "payout_date")
_AMOUNT = re.compile(r"^(?P<neg>-)?(?P<int>\d{1,13})(?:\.(?P<dec>\d{1,2}))?$")


class StatementError(ValueError):
    pass


def parse_amount(raw: str | None) -> int | None:
    """Text -> paisa, or None for an empty cell. Raises StatementError for anything that isn't an amount."""
    text = (raw or "").strip()
    if not text or text in ("-", "—"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace(" ", "")
    text = re.sub(r"^(?:rs\.?|pkr)", "", text, flags=re.I)
    text = re.sub(r"(?:cr|dr)$", "", text, flags=re.I)
    match = _AMOUNT.match(text)
    if not match:
        raise StatementError(f'"{raw}" is not an amount')
    minor = int(match["int"]) * 100 + int((match["dec"] or "").ljust(2, "0"))
    return -minor if (negative or match["neg"]) else minor


def parse_date(raw: str | None, fmt: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    for pattern in DATE_FORMATS.get(fmt, []):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise StatementError(f'"{raw}" is not a date in {fmt}')


def read(content: bytes) -> tuple[list[str], list[list[str]]]:
    """Header and rows of a CSV (UTF-8, with or without BOM; comma, semicolon or tab separated)."""
    if len(content) > MAX_BYTES:
        raise StatementError("The file is larger than 2 MB. Split it by month.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise StatementError("The file has no rows under its header.")
    if len(rows) > MAX_ROWS + 1:
        raise StatementError(f"The file has more than {MAX_ROWS} rows. Split it by month.")
    header = [cell.strip() for cell in rows[0]]
    return header, [[cell.strip() for cell in row] for row in rows[1:]]


@dataclass(frozen=True)
class Line:
    line_no: int
    reference: str | None
    description: str | None
    occurred_on: date | None
    amount_minor: int
    fee_minor: int | None
    net_minor: int | None
    payout_on: date | None


def lines(header: list[str], rows: list[list[str]], mapping: dict[str, str], date_format: str, credits_only: bool = False) -> tuple[list[Line], list[str]]:
    """Map rows to statement lines. Returns (lines, problems): a row that can't be read is reported, never guessed."""
    unknown = [column for column in mapping.values() if column and column not in header]
    if unknown:
        raise StatementError(f"These columns aren't in the file: {', '.join(unknown)}")
    if not (mapping.get("amount") or mapping.get("credit")):
        raise StatementError("Choose the amount column (or the credit column).")
    index = {field: header.index(column) for field, column in mapping.items() if column}
    cell = lambda row, field: row[index[field]] if field in index and index[field] < len(row) else None  # noqa: E731
    out, problems = [], []
    for number, row in enumerate(rows, start=2):
        try:
            amount = parse_amount(cell(row, "amount")) if "amount" in index else None
            if amount is None and "credit" in index:
                amount = parse_amount(cell(row, "credit"))
                debit = parse_amount(cell(row, "debit")) if "debit" in index else None
                if amount is None and debit:
                    amount = -abs(debit)
            if amount is None:
                continue  # an empty row (totals, blank lines)
            if credits_only and amount <= 0:
                continue  # money out of the account: not a customer payment
            out.append(Line(number, (cell(row, "reference") or None), (cell(row, "description") or None),
                            parse_date(cell(row, "date"), date_format), amount, parse_amount(cell(row, "fee")), parse_amount(cell(row, "net")),
                            parse_date(cell(row, "payout_date"), date_format)))
        except StatementError as error:
            problems.append(f"Row {number}: {error}")
    return out, problems
