"""Travelian's email templates: every message to customers and staff, as (subject, HTML, plain text).

One shared layout (logo header, coloured status strip, content, contact footer), table-based with inline CSS so it
renders the same in Gmail, Outlook, Apple Mail and on phones. Every value is escaped. Customer templates take no
supplier name, cost or internal note: the data they accept has no such fields. Staff alerts carry everything staff
need to act, with a red URGENT banner for paid-but-not-ticketed bookings.

Times: the hold deadline is shown in Pakistan time (PKT, UTC+5) with the UTC time under it, so nobody misses it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape

from app.core.config import settings

# ---------- brand (from the website: src/index.css, admin-tokens.css, site-config.ts) ----------

NAVY = "#00245D"          # brand navy: headings, header text, buttons
BLUE = "#073C92"          # primary
TEAL = "#1A8B93"
TEAL_SOFT = "#E7F7F8"
INK = "#0F1729"
MUTED = "#5B6475"
LINE = "#E5E7EB"
PAGE = "#F3F5F9"
AMBER_BG, AMBER_LINE, AMBER_INK = "#FFF8E6", "#F5C451", "#7A4A00"
RED, RED_SOFT, RED_INK = "#C81E1E", "#FDECEC", "#8A1414"
GREEN_SOFT, GREEN_INK = "#E8F6EF", "#1D6B47"

FONT = "'Inter', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
DISPLAY = "'Plus Jakarta Sans', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
MONO = "'SFMono-Regular', Menlo, Consolas, 'Courier New', monospace"

PHONE = "+92 324 4440014"
WHATSAPP = "https://wa.me/923244440014"
EMAIL = "info@travelian.pk"
ADDRESS = "589-J2, Johar Town, Lahore"
SOCIAL = (("Facebook", "https://www.facebook.com/Travelian.pk/"), ("Instagram", "https://www.instagram.com/travelian.pk/"),
          ("YouTube", "https://www.youtube.com/@Travelianpk"), ("TikTok", "https://www.tiktok.com/@travelian.pk"))
PKT = timezone(timedelta(hours=5))


def _site() -> str:
    """The public website's origin (PUBLIC_SITE_URL), read when a message is built so tests and config changes apply."""
    return settings.public_site_url.rstrip("/")


def _logo() -> str:
    return f"{_site()}/travelian-logo-wordmark.png"

TONES = {  # status strip: background, text
    "info": (NAVY, "#FFFFFF"), "waiting": (AMBER_BG, AMBER_INK), "done": (TEAL, "#FFFFFF"), "urgent": (RED, "#FFFFFF"),
}


# ---------- data ----------

@dataclass
class Account:
    """Where the customer pays (Travelian's own account, shown in full)."""
    bank_name: str | None = None
    account_title: str | None = None
    account_number: str | None = None
    iban: str | None = None
    qr_url: str | None = None


@dataclass
class Booking:
    """What a customer email may show. No supplier, cost or internal fields exist here on purpose."""
    reference: str
    passengers: list[str]
    amount_minor: int
    currency: str = "PKR"
    pnr: str | None = None
    route: str | None = None            # "KHI → ISB"
    baggage: str | None = None          # "7 kg hand carry + 20 kg checked" (app.services.baggage, full words)
    travel_date: str | None = None      # "10 Dec 2026, 21:30"
    airline: str | None = None          # the airline the customer flies (not the supplier)
    flight: str | None = None           # "Z8259"
    hold_expires_at: datetime | None = None
    paid_minor: int | None = None
    card_fee_minor: int = 0  # card fee paid on top of the fare (travelian#74)
    tickets: list[tuple[str, str]] = field(default_factory=list)   # (passenger, ticket number)
    customer_name: str | None = None    # "Mr Mohammad Hafeez" for the greeting
    view_url: str | None = None         # signed link that opens this booking (read-only); Find my booking when absent


@dataclass
class StaffAlert:
    """A staff notification: everything the admin's Notifications page shows for it."""
    title: str
    reference: str
    passenger: str | None = None
    route: str | None = None
    travel_date: str | None = None
    fare: str | None = None
    pnr: str | None = None
    hold_expires_at: datetime | None = None
    note: str | None = None
    amount: str | None = None
    payfast_transaction_id: str | None = None
    supplier: str | None = None
    hold_status: str | None = None
    reason: str | None = None
    action: str | None = None


@dataclass
class Email:
    subject: str
    html: str
    text: str
    preheader: str


# ---------- formatting ----------

def money(amount_minor: int | None, currency: str = "PKR") -> str:
    if amount_minor is None:
        return "—"
    label = "Rs" if currency == "PKR" else currency
    whole, paisa = divmod(amount_minor, 100)
    return f"{label} {whole:,}" + (f".{paisa:02d}" if paisa else "")


def pkt(value: datetime) -> str:
    """'20 Nov 2026, 7:23 PM PKT'."""
    local = value.astimezone(PKT)
    return f"{local:%d %b %Y}, {local.strftime('%I:%M %p').lstrip('0')} PKT"


