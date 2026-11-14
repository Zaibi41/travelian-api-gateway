"""The booking itinerary as a PDF (admin download, Flights and Group Ticketing).

A small, dependency-free PDF writer: text lines in the standard Helvetica fonts (every PDF reader has them), A4, as many
pages as needed. Text outside Latin-1 is replaced by "?" (the standard fonts can't draw it).

What the document shows: booking reference, PNR (an airline PNR for flights; for group bookings the PNR is the
supplier's, so only for viewers with the supplier names permission), status, the customer, the travelers (names and
type, never documents or dates of birth), every flight (outbound then return), and the selling price, paid and due.
Never a supplier name, group id, supplier cost or profit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

PAGE_W, PAGE_H, MARGIN = 595, 842, 50
FONTS = {False: "F1", True: "F2"}  # regular, bold


@dataclass
class _Line:
    text: str
    size: float = 10
    bold: bool = False
    gap: float = 0  # extra space before the line


@dataclass
class Document:
    lines: list[_Line] = field(default_factory=list)

    def title(self, text: str) -> None:
        self.lines.append(_Line(text, 18, True))

    def heading(self, text: str) -> None:
        self.lines.append(_Line(text, 12, True, gap=10))

    def text(self, text: str, bold: bool = False) -> None:
        self.lines.append(_Line(text, 10, bold))

    def row(self, label: str, value: str) -> None:
        self.lines.append(_Line(f"{label}: {value}", 10))

    def render(self) -> bytes:
        return _pdf(self._pages())

    def _pages(self) -> list[list[tuple[float, float, _Line]]]:
        pages: list[list[tuple[float, float, _Line]]] = [[]]
        y = PAGE_H - MARGIN
        for line in self.lines:
            y -= line.gap
            for part in _wrap(line.text, line.size):
                y -= line.size * 1.45
                if y < MARGIN:
                    pages.append([])
                    y = PAGE_H - MARGIN - line.size * 1.45
                pages[-1].append((MARGIN, y, _Line(part, line.size, line.bold)))
        return pages


def _wrap(text: str, size: float) -> list[str]:
    """Rough wrap at the page width (Helvetica averages about half the font size per character)."""
    width = int((PAGE_W - 2 * MARGIN) / (size * 0.5))
    words, lines, current = text.split(" "), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + [current] if current or not lines else lines


def _escape(text: str) -> str:
    latin = text.encode("latin-1", "replace").decode("latin-1")
    return latin.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\r", " ").replace("\n", " ")


def _pdf(pages: list[list[tuple[float, float, _Line]]]) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled below
    pages_id = add(b"")
    regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    page_ids = []
    for page in pages or [[]]:
        content = "\n".join(f"BT /{FONTS[line.bold]} {line.size:g} Tf {x:.1f} {y:.1f} Td ({_escape(line.text)}) Tj ET" for x, y, line in page)
        stream = content.encode("latin-1")
        content_id = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                            f"/Resources << /Font << /F1 {regular} 0 R /F2 {bold} 0 R >> >> /Contents {content_id} 0 R >>".encode()))
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(page_ids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


# ---------- the itinerary ----------

TYPE_LABEL = {"ADT": "Adult", "CHD": "Child", "INF": "Infant"}
STATUS_LABEL = {"held": "On hold", "pending_payment": "Pending payment", "payment_received": "Payment received", "confirmed": "Confirmed",
                "ticket_issued": "Ticket issued", "cancelled": "Cancelled", "expired": "Expired"}


def _money(minor: int | None, currency: str | None) -> str:
    if minor is None:
        return "-"
    prefix = "Rs" if (currency or "PKR") == "PKR" else currency
    return f"{prefix} {minor / 100:,.0f}" if minor % 100 == 0 else f"{prefix} {minor / 100:,.2f}"


def _when(value: str | None) -> str:
    """'2026-12-02T06:40' -> '2 Dec 2026 06:40' (local time at the airport, as the supplier sent it)."""
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    text = f"{parsed.day} {parsed:%b %Y}"
    return f"{text} {parsed:%H:%M}" if "T" in value else text


def _meal(value) -> str | None:
    """Suppliers send 1/0, Yes/No: 'Meal' or 'No meal'; anything else is left out."""
    text = str(value).strip().casefold() if value is not None else ""
    return "Meal" if text in ("1", "yes", "true", "y") else "No meal" if text in ("0", "no", "false", "n") else None


def _instant(value: datetime | None) -> str:
    return f"{value.astimezone(timezone.utc):%d %b %Y %H:%M} UTC" if value else "-"


def itinerary(*, reference: str, product: str, pnr: str | None, status: str, created_at: datetime | None, hold_expires_at: datetime | None,
              customer: str | None, travelers: list[tuple[str, str, str, str]], segments: list[dict], currency: str | None,
              total_minor: int | None, paid_minor: int | None, now: datetime) -> bytes:
    """travelers: (type, title, first name, last name). segments: the booking's itinerary as saved (outbound, then return)."""
    doc = Document()
    doc.title("Travelian - Itinerary")
    doc.row("Booking reference", reference)
    if pnr:
        doc.row("PNR", pnr)
    doc.row("Product", "Group Ticketing" if product == "group" else "Flight")
    doc.row("Status", STATUS_LABEL.get(status, status.replace("_", " ").capitalize()))
    if created_at:
        doc.row("Booked", _instant(created_at))
    if status in ("held", "pending_payment") and hold_expires_at:
        doc.row("Hold expires", _instant(hold_expires_at))

    if customer:
        doc.heading("Customer")
        doc.text(customer)

    doc.heading("Travelers")
    if travelers:
        for index, (kind, title, first, last) in enumerate(travelers, start=1):
            doc.text(f"{index}. {(title or '').title()} {first} {last} ({TYPE_LABEL.get(kind, kind)})".replace("  ", " "))
    else:
        doc.text("Not recorded")

    doc.heading("Flights")
    if segments:
        for segment in segments:
            direction = "Return" if segment.get("direction") == "return" else "Outbound"
            route = f"{segment.get('origin') or '-'} to {segment.get('destination') or '-'}"
            flight = " ".join(part for part in (segment.get("airline_name"), segment.get("flight_number")) if part)
            doc.text(f"{direction}: {route}", bold=True)
            doc.text(f"Departs {_when(segment.get('departure_local'))}" + (f", arrives {_when(segment.get('arrival_local'))}" if segment.get("arrival_local") else ""))
            details = [flight, f"Baggage {segment['baggage']}" if segment.get("baggage") else None, _meal(segment.get("meal"))]
            if any(details):
                doc.text(", ".join(part for part in details if part))
    else:
        doc.text("Flight details were not recorded for this booking.")

    doc.heading("Price")
    doc.row("Total", _money(total_minor, currency))
    if paid_minor is not None:
        doc.row("Paid", _money(paid_minor, currency))
        doc.row("Due", _money(max((total_minor or 0) - paid_minor, 0), currency))

    doc.lines.append(_Line(f"Generated {_instant(now)}. Times of flights are local at each airport.", 8, gap=14))
    return doc.render()
