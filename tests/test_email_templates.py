"""Email templates: one layout, escaped values, a plain-text version, no supplier data in customer emails."""

from dataclasses import fields
from datetime import datetime, timezone

from app.services import email_templates as t

EXPIRES = datetime(2026, 11, 20, 14, 23, tzinfo=timezone.utc)
BOOKING = t.Booking(reference="TRV-CR01QW", passengers=["MR Ali Raza"], amount_minor=1_439_000, pnr="E8998C", route="LHE → KHI",
                    hold_expires_at=EXPIRES, customer_name="Mr Ali Raza")
BANK = t.Account(bank_name="Meezan Bank", account_title="Travelian Private Limited", account_number="0101", iban="PK11UNIL0109000359762048",
                 qr_url="https://assets.example/payment-qr/a.png")


def customer_emails(booking=BOOKING):
    return [t.booking_held(booking), t.book_only_held(booking), t.transfer_instructions(booking, [BANK], "bank"),
            t.transfer_instructions(booking, [BANK], "jazzcash"), t.payment_received(booking), t.ticket_issued(booking),
            t.issuance_pending(booking), t.paid_not_ticketed(booking)]


def test_every_email_has_the_shared_layout_a_text_version_and_the_reference():
    staff = [t.staff_alert(t.StaffAlert(title="Held (Book Only) — no payment yet", reference="TRV-CR01QW")),
             t.staff_red_alert(t.StaffAlert(title="URGENT: Payment received, ticket NOT issued — TRV-CR01QW", reference="TRV-CR01QW",
                                            supplier="Southair", amount="Rs 14,390", hold_expires_at=EXPIRES, action="Issue by hand."))]
    for email in customer_emails() + staff:
        assert email.html.startswith("<!DOCTYPE html>") and "travelian-logo-wordmark.png" in email.html
        assert "role=\"presentation\"" in email.html and "<table" in email.html          # table-based for Outlook
        assert "TRV-CR01QW" in email.subject + email.html and "TRV-CR01QW" in email.text
        assert "+92 324 4440014" in email.html and "+92 324 4440014" in email.text           # footer contact + WhatsApp
        assert "<" not in email.text                                                         # plain text, no tags


def test_the_hold_deadline_is_in_pakistan_time_with_utc():
    html = t.booking_held(BOOKING).html
    assert "20 Nov 2026, 7:23 PM PKT" in html and "20 Nov 2026, 14:23 UTC" in html


def test_customer_emails_carry_no_supplier_fields():
    assert not {f.name for f in fields(t.Booking)} & {"supplier", "provider", "supplier_cost", "margin", "note"}
    for email in customer_emails():
        for word in ("Southair", "SooperFare", "supplier cost", "Supplier:"):
            assert word not in email.html and word not in email.text


def test_values_are_escaped():
    email = t.booking_held(t.Booking(reference="TRV-1", passengers=["<script>alert(1)</script>"], amount_minor=100, customer_name="A & B"))
    assert "<script>" not in email.html and "&lt;script&gt;" in email.html and "A &amp; B" in email.html


def test_bank_details_qr_and_grouped_iban():
    email = t.transfer_instructions(BOOKING, [BANK], "bank")
    assert 'src="https://assets.example/payment-qr/a.png"' in email.html and "PK11 UNIL 0109 0003 5976 2048" in email.html
    assert "IBAN: PK11UNIL0109000359762048" in email.text                                   # copyable in plain text
    assert "Rs 14,390" in email.subject


def test_red_alert_has_the_urgent_banner_and_every_detail():
    alert = t.StaffAlert(title="URGENT: Payment received, ticket NOT issued — TRV-CR01QW", reference="TRV-CR01QW", amount="Rs 14,390",
                         payfast_transaction_id="PF-1", supplier="Southair", pnr="E8998C", hold_status="active", hold_expires_at=EXPIRES,
                         passenger="MR Ali Raza", route="LHE → KHI", travel_date="2026-12-10 09:30", reason="unclear", action="Issue by hand.")
    email = t.staff_red_alert(alert)
    assert email.subject.startswith("URGENT") and "Payment received, ticket NOT issued" in email.html
    for value in ("Rs 14,390", "PF-1", "Southair", "E8998C", "active", "MR Ali Raza", "LHE → KHI", "unclear", "Issue by hand."):
        assert value in email.text