def utc(value: datetime) -> str:
    return f"{value.astimezone(timezone.utc):%d %b %Y, %H:%M} UTC"


def _e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


# ---------- building blocks (HTML) ----------

def _p(text: str, *, size: int = 15, color: str = INK, margin: str = "0 0 14px", weight: int = 400) -> str:
    return f'<p style="margin:{margin};font-family:{FONT};font-size:{size}px;line-height:1.6;color:{color};font-weight:{weight};">{text}</p>'


def _heading(text: str) -> str:
    return (f'<h1 style="margin:0 0 12px;font-family:{DISPLAY};font-size:22px;line-height:1.3;color:{NAVY};font-weight:700;">'
            f'{_e(text)}</h1>')


def _section_title(text: str, color: str = NAVY) -> str:
    return (f'<p style="margin:0 0 8px;font-family:{DISPLAY};font-size:15px;line-height:1.4;color:{color};font-weight:700;">'
            f'{_e(text)}</p>')


def _rows(rows: list[tuple[str, str]], *, total: tuple[str, str] | None = None, mono: set[str] = frozenset()) -> str:
    """Label / value rows (values already escaped HTML)."""
    out = []
    for label, value in rows:
        family = MONO if label in mono else FONT
        out.append(
            f'<tr><td style="padding:10px 0;border-bottom:1px solid {LINE};font-family:{FONT};font-size:14px;color:{MUTED};vertical-align:top;width:42%;">{_e(label)}</td>'
            f'<td style="padding:10px 0;border-bottom:1px solid {LINE};font-family:{family};font-size:14px;color:{INK};font-weight:600;text-align:right;vertical-align:top;word-break:break-word;">{value}</td></tr>')
    if total:
        out.append(
            f'<tr><td style="padding:14px 0 4px;font-family:{FONT};font-size:15px;color:{INK};font-weight:700;">{_e(total[0])}</td>'
            f'<td style="padding:14px 0 4px;font-family:{DISPLAY};font-size:20px;color:{NAVY};font-weight:700;text-align:right;white-space:nowrap;">{total[1]}</td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">{"".join(out)}</table>'


def _badge(text: str, tone: str) -> str:
    bg, ink = {"waiting": (AMBER_BG, AMBER_INK), "done": (GREEN_SOFT, GREEN_INK), "info": ("#E7F0FE", BLUE), "urgent": (RED_SOFT, RED_INK)}[tone]
    return (f'<span style="display:inline-block;padding:3px 10px;border-radius:999px;background:{bg};color:{ink};font-family:{FONT};'
            f'font-size:12px;font-weight:700;">{_e(text)}</span>')


def _box(inner: str, *, bg: str = "#F8FAFC", line: str = LINE) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate;margin:0 0 20px;">'
            f'<tr><td style="background:{bg};border:1px solid {line};border-radius:10px;padding:18px 20px;">{inner}</td></tr></table>')


def _deadline(expires: datetime, note: str) -> str:
    return _box(
        f'<p style="margin:0 0 4px;font-family:{FONT};font-size:12px;letter-spacing:0.04em;color:{AMBER_INK};font-weight:700;">PAY BEFORE THE HOLD ENDS</p>'
        f'<p style="margin:0;font-family:{DISPLAY};font-size:20px;color:{AMBER_INK};font-weight:700;">{_e(pkt(expires))}</p>'
        f'<p style="margin:2px 0 10px;font-family:{FONT};font-size:12px;color:{AMBER_INK};">{_e(utc(expires))}</p>'
        f'<p style="margin:0;font-family:{FONT};font-size:14px;line-height:1.55;color:{AMBER_INK};">{note}</p>',
        bg=AMBER_BG, line=AMBER_LINE)


def _button(label: str, href: str, color: str = NAVY) -> str:
    """Bulletproof button (table cell + link; a VML fallback isn't needed for a flat rounded rectangle)."""
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 22px;"><tr>'
            f'<td style="background:{color};border-radius:8px;mso-padding-alt:14px 26px;">'
            f'<a href="{_e(href)}" style="display:inline-block;padding:14px 26px;font-family:{FONT};font-size:15px;font-weight:700;color:#FFFFFF;'
            f'text-decoration:none;border-radius:8px;">{_e(label)}</a></td></tr></table>')


