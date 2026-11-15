"""The booking itinerary / e-ticket as a PDF (admin download, Flights and Group Ticketing), drawn with ReportLab.

Design: the Travelian print design already in the frontend (src/lib/ticket-invoice-pdf.ts): navy #061f52 and teal
#007d82, a navy page border, the Travelian wordmark, section titles with a rule, rounded cards, a navy footer with
Travelian's contact details. Layout ideas from airline itinerary and hold sheets; Travelian branding only.

Content: header (logo, reference, PNR), a status banner (on hold with the expiry in PKT; payment received, ticket not
yet issued; e-ticket issued; expired or cancelled), Booked by / Contact (Travelian's own details), travelers (SR, name,
type, passport number, ticket number when issued, fare) with Total, Paid and Due (the selling price only), one block
per flight, outbound then return, and a terms box only when the booking carries terms.

Times: flight times are local at each airport, as the supplier gave them. Booked, generated and hold-expiry times are
Pakistan time (Asia/Karachi), labelled PKT. Never shown: supplier names or logos, group ids, supplier cost, profit,
other suppliers' references. Airline logos only from files the gateway holds (app/assets/airline-logos), else the
airline name in text. Fonts: DejaVu Sans, bundled (app/assets/fonts), which covers Latin, Greek, Cyrillic and more;
Arabic-script names (e.g. Urdu) print with their letters unjoined (ReportLab doesn't shape Arabic script).
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.platypus import HRFlowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ASSETS = Path(__file__).resolve().parents[1] / "assets"
DATA = Path(__file__).resolve().parents[1] / "data"
PKT = ZoneInfo("Asia/Karachi")

# The Travelian print design (src/lib/ticket-invoice-pdf.ts).
NAVY, TEAL, TEXT, MUTED, LINE = (colors.HexColor(c) for c in ("#061f52", "#007d82", "#172b4d", "#52637c", "#cfd7e3"))
SOFT = colors.HexColor("#f3f6fa")
BANNER = {  # state: (background, border, text)
    "hold": ("#fff6e0", "#e8a317", "#7a4d00"),
    "paid": ("#e6f4f4", "#007d82", "#00585c"),
    "issued": ("#e8f6ee", "#1f8a4c", "#14532d"),
    "closed": ("#fdecec", "#c0392b", "#7f1d1d"),
}
TRAVELIAN = {"name": "Travelian", "phone": "+92 324 4440014", "email": "travelian.pk@gmail.com", "website": "www.travelian.pk"}

#: Airline code -> (name, logo file in app/assets/airline-logos). Logos are files Travelian holds; nothing is hotlinked.
AIRLINES = {
    "9P": ("Fly Jinnah", "fly-jinnah"), "OV": ("Salam Air", "salam-air"), "PA": ("Airblue", "airblue"), "PK": ("PIA", "pia"),
    "PF": ("AirSial", "airsial"), "G9": ("Air Arabia", "air-arabia"), "EK": ("Emirates", "emirates"), "EY": ("Etihad", "etihad"),
    "FZ": ("flydubai", "flydubai"), "XY": ("flynas", "flynas"), "F3": ("flyadeal", "flyadeal"), "J9": ("Jazeera Airways", "jazeera-airways"),
    "KU": ("Kuwait Airways", "kuwait-airways"), "WY": ("Oman Air", "oman-air"), "PC": ("Pegasus", "pegasus"), "QR": ("Qatar Airways", "qatar-airways"),
    "SV": ("Saudia", "saudia"), "UL": ("SriLankan Airlines", "srilankan-airlines"), "TK": ("Turkish Airlines", "turkish-airlines"),
    "J2": ("Azerbaijan Airlines", "azerbaijan-airlines"), "CA": ("Air China", "air-china"),
}
TYPE_LABEL = {"ADT": "Adult", "CHD": "Child", "INF": "Infant"}


# ---------- fonts and styles ----------

@lru_cache(maxsize=1)
def _fonts() -> tuple[str, str]:
    regular, bold = ASSETS / "fonts" / "DejaVuSans.ttf", ASSETS / "fonts" / "DejaVuSans-Bold.ttf"
    if regular.exists() and bold.exists():
        pdfmetrics.registerFont(TTFont("TravelianSans", str(regular)))
        pdfmetrics.registerFont(TTFont("TravelianSans-Bold", str(bold)))
        return "TravelianSans", "TravelianSans-Bold"
    return "Helvetica", "Helvetica-Bold"  # fallback: Latin only


def _p(text: str, size: float = 9, bold: bool = False, color=TEXT, align=TA_LEFT, leading: float | None = None) -> Paragraph:
    regular, heavy = _fonts()
    style = ParagraphStyle("p", fontName=heavy if bold else regular, fontSize=size, leading=leading or size * 1.3, textColor=color, alignment=align)
    return Paragraph((text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), style)


def _label(text: str) -> Paragraph:
    return _p(text.upper(), size=7.5, bold=True, color=TEAL)


def _line(label: str, value: str) -> Paragraph:
    """'BAGGAGE  10+20 kg' on one line: a teal label, then the value."""
    regular, heavy = _fonts()
    escape = lambda text: text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    style = ParagraphStyle("line", fontName=regular, fontSize=8.5, leading=11.5, textColor=TEXT)
    return Paragraph(f'<font name="{heavy}" size="7.5" color="#007d82">{escape(label.upper())}</font>&nbsp;&nbsp;{escape(value)}', style)


# ---------- formatting ----------

@lru_cache(maxsize=1)
def _cities() -> dict[str, str]:
    try:
        return json.loads((DATA / "airport_cities.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def city(code: str | None) -> str:
    return _cities().get((code or "").upper(), "") if code else ""


def money(minor: int | None, currency: str | None) -> str:
    if minor is None:
        return "-"
    prefix = "Rs" if (currency or "PKR") == "PKR" else currency
    return f"{prefix} {minor / 100:,.0f}" if minor % 100 == 0 else f"{prefix} {minor / 100:,.2f}"


def pkt(value: datetime | None) -> str:
    """'15 Nov 2026, 02:56 PKT'."""
    return f"{value.astimezone(PKT):%d %b %Y, %H:%M} PKT" if value else "-"


def _local(value: str | None) -> tuple[str, str]:
    """('Wednesday, 18 Nov 2026', '03:25') from '2026-11-18T03:25' (local time at the airport)."""
    if not value:
        return "", ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value, ""
    return f"{moment:%A}, {moment.day} {moment:%b %Y}", (f"{moment:%H:%M}" if "T" in value else "")


def meal(value) -> str:
    text = str(value).strip() if value is not None else ""
    folded = text.casefold()
    if folded in ("1", "yes", "true", "y"):
        return "Meal included"
    if folded in ("0", "no", "false", "n"):
        return "No meal"
    return text or "Meal info not available"


def airline_of(segment: dict) -> tuple[str | None, str, str | None]:
    """(airline code, airline name, flight number shown as 'OV 522'). The code appears once: in the flight number."""
    number = re.sub(r"[\s-]", "", str(segment.get("flight_number") or "")).upper()
    code = (segment.get("airline_code") or (number[:2] if len(number) > 2 else "") or "").upper() or None
    known = AIRLINES.get(code or "")
    name = (segment.get("airline_name") or "").strip()
    if not name or name.upper() == code:
        name = known[0] if known else name
    shown = f"{number[:2]} {number[2:]}" if code and number.startswith(code) and len(number) > 2 else (number or None)
    return code, name, shown


def _image(path: Path, height: float, align: str = "LEFT") -> Image:
    """An image at `height`, its width from its own proportions (set at creation: ReportLab sizes lazy images itself)."""
    width_px, height_px = ImageReader(str(path)).getSize()
    image = Image(str(path), width=height * width_px / height_px, height=height)
    image.hAlign = align
    return image


def _airline_logo(code: str | None, height: float) -> Image | None:
    known = AIRLINES.get(code or "")
    path = ASSETS / "airline-logos" / f"{known[1]}.png" if known else None
    return _image(path, height) if path and path.exists() else None


# ---------- the document ----------

@dataclass
class Traveler:
    passenger_type: str
    title: str | None
    first_name: str
    last_name: str
    passport: str | None = None
    fare_minor: int | None = None
    ticket_number: str | None = None


@dataclass
class Itinerary:
    reference: str
    product: str
    pnr: str | None
    booking_status: str
    issuance_status: str | None
    payment_status: str | None
    created_at: datetime | None
    hold_expires_at: datetime | None
    currency: str | None
    total_minor: int | None
    paid_minor: int | None
    travelers: list[Traveler] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)
    customer: str | None = None
    terms: list[str] = field(default_factory=list)  # only terms the booking actually carries; none are invented

    @property
    def state(self) -> str:
        if self.booking_status == "ticket_issued" or self.issuance_status == "issued":
            return "issued"
        if self.booking_status in ("cancelled", "expired", "hold_unrecorded"):
            return "closed"
        if self.payment_status == "paid" or (self.total_minor and (self.paid_minor or 0) >= self.total_minor):
            return "paid"
        return "hold"


def _card(rows, widths, extra=()) -> Table:
    table = Table(rows, colWidths=widths)
    table.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, LINE), ("ROUNDEDCORNERS", [6, 6, 6, 6]), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6), *extra]))
    return table


def _section(title: str, width: float) -> KeepTogether:
    return KeepTogether([Spacer(1, 3 * mm), _p(title.upper(), size=10.5, bold=True, color=NAVY),
                         HRFlowable(width="100%", thickness=0.6, color=NAVY, spaceBefore=1, spaceAfter=4)])


def _header(doc: Itinerary, width: float) -> Table:
    logo = _image(ASSETS / "travelian-logo.png", 15 * mm)
    title = "E-TICKET & ITINERARY" if doc.state == "issued" else "BOOKING ITINERARY"
    right = [_p(title, size=13, bold=True, color=NAVY, align=TA_RIGHT), Spacer(1, 2),
             _p(f"Booking reference  {doc.reference}", size=9.5, bold=True, align=TA_RIGHT)]
    if doc.pnr:
        right.append(_p(f"PNR  {doc.pnr}", size=12, bold=True, color=TEAL, align=TA_RIGHT))
    table = Table([[logo, right]], colWidths=[width * 0.5, width * 0.5])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, 0), 1.2, NAVY),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return table


def _banner(doc: Itinerary, width: float) -> Table:
    state = doc.state
    background, border, ink = (colors.HexColor(c) for c in BANNER[state])
    expiry = f"Hold expires {pkt(doc.hold_expires_at)}" if doc.hold_expires_at else ""
    if state == "issued":
        title, detail = "E-ticket issued", "Ticket numbers are listed with each traveler below."
    elif state == "paid":
        title, detail = "Payment received, ticket not yet issued", expiry
    elif state == "closed":
        title = {"cancelled": "This booking was cancelled", "expired": "This booking has expired"}.get(doc.booking_status, "This booking is not active")
        detail = ""
    else:
        title, detail = "Your booking is on hold", expiry
    cells = [[_p(title, size=13, bold=True, color=ink)]] + ([[_p(detail, size=10, bold=True, color=ink)]] if detail else [])
    table = Table(cells, colWidths=[width])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), background), ("BOX", (0, 0), (-1, -1), 1, border), ("ROUNDEDCORNERS", [6, 6, 6, 6]),
                               ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return table


def _booked_by(doc: Itinerary, width: float) -> Table:
    left = [_p(TRAVELIAN["name"], size=10, bold=True, color=NAVY), _line("Booked on", pkt(doc.created_at))]
    if doc.customer:
        left.append(_line("Customer", doc.customer))
    right = [_p(TRAVELIAN["phone"], size=10, bold=True, color=NAVY), _p(f"{TRAVELIAN['email']}  ·  {TRAVELIAN['website']}", size=9)]
    return _card([[left, right]], [width / 2, width / 2], [("LINEAFTER", (0, 0), (0, 0), 0.6, LINE)])


def _travelers(doc: Itinerary, width: float) -> Table:
    issued = doc.state == "issued"
    head = ["SR", "NAME", "TYPE", "PASSPORT"] + (["TICKET NO."] if issued else []) + ["FARE"]
    count = len(head)
    rows = [[_p(h, size=7.5, bold=True, color=colors.white, align=TA_RIGHT if h == "FARE" else TA_LEFT) for h in head]]
    for index, traveler in enumerate(doc.travelers, start=1):
        name = " ".join(part for part in ((traveler.title or "").title(), traveler.first_name, traveler.last_name) if part)
        row = [_p(str(index)), _p(name, bold=True, color=NAVY), _p(TYPE_LABEL.get(traveler.passenger_type, traveler.passenger_type)),
               _p(traveler.passport or "-")]
        if issued:
            row.append(_p(traveler.ticket_number or "-"))
        row.append(_p(money(traveler.fare_minor, doc.currency), align=TA_RIGHT))
        rows.append(row)
    if not doc.travelers:
        rows.append([_p(""), _p("Travelers not recorded", color=MUTED)] + [_p("")] * (count - 2))
    last_traveler = len(rows) - 1
    paid = doc.paid_minor or 0
    for label, value, strong in (("Total", money(doc.total_minor, doc.currency), True), ("Paid", money(paid, doc.currency), False),
                                 ("Due", money(max((doc.total_minor or 0) - paid, 0), doc.currency), True)):
        rows.append([""] * (count - 2) + [_p(label, bold=strong, align=TA_RIGHT), _p(value, size=10 if strong else 9, bold=strong, color=NAVY, align=TA_RIGHT)])
    shares = [0.05, 0.33, 0.09, 0.17] + ([0.19] if issued else []) + [0.17]
    table = Table(rows, colWidths=[width * share / sum(shares) for share in shares], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("ROWBACKGROUNDS", (0, 1), (-1, last_traveler), [colors.white, SOFT]),
                               ("BOX", (0, 0), (-1, last_traveler), 0.8, LINE), ("LINEABOVE", (count - 2, len(rows) - 1), (-1, len(rows) - 1), 0.8, NAVY),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return table


def _leg(doc: Itinerary, segment: dict, width: float) -> KeepTogether:
    code, airline, flight = airline_of(segment)
    origin, destination = (segment.get("origin") or "").upper(), (segment.get("destination") or "").upper()
    direction = "Return" if segment.get("direction") == "return" else "Outbound"
    title = f"{direction}  ·  {city(origin) or origin}  to  {city(destination) or destination}"
    bar = Table([[_p(title, size=10.5, bold=True, color=colors.white)]], colWidths=[width])
    bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TEAL), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("ROUNDEDCORNERS", [6, 6, 0, 0])]))

    def end(prefix: str, airport: str, when: str | None) -> list:
        day, clock = _local(when)
        cells = [_label("Departure" if prefix == "departure" else "Arrival")]
        if day:
            cells.append(_p(day, size=8.5))
        cells.append(_p(clock, size=18, bold=True, color=NAVY, leading=21) if clock else _p("Time not provided", size=8.5, color=MUTED))
        cells.append(_p(airport or "-", size=11.5, bold=True, color=NAVY))
        if city(airport):
            cells.append(_p(city(airport), size=8.5, color=MUTED))
        if segment.get(f"{prefix}_terminal"):
            cells.append(_p(f"Terminal {segment[f'{prefix}_terminal']}", size=8, color=MUTED))
        return cells

    status = {"issued": "Ticketed", "paid": "Confirmed, not ticketed", "hold": "On hold"}.get(doc.state) or doc.booking_status.replace("_", " ").capitalize()
    logo = _airline_logo(code, 8 * mm)
    side = ([logo, Spacer(1, 2)] if logo else []) + [_p(airline or "Airline not recorded", size=9.5, bold=True, color=NAVY)]
    if flight:
        side.append(_p(f"Flight {flight}", size=9, bold=True, color=TEAL))
    for label, value in (("Cabin", segment.get("cabin")), ("Baggage", segment.get("baggage") or "Baggage info not available"),
                         ("Meal", meal(segment.get("meal"))), ("Status", status)):
        if value:
            side.append(_line(label, str(value)))
    body = _card([[end("departure", origin, segment.get("departure_local")), _p("→", size=18, color=MUTED),
                   end("arrival", destination, segment.get("arrival_local")), side]],
                 [width * 0.31, width * 0.07, width * 0.31, width * 0.31],
                 [("LINEBEFORE", (3, 0), (3, 0), 0.6, LINE), ("VALIGN", (1, 0), (1, 0), "MIDDLE"), ("ROUNDEDCORNERS", [0, 0, 6, 6])])
    return KeepTogether([bar, body, Spacer(1, 3 * mm)])


def render(doc: Itinerary, now: datetime) -> bytes:
    buffer = io.BytesIO()
    page_w, page_h = A4
    margin = 14 * mm
    width = page_w - 2 * margin
    regular, _ = _fonts()

    def frame(canvas, document) -> None:
        canvas.saveState()
        canvas.setStrokeColor(NAVY)
        canvas.setLineWidth(4)
        canvas.rect(6 * mm, 6 * mm, page_w - 12 * mm, page_h - 12 * mm)
        canvas.setFillColor(NAVY)
        canvas.rect(6 * mm, 6 * mm, page_w - 12 * mm, 11 * mm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont(regular, 8.5)
        canvas.drawCentredString(page_w / 2, 10.5 * mm, f"{TRAVELIAN['phone']}   ·   {TRAVELIAN['email']}   ·   {TRAVELIAN['website']}")
        canvas.setFillColor(MUTED)
        canvas.setFont(regular, 7)
        canvas.drawString(margin, 19.5 * mm, f"Generated {pkt(now)}. Flight times are local at each airport.")
        canvas.drawRightString(page_w - margin, 19.5 * mm, f"Page {document.page}")
        canvas.restoreState()

    template = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=26 * mm,
                                 title=f"Travelian {doc.reference}", author="Travelian", subject="Booking itinerary", creator="Travelian")
    story = [_header(doc, width), Spacer(1, 4 * mm), _banner(doc, width),
             _section("Booked by / Contact", width), _booked_by(doc, width),
             _section("Travelers", width), _travelers(doc, width),
             _section("Flights", width)]
    if doc.segments:
        story += [_leg(doc, segment, width) for segment in doc.segments]
    else:
        story.append(_p("Flight details were not recorded for this booking.", color=MUTED))
    if doc.terms:
        story += [_section("Terms", width),
                  _card([[_p(f"{index}.", bold=True, color=TEAL), _p(term)] for index, term in enumerate(doc.terms, start=1)], [8 * mm, width - 8 * mm])]
    template.build(story, onFirstPage=frame, onLaterPages=frame)
    return buffer.getvalue()
