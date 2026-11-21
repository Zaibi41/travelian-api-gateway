"""A minimal Excel (.xlsx) writer for finance exports: sheets of plain rows, no dependency.

Text cells are inline strings (never formulas, so nothing in a cell runs in Excel); numbers are written as numbers.
Money is passed in already as exact rupee Decimals built from integer paisa by the caller.
"""

from __future__ import annotations

import io
import re
import zipfile
from decimal import Decimal
from xml.sax.saxutils import escape

_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _col(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _cell(ref: str, value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(_BAD_XML.sub("", str(value)))}</t></is></c>'


def _sheet(rows: list[list]) -> str:
    body = "".join(f'<row r="{r + 1}">' + "".join(_cell(f"{_col(c)}{r + 1}", v) for c, v in enumerate(row)) + "</row>"
                   for r, row in enumerate(rows))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + body + "</sheetData></worksheet>")


def workbook(sheets: dict[str, list[list]]) -> bytes:
    names = [re.sub(r"[\[\]:*?/\\]", " ", name)[:31] or f"Sheet{i + 1}" for i, name in enumerate(sheets)]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(names)))
                   + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                             f'Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(names))) + "</Relationships>")
        for i, rows in enumerate(sheets.values()):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet(rows))
    return out.getvalue()


def rupees(minor: int | None) -> Decimal | None:
    """Integer paisa -> exact rupees for a number cell."""
    return None if minor is None else Decimal(minor).scaleb(-2)