def _steps(steps: list[tuple[str, str]]) -> str:
    out = []
    for number, (title, body) in enumerate(steps, 1):
        out.append(
            f'<tr><td style="width:30px;vertical-align:top;padding:0 0 12px;">'
            f'<span style="display:inline-block;width:22px;height:22px;line-height:22px;border-radius:999px;background:{TEAL_SOFT};color:{TEAL};'
            f'font-family:{FONT};font-size:12px;font-weight:700;text-align:center;">{number}</span></td>'
            f'<td style="vertical-align:top;padding:1px 0 12px;font-family:{FONT};font-size:14px;line-height:1.55;color:{INK};">'
            f'<strong>{_e(title)}</strong> {body}</td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 8px;">{"".join(out)}</table>'


def _summary(booking: Booking, status: str, tone: str, *, total_label: str = "Total") -> str:
    rows = [("Booking reference", f"<strong>{_e(booking.reference)}</strong>")]
    if booking.pnr:
        rows.append(("Airline PNR", _e(booking.pnr)))
    if booking.airline or booking.flight:
        rows.append(("Flight", _e(" ".join(part for part in (booking.airline, booking.flight) if part))))
    if booking.route:
        rows.append(("Route", _e(booking.route)))
    if booking.travel_date:
        rows.append(("Travel date", _e(booking.travel_date)))
    if booking.baggage:
        rows.append(("Baggage", _e(booking.baggage)))
    rows.append(("Passengers" if len(booking.passengers) > 1 else "Passenger", "<br>".join(_e(p) for p in booking.passengers) or "—"))
    rows.append(("Status", _badge(status, tone)))
    if booking.paid_minor is not None:
        rows.append(("Paid", _e(money(booking.paid_minor, booking.currency))))
    if booking.card_fee_minor:
        rows.append(("Card fee paid", _e(money(booking.card_fee_minor, booking.currency))))
    return _section_title("Booking summary") + _rows(rows, total=(total_label, _e(money(booking.amount_minor, booking.currency))),
                                                     mono={"Booking reference", "Airline PNR"})


def _accounts(accounts: list[Account], reference: str, amount: str, kind: str) -> str:
    out = []
    for account in accounts:
        rows = [(label, _e(value)) for label, value in (
            ("Bank" if kind == "bank" else "Wallet", account.bank_name if kind == "bank" else "JazzCash"),
            ("Account title", account.account_title), ("Account number" if kind == "bank" else "JazzCash number", account.account_number),
            ("IBAN", _iban(account.iban)), ("Amount", amount), ("Payment reference", reference)) if value]
        table = _rows(rows, mono={"Account number", "JazzCash number", "IBAN", "Payment reference"})
        qr = (f'<td width="132" style="width:132px;vertical-align:top;padding:0 18px 0 0;" class="qr">'
              f'<img src="{_e(account.qr_url)}" width="120" height="120" alt="QR code to pay {_e(account.account_title or "Travelian")}" '
              f'style="display:block;width:120px;height:120px;border:1px solid {LINE};border-radius:8px;background:#FFFFFF;"></td>') if account.qr_url else ""
        out.append(_box(
            _section_title("Bank transfer details" if kind == "bank" else "JazzCash details")
            + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{qr}<td class="acct" style="vertical-align:top;">{table}</td></tr></table>'
            + _p(f"Use <strong>{_e(reference)}</strong> as the payment reference so we can match your payment quickly."
                 + (" You can also scan the QR code." if account.qr_url else ""), size=13, color=MUTED, margin="12px 0 0")))
    return "".join(out)


def _iban(iban: str | None) -> str | None:
    """'PK11 UNIL 0109 0003 5976 2048': the standard grouping, so it wraps between groups, never inside one."""
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4)) if iban else None


def _tickets(tickets: list[tuple[str, str]]) -> str:
    return _section_title("Your e-tickets") + _rows([(name, _e(number)) for name, number in tickets], mono={name for name, _ in tickets})


def _layout(*, reference: str, status: str, tone: str, preheader: str, body: str, audience: str = "customer") -> str:
    strip_bg, strip_ink = TONES[tone]
    footer_note = ("You're receiving this because you booked with Travelian. Please quote your booking reference in every message."
                   if audience == "customer" else "Internal notification for Travelian staff. Don't forward it to customers.")
    socials = " &nbsp;·&nbsp; ".join(f'<a href="{_e(url)}" style="color:{BLUE};text-decoration:none;">{_e(name)}</a>' for name, url in SOCIAL)
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light only">
<title>{_e(status)}: {_e(reference)}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&amp;family=Plus+Jakarta+Sans:wght@700&amp;display=swap" rel="stylesheet">
<style>
  @media only screen and (max-width: 620px) {{
    .card {{ width: 100% !important; border-radius: 0 !important; }}
    .pad {{ padding-left: 20px !important; padding-right: 20px !important; }}
    .qr, .acct {{ display: block !important; width: 100% !important; }}
    .qr {{ padding: 0 0 14px !important; }}
    .hide-sm {{ display: none !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{PAGE};-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{_e(preheader)}&#8199;&#65279;&#847;&#8199;&#65279;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAGE};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" class="card" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:#FFFFFF;border:1px solid {LINE};border-radius:12px;overflow:hidden;">
  <tr><td class="pad" style="padding:22px 32px;border-bottom:1px solid {LINE};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td style="vertical-align:middle;"><a href="{_site()}" style="text-decoration:none;"><img src="{_logo()}" width="148" height="42" alt="Travelian" style="display:block;width:148px;height:auto;border:0;"></a></td>
      <td style="vertical-align:middle;text-align:right;font-family:{FONT};">
        <span style="display:block;font-size:11px;color:{MUTED};">Booking ref</span>
        <span style="display:block;font-family:{MONO};font-size:16px;font-weight:700;color:{NAVY};">{_e(reference)}</span>
      </td>
    </tr></table>
  </td></tr>
  <tr><td class="pad" style="padding:12px 32px;background:{strip_bg};font-family:{FONT};font-size:14px;font-weight:700;color:{strip_ink};">{_e(status)}</td></tr>
  <tr><td class="pad" style="padding:28px 32px 10px;">{body}</td></tr>
  <tr><td class="pad" style="padding:20px 32px 24px;background:#F8FAFC;border-top:1px solid {LINE};font-family:{FONT};font-size:13px;line-height:1.6;color:{MUTED};">
    <p style="margin:0 0 6px;font-family:{DISPLAY};font-size:14px;font-weight:700;color:{NAVY};">Travelian</p>
    <p style="margin:0;">Call <a href="tel:+923244440014" style="color:{BLUE};text-decoration:none;">{PHONE}</a> &nbsp;·&nbsp;
      WhatsApp <a href="{_e(_whatsapp(reference))}" style="color:{BLUE};text-decoration:none;">{PHONE}</a></p>
    <p style="margin:0;"><a href="mailto:{EMAIL}" style="color:{BLUE};text-decoration:none;">{EMAIL}</a> &nbsp;·&nbsp; <a href="{_site()}" style="color:{BLUE};text-decoration:none;">www.travelian.pk</a></p>
    <p style="margin:0 0 10px;">{ADDRESS}</p>
    <p style="margin:0 0 10px;">{socials}</p>
    <p style="margin:0;font-size:12px;">{_e(footer_note)}</p>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


# ---------- plain text ----------

def _text(status: str, lines: list[str], *, audience: str = "customer", reference: str | None = None) -> str:
    footer = [
        "", "--", "Travelian", f"Call {PHONE}" + (f" · WhatsApp {_whatsapp(reference)}" if reference else f" or WhatsApp {PHONE}"), f"{EMAIL} · www.travelian.pk", ADDRESS,
        "Please quote your booking reference in every message." if audience == "customer" else "Internal notification for Travelian staff.",
    ]
    return "\n".join([status.upper(), "", *lines, *footer]) + "\n"


def _text_summary(booking: Booking, status: str) -> list[str]:
    lines = ["BOOKING SUMMARY", f"Booking reference: {booking.reference}"]
    if booking.pnr:
        lines.append(f"Airline PNR: {booking.pnr}")
    if booking.airline or booking.flight:
        lines.append("Flight: " + " ".join(part for part in (booking.airline, booking.flight) if part))
    if booking.route:
        lines.append(f"Route: {booking.route}")
    if booking.travel_date:
        lines.append(f"Travel date: {booking.travel_date}")
    if booking.baggage:
        lines.append(f"Baggage: {booking.baggage}")
    lines.append(("Passengers: " if len(booking.passengers) > 1 else "Passenger: ") + (", ".join(booking.passengers) or "—"))
    lines.append(f"Status: {status}")
    if booking.paid_minor is not None:
        lines.append(f"Paid: {money(booking.paid_minor, booking.currency)}")
    if booking.card_fee_minor:
        lines.append(f"Card fee paid: {money(booking.card_fee_minor, booking.currency)}")
    lines.append(f"Total: {money(booking.amount_minor, booking.currency)}")
    return lines


def _text_accounts(accounts: list[Account], reference: str, amount: str, kind: str) -> list[str]:
    out = []
    for account in accounts:
        out += ["", "BANK TRANSFER DETAILS" if kind == "bank" else "JAZZCASH DETAILS"]
        for label, value in (("Bank", account.bank_name if kind == "bank" else "JazzCash"), ("Account title", account.account_title),
                             ("Account number" if kind == "bank" else "JazzCash number", account.account_number), ("IBAN", account.iban),
                             ("Amount", amount), ("Payment reference", reference)):
            if value:
                out.append(f"{label}: {value}")
        if account.qr_url:
            out.append(f"QR code: {account.qr_url}")
    return out


def _greeting(booking: Booking) -> str:
    return f"Dear {booking.customer_name}," if booking.customer_name else "Hello,"


def _manage(booking: Booking) -> str:
    """The booking's signed link (opens it directly); Find my booking when there is none."""
    return booking.view_url or f"{_site()}/find-booking"


def _fallback(booking: Booking) -> str:
    """Under every button: the way in that always works."""
    return _p(f"Link not working? Go to <a href=\"{_site()}/find-booking\" style=\"color:{BLUE};\">travelian.pk/find-booking</a> and enter "
              f"<strong>{_e(booking.reference)}</strong> with your email or phone number.", size=12, color=MUTED, margin="-10px 0 18px")


def _fallback_text(booking: Booking) -> str:
    return f"Link not working? Go to {_site()}/find-booking and enter {booking.reference} with your email or phone number."


def _whatsapp(reference: str) -> str:
    from urllib.parse import quote

    return f"{WHATSAPP}?text={quote(f'Hello Travelian, I have a question about booking {reference}.')}"


# ---------- customer templates ----------

def booking_held(booking: Booking) -> Email:
    """Seats held, card payment (PayFast) still to make."""
    status = "Seats held: payment pending"
    amount = money(booking.amount_minor, booking.currency)
    expires = booking.hold_expires_at
    preheader = f"Your seats on booking {booking.reference} are held" + (f" until {pkt(expires)}" if expires else "") + f". Pay {amount} to get your ticket."
    body = (_heading("Your seats are held")
            + _p(f"{_e(_greeting(booking))}")
            + _p(f"Thank you for booking with Travelian. Your seats are <strong>on hold</strong>. Complete the payment of "
                 f"<strong>{_e(amount)}</strong> before the hold ends and we'll issue your e-ticket.")
            + (_deadline(expires, "If the payment isn't completed in time, the airline releases the seats and the fare may change.") if expires else "")
            + _summary(booking, "On hold, payment pending", "waiting", total_label="Total to pay")
            + _p("", margin="0 0 10px")
            + _button("View booking and pay", _manage(booking))
            + _fallback(booking)
            + _p("Questions? Reply to this email or message us on WhatsApp.", size=13, color=MUTED))
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", f"Your seats are on hold. Pay {amount} before the hold ends to get your e-ticket."]
                 + ([f"Pay before: {pkt(expires)} ({utc(expires)})"] if expires else []) + [""] + _text_summary(booking, "On hold, payment pending")
                 + ["", f"View booking and pay: {_manage(booking)}", _fallback_text(booking)])
    return Email(f"Seats held: pay {amount} to confirm booking {booking.reference}", _layout(reference=booking.reference, status=status, tone="waiting",
                 preheader=preheader, body=body), text, preheader)


def book_only_held(booking: Booking) -> Email:
    """Held with Book Only: no payment taken now; Travelian's team arranges payment."""
    status = "Seats held: no payment yet"
    expires = booking.hold_expires_at
    preheader = f"Booking {booking.reference} is held. Our team will contact you to arrange payment."
    body = (_heading("Your seats are held")
            + _p(_e(_greeting(booking)))
            + _p("Your seats are <strong>on hold</strong>. No payment has been taken. Our team will contact you shortly to arrange "
                 "payment; your ticket is issued once the payment is received.")
            + (_deadline(expires, "Seats on hold are released by the airline at this time if payment hasn't been arranged.") if expires else "")
            + _summary(booking, "On hold, no payment yet", "waiting")
            + _steps([("We call you:", "a Travelian agent confirms the fare and the payment method with you."),
                      ("You pay:", "by bank transfer, JazzCash or card, as agreed."),
                      ("We issue your ticket:", "your e-ticket arrives by email as soon as the payment is confirmed.")])
            + _button("View my booking", _manage(booking)) + _fallback(booking))
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", "Your seats are on hold. No payment has been taken. Our team will contact you to arrange payment."]
                 + ([f"Hold ends: {pkt(expires)} ({utc(expires)})"] if expires else []) + [""] + _text_summary(booking, "On hold, no payment yet")
                 + ["", f"View my booking: {_manage(booking)}", _fallback_text(booking)])
    return Email(f"Seats held for booking {booking.reference}: we'll contact you about payment", _layout(
        reference=booking.reference, status=status, tone="waiting", preheader=preheader, body=body), text, preheader)


def transfer_instructions(booking: Booking, accounts: list[Account], kind: str = "bank") -> Email:
    """Held for bank transfer (kind 'bank') or JazzCash ('jazzcash'): the account(s), QR and how the ticket is issued."""
    method = "bank transfer" if kind == "bank" else "JazzCash"
    status = f"Seats held: pay by {method}"
    amount = money(booking.amount_minor, booking.currency)
    expires = booking.hold_expires_at
    preheader = (f"Pay {amount} by {method} using reference {booking.reference}" + (f" before {pkt(expires)}" if expires else "") + ".")
    body = (_heading(f"Pay by {method} to confirm your booking")
            + _p(_e(_greeting(booking)))
            + _p(f"Your seats are <strong>on hold</strong> with {method} as your payment method. Send <strong>{_e(amount)}</strong> to the "
                 f"account below before the hold ends, then send us the payment receipt.")
            + (_deadline(expires, "Unpaid holds are released by the airline at this time. Send your receipt early so we can verify it in time.") if expires else "")
            + _accounts(accounts, booking.reference, amount, "bank" if kind == "bank" else "jazzcash")
            + _summary(booking, f"On hold, awaiting {method}", "waiting", total_label="Amount to pay")
            + _p("", margin="0 0 10px")
            + _section_title("How your ticket is issued")
            + _steps([("Send the payment:", f"{_e(amount)} by {method}, with <strong>{_e(booking.reference)}</strong> as the reference."),
                      ("Send the receipt:", f"reply to this email or WhatsApp it to <strong>{PHONE}</strong>."),
                      ("Get your e-ticket:", "once our team confirms the payment, we issue your ticket and email it to you.")])
            + _button("Send receipt on WhatsApp", f"{WHATSAPP}?text={_e('Payment receipt for booking ' + booking.reference)}", TEAL)
            + _p(f'<a href="{_e(_manage(booking))}" style="color:{BLUE};">View my booking</a>', size=14, margin="-10px 0 18px"))
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", f"Your seats are on hold. Send {amount} by {method} before the hold ends, then send us the receipt."]
                 + ([f"Pay before: {pkt(expires)} ({utc(expires)})"] if expires else [])
                 + _text_accounts(accounts, booking.reference, amount, "bank" if kind == "bank" else "jazzcash") + [""]
                 + _text_summary(booking, f"On hold, awaiting {method}")
                 + ["", "HOW YOUR TICKET IS ISSUED", f"1. Send {amount} by {method} with {booking.reference} as the reference.",
                    f"2. Send the receipt: reply to this email or WhatsApp {PHONE}.", "3. Once we confirm the payment, we issue and email your e-ticket.",
                    "", f"View my booking: {_manage(booking)}", _fallback_text(booking)])
    return Email(f"Pay {amount} by {method} for booking {booking.reference}", _layout(
        reference=booking.reference, status=status, tone="waiting", preheader=preheader, body=body), text, preheader)


def payment_received(booking: Booking, method: str = "card") -> Email:
    status = "Payment received"
    paid = money(booking.paid_minor if booking.paid_minor is not None else booking.amount_minor, booking.currency)
    preheader = f"We've received {paid} for booking {booking.reference}. Your ticket is being issued."
    body = (_heading("Thank you, we've received your payment")
            + _p(_e(_greeting(booking)))
            + _p(f"We've received <strong>{_e(paid)}</strong> by {_e(method)} for booking <strong>{_e(booking.reference)}</strong>. "
                 "Your ticket is being issued now; you'll get it in a separate email.")
            + _summary(booking, "Paid, ticket being issued", "done", total_label="Total")
            + _p("", margin="0 0 10px")
            + _button("View my booking", _manage(booking)) + _fallback(booking))
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", f"We've received {paid} by {method} for booking {booking.reference}.",
                          "Your ticket is being issued now; you'll get it in a separate email.", ""] + _text_summary(booking, "Paid, ticket being issued")
                 + ["", f"View my booking: {_manage(booking)}", _fallback_text(booking)])
    return Email(f"Payment received for booking {booking.reference}", _layout(
        reference=booking.reference, status=status, tone="done", preheader=preheader, body=body), text, preheader)


def ticket_issued(booking: Booking) -> Email:
    status = "Ticket issued"
    preheader = f"Your e-ticket for booking {booking.reference} is ready" + (f": {booking.route}" if booking.route else "") + "."
    body = (_heading("Your e-ticket is ready")
            + _p(_e(_greeting(booking)))
            + _p("Your booking is confirmed and your ticket is issued. Show your ticket number and ID (passport or CNIC) at the airport.")
            + _tickets(booking.tickets)
            + _p("", margin="0 0 16px")
            + _summary(booking, "Confirmed, ticket issued", "done", total_label="Total paid")
            + _p("", margin="0 0 10px")
            + _box(_section_title("Before you fly")
                   + _p("Arrive at the airport at least 2 hours before a domestic flight and 3 hours before an international one. "
                        "Names must match your ID exactly.", size=14, margin="0"))
            + _button("View booking and itinerary", _manage(booking)) + _fallback(booking))
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", "Your booking is confirmed and your ticket is issued.", "", "YOUR E-TICKETS"]
                 + [f"{name}: {number}" for name, number in booking.tickets] + [""] + _text_summary(booking, "Confirmed, ticket issued")
                 + ["", "Arrive 2 hours before domestic and 3 hours before international flights. Names must match your ID.",
                    f"View booking and itinerary: {_manage(booking)}", _fallback_text(booking)])
    return Email(f"Your e-ticket: booking {booking.reference}" + (f", {booking.route}" if booking.route else ""), _layout(
        reference=booking.reference, status=status, tone="done", preheader=preheader, body=body), text, preheader)


def issuance_pending(booking: Booking) -> Email:
    """Paid, the ticket isn't issued yet (taking longer than usual): reassure and set expectations."""
    status = "Paid: ticket being issued"
    preheader = f"Your payment for {booking.reference} is safe. We're issuing your ticket and will email it shortly."
    body = (_heading("We're issuing your ticket")
            + _p(_e(_greeting(booking)))
            + _p("Your payment is received and safe. Issuing your ticket is taking a little longer than usual; our team is on it and "
                 "will email your e-ticket as soon as it's ready. You don't need to do anything.")
            + _summary(booking, "Paid, ticket being issued", "info", total_label="Total paid")
            + _p("", margin="0 0 10px")
            + _p(f"If you travel within 24 hours, call or WhatsApp us on <strong>{PHONE}</strong>.", size=14)
            + _button("View my booking", _manage(booking)) + _fallback(booking))
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", "Your payment is received and safe. Issuing your ticket is taking a little longer than usual;",
                          "our team will email your e-ticket as soon as it's ready. You don't need to do anything.", ""]
                 + _text_summary(booking, "Paid, ticket being issued") + ["", f"Travelling within 24 hours? Call or WhatsApp {PHONE}.",
                                                                        f"View my booking: {_manage(booking)}", _fallback_text(booking)])
    return Email(f"We're issuing your ticket: booking {booking.reference}", _layout(
        reference=booking.reference, status=status, tone="info", preheader=preheader, body=body), text, preheader)


def paid_not_ticketed(booking: Booking) -> Email:
    """Paid, but the seats couldn't be ticketed (hold lost): the customer chooses a refund or a new booking."""
    status = "Action needed: we couldn't issue your ticket"
    paid = money(booking.paid_minor if booking.paid_minor is not None else booking.amount_minor, booking.currency)
    preheader = f"Your payment of {paid} for {booking.reference} is safe. Choose a full refund or a new booking."
    body = (_heading("We couldn't issue your ticket")
            + _p(_e(_greeting(booking)))
            + _p(f"We received your payment of <strong>{_e(paid)}</strong>, but the airline released the seats before we could issue the "
                 "ticket. We're sorry for the trouble. <strong>Your money is safe</strong>, and you can choose what happens next.")
            + _box(_section_title("Your options")
                   + _steps([("Full refund:", f"we return {_e(paid)} to the way you paid."),
                             ("New booking:", "we find you seats on the same or another flight; any fare difference is agreed with you first.")]),
                   bg=TEAL_SOFT, line="#BFE6E9")
            + _summary(booking, "Paid, not ticketed", "urgent", total_label="Total paid")
            + _p("", margin="0 0 10px")
            + _p("Reply to this email or message us on WhatsApp with your choice. A Travelian agent will also call you.", size=14)
            + _button("Message us on WhatsApp", f"{WHATSAPP}?text={_e('Booking ' + booking.reference + ': refund or rebook')}", TEAL)
            + _p(f'<a href="{_e(_manage(booking))}" style="color:{BLUE};">View my booking</a>', size=14, margin="-10px 0 18px"))
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", f"We received your payment of {paid}, but the airline released the seats before we could issue the ticket.",
                          "Your money is safe. Choose what happens next:", f"1. Full refund: we return {paid} to the way you paid.",
                          "2. New booking: we find you seats; any fare difference is agreed with you first.", ""]
                 + _text_summary(booking, "Paid, not ticketed") + ["", f"Reply to this email or WhatsApp {PHONE} with your choice.",
                                                                 f"View my booking: {_manage(booking)}", _fallback_text(booking)])
    return Email(f"Booking {booking.reference}: choose a refund or a new booking", _layout(
        reference=booking.reference, status=status, tone="urgent", preheader=preheader, body=body), text, preheader)


def hold_expired(booking: Booking) -> Email:
    """An unpaid hold ended (gateway#52): the seats are released; nothing was charged; search again or ask us."""
    status = "Hold ended: seats released"
    preheader = f"The hold on booking {booking.reference} has ended and the seats are released. Nothing was charged."
    body = (_heading("Your hold has ended")
            + _p(_e(_greeting(booking)))
            + _p("The time to pay for this booking has passed, so the airline has released the seats. <strong>Nothing was charged.</strong> "
                 "Fares change often: search again for the latest price, or message us and we'll help you book.")
            + _summary(booking, "Hold ended, seats released", "info", total_label="Fare when held")
            + _p("", margin="0 0 10px")
            + _button("Search flights again", f"{_site()}/flights-search")
            + _p(f'<a href="{_e(_whatsapp(booking.reference))}" style="color:{BLUE};">Ask us on WhatsApp</a>', size=14, margin="-10px 0 18px"))
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", "The time to pay for this booking has passed, so the airline has released the seats.",
                          "Nothing was charged. Search again for the latest price, or message us and we'll help you book.", ""]
                 + _text_summary(booking, "Hold ended, seats released") + ["", f"Search flights again: {_site()}/flights-search",
                                                                         f"Ask us on WhatsApp: {_whatsapp(booking.reference)}"])
    return Email(f"Booking {booking.reference}: your hold has ended", _layout(
        reference=booking.reference, status=status, tone="info", preheader=preheader, body=body), text, preheader)


def booking_cancelled(booking: Booking) -> Email:
    """Travelian cancelled the booking (e.g. at the customer's request, gateway#48). A refund, if any, is a separate message."""
    status = "Booking cancelled"
    preheader = f"Booking {booking.reference} is cancelled."
    paid = booking.paid_minor or 0
    refund_note = (f"You paid <strong>{_e(money(paid, booking.currency))}</strong>. Our team will contact you about the refund."
                   if paid else "Nothing was charged.")
    body = (_heading("Your booking is cancelled")
            + _p(_e(_greeting(booking)))
            + _p(f"Booking <strong>{_e(booking.reference)}</strong> is cancelled. {refund_note}")
            + _summary(booking, "Cancelled", "info")
            + _p("", margin="0 0 10px")
            + _p("Didn't ask for this, or have a question? Reply to this email or message us on WhatsApp.", size=14)
            + _button("Message us on WhatsApp", _whatsapp(booking.reference), TEAL))
    refund_text = f"You paid {money(paid, booking.currency)}. Our team will contact you about the refund." if paid else "Nothing was charged."
    text = _text(status, reference=booking.reference, lines=[_greeting(booking), "", f"Booking {booking.reference} is cancelled. {refund_text}", ""]
                 + _text_summary(booking, "Cancelled") + ["", "Didn't ask for this, or have a question? Reply to this email or WhatsApp us."])
    return Email(f"Booking {booking.reference} is cancelled", _layout(
        reference=booking.reference, status=status, tone="info", preheader=preheader, body=body), text, preheader)


# ---------- staff templates ----------

def _admin_link(reference: str) -> str:
    return f"{_site()}/admin/bookings/{reference}/payments"


def _staff_rows(alert: StaffAlert) -> list[tuple[str, str]]:
    pairs = [("Booking reference", alert.reference), ("Amount received", alert.amount), ("PayFast transaction ID", alert.payfast_transaction_id),
             ("Supplier", alert.supplier), ("PNR", alert.pnr), ("Hold status", alert.hold_status),
             ("Hold expires", f"{pkt(alert.hold_expires_at)} · {utc(alert.hold_expires_at)}" if alert.hold_expires_at else None),
             ("Passengers", alert.passenger), ("Route", alert.route), ("Travel date", alert.travel_date), ("Fare", alert.fare),
             ("Reason", alert.reason)]
    return [(label, value) for label, value in pairs if value]


def staff_alert(alert: StaffAlert) -> Email:
    """A normal staff notification (hold created, request, bank transfer hold, Book Only hold, issued...)."""
    preheader = f"{alert.title}: {alert.reference}" + (f", {alert.passenger}" if alert.passenger else "")
    body = (_heading(alert.title)
            + _rows([(label, _e(value)) for label, value in _staff_rows(alert)], mono={"Booking reference", "PNR", "PayFast transaction ID"})
            + (_box(_p(_e(alert.note), size=14, margin="0")) if alert.note else _p("", margin="0 0 12px"))
            + _button("Open in admin", _admin_link(alert.reference)))
    text = _text(alert.title, reference=alert.reference, lines=[f"{label}: {value}" for label, value in _staff_rows(alert)] + ([""] + [alert.note] if alert.note else [])
                 + ["", f"Open in admin: {_admin_link(alert.reference)}"], audience="staff")
    return Email(f"Travelian: {alert.title}: {alert.reference}", _layout(
        reference=alert.reference, status=alert.title, tone="info", preheader=preheader, body=body, audience="staff"), text, preheader)


def staff_red_alert(alert: StaffAlert) -> Email:
    """URGENT: a customer paid and has no ticket. Every current detail, the reason and what to do."""
    subject = alert.title if alert.title.startswith("URGENT") else f"URGENT: {alert.title}"
    preheader = f"Customer paid {alert.amount or ''} and has no ticket. Act before the hold ends.".replace("  ", " ")
    banner = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px;"><tr>'
              f'<td style="background:{RED};border-radius:10px;padding:16px 20px;font-family:{FONT};color:#FFFFFF;">'
              f'<p style="margin:0 0 2px;font-size:12px;letter-spacing:0.08em;font-weight:700;">URGENT</p>'
              f'<p style="margin:0;font-family:{DISPLAY};font-size:19px;font-weight:700;line-height:1.35;">Payment received, ticket NOT issued</p>'
              + (f'<p style="margin:6px 0 0;font-size:14px;">Hold ends {_e(pkt(alert.hold_expires_at))} ({_e(utc(alert.hold_expires_at))})</p>' if alert.hold_expires_at else "")
              + '</td></tr></table>')
    body = (banner
            + _rows([(label, _e(value)) for label, value in _staff_rows(alert)], mono={"Booking reference", "PNR", "PayFast transaction ID"})
            + (_box(_section_title("What to do", RED_INK) + _p(_e(alert.action), size=14, margin="0"), bg=RED_SOFT, line="#F5B5B5") if alert.action
               else _p("", margin="0 0 12px"))
            + _button("Open booking in admin", _admin_link(alert.reference), RED))
    text = _text(subject, reference=alert.reference, lines=["URGENT: PAYMENT RECEIVED, TICKET NOT ISSUED", ""] + [f"{label}: {value}" for label, value in _staff_rows(alert)]
                 + ([""] + ["WHAT TO DO", alert.action] if alert.action else []) + ["", f"Open in admin: {_admin_link(alert.reference)}"], audience="staff")
    return Email(subject, _layout(reference=alert.reference, status="Urgent staff alert", tone="urgent",
                                  preheader=preheader, body=body, audience="staff"), text, preheader)
