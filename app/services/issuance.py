"""Ticket issuance on the supplier (spec 6 Flight Operations; D18 "safe issuance and Unknown handling").

Request (any back-office user): needs full payment recorded and a valid hold -> 'requested'.
Issue (issue_tickets, ticketing flag on):
  1. Claim: the booking moves to 'processing' in a locked transaction; a second click is refused.
  2. Before sending: pnr_info (booked, no tickets, nothing paid, ticketable, supplier total equals
     the stored supplier cost) and the supplier balance covers it. Any failure here sends nothing,
     and the booking returns to its previous status.
  3. Ticketing: CASH, the exact supplier total, ONE call, never retried.
  4. Always pnr_info afterwards and decide from it: every passenger ticketed -> issued (ticket
     numbers stored); booked, no tickets, nothing paid and the supplier didn't say OK -> not issued
     (back to the previous status); anything else, or pnr_info failing -> 'unknown', which blocks
     issuing until verified with the supplier or resolved with a reason.
Every step writes an audit event. Responses never carry supplier names, cost or balance except
the checklist's supplier fields for viewers with the cost and margin permission.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.field_visibility import ISSUE_TICKETS, BackOfficeUser
from app.db import issuance as store
from app.db.issuance import IssuanceRefused
from app.services.live_flights import to_minor
from app.vendors.flights.southair import normalize
from app.vendors.flights.southair.client import SouthairError, configured_client

logger = logging.getLogger(__name__)


@dataclass
class Outcome:
    result: str  # issued, not_issued, unknown, requested
    issuance_status: str
    message: str
    tickets: dict[int, str] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def match_tickets(target: store.Target, state: normalize.PnrState) -> dict[int, str] | None:
    """Our travelers (position, type) against the supplier's (type, ticket) list, by type and order."""
    ours: dict[str, list[int]] = {}
    for position, kind in target.traveler_types:
        ours.setdefault(kind, []).append(position)
    theirs: dict[str, list[str | None]] = {}
    for kind, number in state.tickets:
        theirs.setdefault(kind, []).append(number)
    if not ours or {k: len(v) for k, v in ours.items()} != {k: len(v) for k, v in theirs.items()}:
        return None
    tickets = {position: number for kind, positions in ours.items() for position, number in zip(positions, theirs[kind])}
    return tickets if all(tickets.values()) else None  # type: ignore[return-value]


#: Providers whose tickets the gateway can issue (and check). SooperFare only with SOOPERFARE_TICKETING_ENABLED and
#: SOOPERFARE_CONFIRM_ENABLED (its /v1/booking/confirm takes payment from Travelian's SooperFare account).
ISSUING_PROVIDERS = ("southair", "sooperfare")
NOT_ISSUABLE_HERE = "Tickets for this supplier can't be issued from Travelian. Issue in the supplier's portal, then record the ticket numbers. Nothing was issued."


def ticketing_reason(provider: str | None, product: str = "flight") -> str | None:
    """Why tickets can't be issued from Travelian for this booking, naming the supplier and the switch (None: they can)."""
    if product == "group":
        return "Group tickets come from the group supplier: record the final PNR and ticket numbers on the Tickets tab."
    name = provider or "southair"
    if name == "southair":
        return None if settings.southair_ticketing_enabled else (
            "Southair ticketing is switched off on the server (SOUTHAIR_TICKETING_ENABLED). Payments and issuance requests still work.")
    if name == "sooperfare":
        if not settings.sooperfare_ticketing_enabled:
            return "SooperFare ticketing is switched off on the server (SOOPERFARE_TICKETING_ENABLED). Issue in SooperFare's portal, then record the ticket numbers."
        if not settings.sooperfare_confirm_enabled:
            return ("SooperFare ticketing from Travelian isn't switched on yet (SOOPERFARE_CONFIRM_ENABLED: waiting for the owner to approve "
                    "the first confirm). Issue in SooperFare's portal, then record the ticket numbers.")
        return None
    return f"Tickets for this supplier ({name}) can't be issued from Travelian: issue in the supplier's portal, then record the ticket numbers."


def ticketing_on(provider: str | None, product: str = "flight") -> bool:
    """Issuing is possible for this booking's provider (and switched on)."""
    return ticketing_reason(provider, product) is None


def _issuable_provider(target) -> None:
    if (target.provider or "southair") not in ISSUING_PROVIDERS:
        raise IssuanceRefused(409, NOT_ISSUABLE_HERE, "provider_not_supported")


def _require(viewer: BackOfficeUser) -> None:
    if not viewer.can(ISSUE_TICKETS):
        raise IssuanceRefused(403, "Issuing tickets requires the issue tickets permission.")


async def request(reference: str, viewer: BackOfficeUser, note: str | None = None) -> Outcome:
    await run_in_threadpool(store.request, reference, viewer.user_id, _now(), settings.ticketing_deadline_margin_seconds, note)
    return Outcome("requested", "requested", "Issuance requested. An approver can now issue the ticket.")


async def recheck_balance(reference: str, viewer: BackOfficeUser, client_factory=None) -> None:
    """After a ticket is issued (travelian#68): record the supplier balance again and alert once if it is now low.
    Southair: a fresh read-only /my-profile (the value read before issuing doesn't include this ticket's charge), kept as a
    snapshot. SooperFare: its calculated balance now includes the ticket's charge. Never raises: the ticket is issued
    either way, so a failure here is only logged."""
    from app.db import finance
    from app.services import supplier_wallets

    try:
        provider = (await run_in_threadpool(store.load, reference)).provider or "southair"
        if provider == "southair":
            await supplier_wallets.refresh_southair(finance.Actor(viewer.user_id, viewer.role), client_factory)
        elif provider == "sooperfare":
            await supplier_wallets.check_low(provider)
    except supplier_wallets.BalanceUnavailable as error:
        logger.warning("Balance after issue not checked: %s", error.message)
    except Exception:  # noqa: BLE001 - never changes the issue result
        logger.warning("Balance after issue not checked", exc_info=True)


async def issue(reference: str, viewer: BackOfficeUser, client_factory=None, accepted_cost_minor: int | None = None,
                sooperfare_factory=None) -> Outcome:
    """Issue the ticket (steps above); once it is issued, the supplier balance is checked again (recheck_balance)."""
    outcome = await _issue(reference, viewer, client_factory, accepted_cost_minor, sooperfare_factory)
    if outcome.result == "issued":
        await recheck_balance(reference, viewer, client_factory)
    return outcome


async def _issue(reference: str, viewer: BackOfficeUser, client_factory, accepted_cost_minor: int | None,
                 sooperfare_factory) -> Outcome:
    _require(viewer)
    loaded = await run_in_threadpool(store.load, reference)
    if loaded.test_booking:  # before anything else: a TEST booking never reaches a supplier's ticketing call
        raise IssuanceRefused(409, f"{store.TEST_BOOKING} Nothing was issued.", "test_booking")
    reason = ticketing_reason(loaded.provider, loaded.product)
    if reason:
        raise IssuanceRefused(409, f"{reason} Nothing was issued.", "ticketing_off")
    if loaded.provider == "sooperfare":
        return await _issue_sooperfare(reference, viewer, sooperfare_factory)
    target = await run_in_threadpool(store.claim, reference, viewer.user_id, _now(), settings.ticketing_deadline_margin_seconds)
    previous = target.issuance_status

    async def abort(status: int, message: str, reason_code: str, **extra) -> None:
        await run_in_threadpool(store.release, target, viewer.user_id, previous, "issuance_aborted", {"reason_code": reason_code})
        raise IssuanceRefused(status, message, reason_code, **extra)

    if (target.provider or "southair") != "southair":  # never ask Southair about another supplier's PNR
        await abort(409, NOT_ISSUABLE_HERE, "provider_not_supported")
    try:
        client = (client_factory or configured_client)()
    except SouthairError:
        await abort(503, "Ticketing isn't available right now. Nothing was issued.", "not_configured")
    # 2. Before sending anything.
    try:
        state = normalize.pnr_state(await client.pnr_info(target.pnr), target.pnr)
    except SouthairError:
        await abort(502, "The PNR couldn't be checked with the supplier. Nothing was issued.", "pnr_check_failed")
    if state.kind == "issued":
        tickets = match_tickets(target, state)
        if tickets:
            await run_in_threadpool(store.mark_issued, target, viewer.user_id, tickets, "issuance_found_issued", {"pnr_status": state.status})
            return Outcome("issued", "issued", "The supplier already shows this PNR as ticketed. Ticket numbers are recorded.", tickets)
    if state.kind != "not_issued":
        await run_in_threadpool(store.release, target, viewer.user_id, "unknown", "issuance_unknown",
                                {"reason_code": "pnr_unclear_before_ticketing", "pnr_status": state.status})
        return Outcome("unknown", "unknown", "The PNR's ticketing status is unclear, so nothing was sent. Check with the supplier and resolve it.")
    if not state.can_ticketing:
        await abort(409, "The supplier won't ticket this PNR now. Nothing was issued.", "not_ticketable")
    if state.total is None or target.supplier_cost_minor is None:
        await abort(409, "The supplier's total for this PNR has changed. Nothing was issued.", "total_mismatch")
    new_cost = to_minor(state.total.value)
    if new_cost != target.supplier_cost_minor:
        # The airline's price moved. The selling price stays (app.services.pricing): refused when it is below the
        # new cost; otherwise the issuer confirms the new cost (profit changes) and issues again.
        if new_cost > target.total_minor:
            await abort(409, "The airline's price is now above the selling price. Nothing was issued.", "below_cost")
        if accepted_cost_minor != new_cost:
            costs = {"new_supplier_cost": {"amount_minor": new_cost, "currency": target.currency},
                     "previous_supplier_cost": {"amount_minor": target.supplier_cost_minor, "currency": target.currency},
                     "profit": {"amount_minor": target.total_minor - new_cost, "currency": target.currency}} if viewer.visibility.cost_and_margin else {}
            await abort(409, "The airline's price has changed. The selling price stays; confirm the new cost to issue. Nothing was issued.",
                        "supplier_cost_changed", accepted_supplier_cost={"amount_minor": new_cost, "currency": target.currency}, **costs)
        target = await run_in_threadpool(store.update_cost, target, viewer.user_id, new_cost)
    try:
        profile = await client.profile()
    except SouthairError:
        await abort(502, "The supplier balance couldn't be checked. Nothing was issued.", "balance_check_failed")
    balance = normalize.parse_amount(((profile or {}).get("limit") or {}).get("cap"))
    if balance is None or to_minor(balance.value) < target.supplier_cost_minor:
        await abort(409, "Not enough supplier balance to issue this ticket. Nothing was issued.", "insufficient_balance")

    # 3. One ticketing call. Never retried.
    response, sent_error = None, None
    try:
        response = await client.ticketing(ticket_ref=target.supplier_ref, cash_amount=normalize.cash_amount(state.total))
    except SouthairError as error:
        sent_error = error.error_code
    except Exception:  # noqa: BLE001 - any failure after sending is an unclear outcome, never a retry
        sent_error = "unexpected"
    said_ok = isinstance(response, dict) and str(response.get("booking_status") or "").upper() == "OK"
    answer = {"booking_status": response.get("booking_status"), "invoice_no": response.get("invoice_no")} if isinstance(response, dict) else {"error": sent_error}
    logger.info("Ticketing answered", extra={"ok": said_ok, "error_code": sent_error})

    # 4. Decide from pnr_info.
    try:
        after = normalize.pnr_state(await client.pnr_info(target.pnr), target.pnr)
    except SouthairError:
        after = None
    if after is not None and after.kind == "issued" and (tickets := match_tickets(target, after)):
        await run_in_threadpool(store.mark_issued, target, viewer.user_id, tickets, "issuance_issued",
                                {"supplier_answer": "ok" if said_ok else (sent_error or "not_ok"), "pnr_status": after.status}, answer)
        return Outcome("issued", "issued", "Ticket issued.", tickets)
    if after is not None and after.kind == "not_issued" and not said_ok:
        await run_in_threadpool(store.release, target, viewer.user_id, previous, "issuance_failed",
                                {"supplier_error": sent_error or "not_ok", "pnr_status": after.status}, answer)
        return Outcome("not_issued", previous, "The supplier didn't issue the ticket and shows nothing paid. You can try again.")
    await run_in_threadpool(store.release, target, viewer.user_id, "unknown", "issuance_unknown",
                            {"reason_code": "unclear_after_ticketing", "supplier_answer": "ok" if said_ok else (sent_error or "not_ok"),
                             "pnr_status": after.status if after else None}, answer)
    return Outcome("unknown", "unknown", "The ticket's status is unknown. Don't issue again: verify it with the supplier or resolve it.")


#: What went wrong, in plain words, per reason code of the audit trail (the admin shows it on the booking).
REASONS = {
    "insufficient_balance": "Not enough balance in the supplier account to pay for the ticket. Nothing was sent to the airline.",
    "balance_check_failed": "The supplier account balance couldn't be checked. Nothing was sent to the airline.",
    "pnr_check_failed": "The supplier couldn't be reached to check the PNR before issuing (timeout or error). Nothing was sent.",
    "not_configured": "Ticketing isn't set up on the server (supplier connection settings). Nothing was sent.",
    "not_ticketable": "The supplier said this PNR can't be ticketed now. Nothing was sent.",
    "total_mismatch": "The supplier's total for this PNR couldn't be matched to the booking. Nothing was sent.",
    "below_cost": "The airline's price is now above the selling price. Nothing was sent.",
    "supplier_cost_changed": "The airline's price changed; the new cost has to be confirmed before issuing. Nothing was sent.",
    "provider_not_supported": "This supplier's tickets can't be issued from Travelian yet: issue it in the supplier's portal.",
    "pnr_unclear_before_ticketing": "Before issuing, the supplier showed the PNR in an unclear state, so nothing was sent.",
    "unclear_after_ticketing": "The ticketing request was sent, but the supplier's PNR check afterwards didn't show a ticket number "
                               "for every passenger, so the result couldn't be confirmed.",
    "pnr_unclear_on_check": "A check with the supplier showed the PNR in an unclear state (not clearly issued or unpaid).",
    "sooperfare_confirmed": "SooperFare issued the booking, but its answer has no ticket numbers: record them from SooperFare's portal.",
    "sooperfare_no_tickets": "SooperFare shows the booking as issued but hasn't given every ticket number yet. Press Check with supplier in a few minutes.",
    "sooperfare_unclear_on_check": "SooperFare's record is unclear (paid but not issued, or not on hold). Check SooperFare's portal before doing anything.",
    "sooperfare_confirm_unclear": "The confirm was sent to SooperFare, but its record doesn't show the booking as issued.",
    "ticketing_off": "Ticketing is switched off for this supplier.",
}
ANSWERS = {"ok": "accepted the request", "not_ok": "did not accept the request", "timeout": "did not answer in time",
           "unexpected": "gave an unreadable answer"}


@dataclass
class Attempt:
    at: datetime
    outcome: str  # issued, not_issued, unknown, aborted, checked
    reason: str
    supplier_answer: str | None = None
    pnr_status: str | None = None
    can_check: bool = False


def describe(status: str, last: tuple[str, dict, datetime] | None) -> Attempt | None:
    """The last issuing attempt or check, with its real reason in plain words (never just "unknown")."""
    if last is None:
        return None
    action, details, at = last
    answer = details.get("supplier_answer") or details.get("supplier_error")
    said = ANSWERS.get(str(answer), f"answered with error '{answer}'") if answer else None
    pnr = details.get("pnr_status")
    code = details.get("reason_code")
    if action == "issuance_aborted":
        outcome, reason = "aborted", REASONS.get(code, f"Issuing stopped before anything was sent ({code}).")
    elif action == "issuance_failed":
        outcome, reason = "not_issued", (f"The supplier {said or 'refused the request'} and its PNR shows no ticket and nothing paid. "
                                         "It's safe to issue again.")
    elif action == "issuance_unknown":
        outcome = "unknown"
        reason = REASONS.get(code, "The result couldn't be confirmed with the supplier.")
        if said:
            reason += f" The supplier {said}."  # the PNR status is its own field (shown beside the reason)
    elif action == "issuance_checked":
        outcome, reason = "checked", "Checked with the supplier: no ticket and nothing paid. It's safe to issue again."
    elif action == "issuance_resolved":
        outcome = "issued" if details.get("outcome") == "issued" else "not_issued"
        reason = f"Resolved by staff: {details.get('reason') or 'no reason given'}."
    elif action == "issuance_verified":
        outcome = "issued" if details.get("outcome") == "issued" else "not_issued"
        reason = "Checked with the supplier: " + ("the ticket is issued; ticket numbers are recorded." if outcome == "issued"
                                                   else "no ticket and nothing paid. It's safe to issue again.")
    else:  # issuance_issued / issuance_found_issued
        outcome, reason = "issued", "Ticket issued; ticket numbers are recorded."
    can_check = status == "unknown" or (status in store.ISSUABLE and action in ("issuance_failed", "issuance_aborted"))
    return Attempt(at, outcome, reason, said, pnr, can_check)


async def last_attempt(target: store.Target) -> Attempt | None:
    return describe(target.issuance_status, await run_in_threadpool(store.last_attempt, target.id))


#: Re-reads of SooperFare's booking list after a confirm (seconds to wait before each): its record can lag the answer.
SOOPERFARE_READS = (0, 3, 6)


class _SfState:
    """SooperFare's tickets in the shape match_tickets reads (passenger type, ticket number), in SooperFare's order."""

    def __init__(self, state: dict | None):
        self.tickets = list((state or {}).get("tickets") or [])


def _sooperfare_tickets(target: store.Target, state: dict | None) -> dict[int, str] | None:
    """Our travelers' ticket numbers from SooperFare's record, only when SooperFare shows the booking issued and gives
    a number for every traveler (matched by passenger type and order)."""
    if not state or not state.get("is_issued"):
        return None
    return match_tickets(target, _SfState(state))


def _sf_summary(state: dict | None) -> dict:
    """What SooperFare's record said, for the audit trail (no personal data)."""
    if not state:
        return {"listed": False}
    return {"listed": True, "reservation_status": state.get("reservation_status"), "is_paid": state.get("is_paid"),
            "is_issued": state.get("is_issued"), "pnr": state.get("pnr"),
            "tickets_given": sum(1 for _, number in state.get("tickets") or [] if number), "travelers_listed": len(state.get("tickets") or [])}


async def _read_sooperfare(client, reference: str, waits: tuple[int, ...] | None = None) -> dict | None:
    """SooperFare's record for the booking, re-read (read-only) until it shows ticket numbers or the reads run out."""
    import asyncio

    from app.services import sooperfare_bookings

    record = await run_in_threadpool(sooperfare_bookings._load, reference)
    state = None
    for wait in SOOPERFARE_READS if waits is None else waits:
        if wait:
            await asyncio.sleep(wait)
        state = await sooperfare_bookings._read_state(client, record) if record else None
        if state and state.get("is_issued") and state.get("tickets") and all(number for _, number in state["tickets"]):
            break
    return state


def _sooperfare_client(factory=None):
    from app.vendors.flights.sooperfare import client as sooperfare

    try:
        return (factory or sooperfare.SooperfareClient)()
    except sooperfare.SooperfareError:
        raise IssuanceRefused(503, "SooperFare isn't set up on the server. Nothing was sent.", "not_configured") from None


def _money(minor: int | None, currency: str) -> str:
    return "unknown" if minor is None else f"{'Rs' if currency == 'PKR' else currency} {minor / 100:,.0f}"


CHECK_SOOPERFARE = "Don't issue again: press Check with supplier, or check the booking in SooperFare's portal."


def balance_shortfall(supplier_id: str, amount_minor: int) -> str | None:
    """D52 for a supplier whose balance is calculated (manual balance mode): the reason issuing would take it below zero, else
    None. None too before migration 0037, for a supplier not on Supplier Balances, or one whose balance isn't set up yet
    (not known, so it can't be shown to go negative). Southair (balance from its API) is checked live in _issue instead."""
    from fastapi import HTTPException
    from sqlalchemy.exc import SQLAlchemyError

    from app.db import finance, online_attempts, supplier_ledger

    try:
        with supplier_ledger.agencies.application_engine().begin() as connection:
            if not online_attempts.has_finance_ops(connection):
                return None
            supplier = supplier_ledger.supplier(connection, supplier_id)
            if supplier is None or supplier.balance_mode != "manual":
                return None
            supplier_ledger.lock(connection, supplier_id)
            try:
                supplier_ledger.covers(connection, supplier_id, amount_minor, what="Issuing this ticket")
            except finance.FinanceRefused:  # no balance figures here: whoever issues may not see supplier balances
                return f"Not enough supplier balance to issue this ticket. Ask Finance to top up {supplier.name} first."
    except HTTPException:  # no application database configured (unit tests): issuing can't run without one anyway
        return None
    except SQLAlchemyError:  # fail closed: a balance that can't be read never lets the confirm (a charge) go out
        return "The supplier balance couldn't be checked."
    return None


async def _issue_sooperfare(reference: str, viewer: BackOfficeUser, factory=None) -> Outcome:
    """SooperFare: one POST /v1/booking/confirm (never retried), then SooperFare's own record (booking/list) decides:
    issued with a ticket number for every traveler -> PNR and ticket numbers saved, Ticket issued. Anything else ->
    'unknown' (Check with supplier reads booking/list again; it never confirms again). Only with SOOPERFARE_CONFIRM_ENABLED
    (it issues and charges Travelian's SooperFare balance). The confirm request and SooperFare's answer are kept on the
    booking (supplier_response.ticketing) for the record of the first real confirm (docs/SOOPERFARE_API_RESPONSES.md)."""
    from app.services import sooperfare_bookings
    from app.vendors.flights.sooperfare import client as sooperfare

    try:
        request = await run_in_threadpool(sooperfare_bookings.confirm_request, reference)
    except sooperfare_bookings.SupplierCancelError as error:
        raise IssuanceRefused(error.status, f"{error.message} Nothing was issued.", error.code) from None
    client = _sooperfare_client(factory)  # before the claim: a missing setup never leaves the booking 'processing'
    target = await run_in_threadpool(store.claim, reference, viewer.user_id, _now(), settings.ticketing_deadline_margin_seconds)
    previous = target.issuance_status
    body = request["body"]

    async def abort(message: str, code: str, **details) -> None:
        await run_in_threadpool(store.release, target, viewer.user_id, previous, "issuance_aborted", {"reason_code": code, **details})
        raise IssuanceRefused(409 if code != "sooperfare_check_failed" else 502, f"{message} Nothing was sent to SooperFare.", code)

    # Before sending (the confirm charges Travelian's balance): the request matches the locked booking, and SooperFare's
    # own record shows it on hold, unpaid, not issued, at the supplier cost stored on the booking.
    if body["booking_ref_id"] != target.supplier_ref or body["txn_ref_id"] != target.reference or target.supplier_cost_minor is None \
            or round(target.supplier_cost_minor / 100, 2) != body["amount"] or body["currency"] != target.currency:
        await abort("The booking changed while issuing started. Reload and try again.", "booking_changed")
    before = await _read_sooperfare(client, reference, waits=(0,))
    if before is None:
        await abort("SooperFare's booking list couldn't be read, or doesn't list this booking.", "sooperfare_check_failed")
    if before.get("is_issued") or before.get("is_paid"):
        await abort("SooperFare already shows this booking as paid or issued. Press Check with supplier instead of issuing.",
                    "sooperfare_already_paid", **_sf_summary(before))
    if before.get("reservation_status") != "active" or before.get("is_cancelled"):
        await abort(f"SooperFare shows the booking as {before.get('reservation_status') or 'not on hold'}, not on hold.",
                    "sooperfare_not_held", **_sf_summary(before))
    if before.get("billable_minor") != target.supplier_cost_minor:
        await abort(f"SooperFare's price for this booking ({_money(before.get('billable_minor'), target.currency)}) differs from the cost "
                    f"recorded ({_money(target.supplier_cost_minor, target.currency)}). Check the fare with SooperFare before issuing.",
                    "supplier_cost_changed_at_sooperfare", **_sf_summary(before))
    # D52: the confirm charges Travelian's SooperFare balance; refused when that would take the calculated balance below zero.
    short = await run_in_threadpool(balance_shortfall, target.provider or "sooperfare", target.supplier_cost_minor)
    if short:
        await abort(short, "insufficient_balance")
    sent_at = _now().isoformat()
    try:
        answer = await client.confirm(body["booking_ref_id"], body["txn_ref_id"], body["amount"], body["currency"], body["remarks"])
    except sooperfare.SooperfareError as error:
        # confirm charges Travelian's balance: an error answer doesn't prove nothing happened. Back to a retryable status only
        # for a plain refusal (400/422) AND when SooperFare's own record still shows the booking active, unpaid, not issued.
        state = None
        if error.status in (400, 422):
            record = await run_in_threadpool(sooperfare_bookings._load, reference)
            state = await sooperfare_bookings._read_state(client, record) if record else None
        untouched = bool(state) and state.get("reservation_status") == "active" and not state.get("is_paid") and not state.get("is_issued")
        response = {"confirm": {"request": request, "sent_at": sent_at, "error": error.error_code, "http_status": error.status,
                                "message": error.public_message}, "booking_list": _sf_summary(state)}
        if untouched:
            await run_in_threadpool(store.release, target, viewer.user_id, previous, "issuance_failed",
                                    {"supplier": "sooperfare", "supplier_error": error.public_message, "http_status": error.status}, response)
            return Outcome("not_issued", previous, f"SooperFare refused to issue: {error.public_message} Its record shows nothing paid or issued, "
                                                   "so you can fix the reason and issue again.")
        await run_in_threadpool(store.release, target, viewer.user_id, "unknown", "issuance_unknown",
                                {"reason_code": "sooperfare_confirm_unclear", "supplier_answer": error.error_code, "http_status": error.status,
                                 "message": error.public_message}, response)
        said = f"SooperFare answered: {error.public_message}" if error.status else f"SooperFare didn't answer ({error.error_code.replace('_', ' ')})."
        return Outcome("unknown", "unknown", f"{said} It may still have issued. {CHECK_SOOPERFARE}")
    state = await _read_sooperfare(client, reference)
    tickets = _sooperfare_tickets(target, state)
    response = {"confirm": {"request": request, "sent_at": sent_at, "answer": answer}, "booking_list": _sf_summary(state)}
    if tickets:
        await run_in_threadpool(store.mark_issued, target, viewer.user_id, tickets, "issuance_issued",
                                {"supplier": "sooperfare", "supplier_answer": "ok", "pnr": (state or {}).get("pnr") or target.pnr,
                                 "message": str(answer.get("message") or "")[:200]}, response)
        return Outcome("issued", "issued", "SooperFare issued the ticket. The PNR and ticket numbers are recorded.", tickets)
    issued = bool(state and state.get("is_issued"))
    await run_in_threadpool(store.release, target, viewer.user_id, "unknown", "issuance_unknown",
                            {"reason_code": "sooperfare_no_tickets" if issued else "sooperfare_confirm_unclear", "supplier_answer": "ok",
                             "pnr_status": (state or {}).get("reservation_status"), "message": str(answer.get("message") or "")[:200]},
                            response)
    if issued:
        return Outcome("unknown", "unknown", "SooperFare shows the booking as issued but hasn't given every ticket number yet. "
                                             "Press Check with supplier in a few minutes. Don't issue again.")
    return Outcome("unknown", "unknown", f"SooperFare accepted the request but doesn't show the booking as issued yet. {CHECK_SOOPERFARE}")


async def _verify_sooperfare(reference: str, viewer: BackOfficeUser, target: store.Target, factory=None) -> Outcome:
    """Check with supplier for SooperFare (read-only: booking/list only, never confirm)."""
    previous = target.issuance_status
    state = await _read_sooperfare(_sooperfare_client(factory), reference, waits=(0,))
    if state is None:
        raise IssuanceRefused(502, "SooperFare's booking list couldn't be read, or doesn't list this booking. Nothing was changed.")
    summary = _sf_summary(state)
    tickets = _sooperfare_tickets(target, state)
    if tickets:
        await run_in_threadpool(store.mark_issued, target, viewer.user_id, tickets, "issuance_verified",
                                {"outcome": "issued", "supplier": "sooperfare", "pnr_status": state.get("reservation_status")},
                                None, (previous,))
        return Outcome("issued", "issued", "SooperFare shows the ticket as issued. The PNR and ticket numbers are recorded.", tickets)
    untouched = not state.get("is_issued") and not state.get("is_paid") and state.get("reservation_status") == "active"
    if untouched and previous != "unknown":  # after a plain refusal (nothing was accepted): safe
        await run_in_threadpool(store.log_check, target, viewer.user_id, {"outcome": "not_issued", "supplier": "sooperfare", **summary})
        return Outcome("not_issued", previous, "SooperFare shows the booking still on hold: not paid and not issued. It's safe to issue again.")
    if untouched:
        # A confirm reached SooperFare (or may have): its list can lag, so "still on hold" here never re-opens issuing by itself.
        # Staff check SooperFare's portal and resolve it with a reason (Mark as not issued), so nobody confirms twice.
        await run_in_threadpool(store.log_check, target, viewer.user_id, {"outcome": "unclear_after_confirm", "supplier": "sooperfare", **summary})
        return Outcome("unknown", "unknown", "SooperFare's list still shows the booking on hold (not paid, not issued), but a confirm was sent, "
                                             "so it may still be processing. Check SooperFare's portal. If it really wasn't issued, use Mark as "
                                             "not issued with a reason; then you can issue again.")
    if previous != "unknown":
        await run_in_threadpool(store.mark_unknown, target, viewer.user_id,
                                {"reason_code": "sooperfare_unclear_on_check", "supplier": "sooperfare", **summary}, (previous,))
    what = ("issued, but not every ticket number is given yet" if state.get("is_issued")
            else "paid but not issued" if state.get("is_paid") else f"{state.get('reservation_status') or 'no status'}")
    return Outcome("unknown", "unknown", f"SooperFare shows the booking as {what}. Don't issue again: check SooperFare's portal, "
                                         "then press Check with supplier again or resolve it with a reason.")


async def verify(reference: str, viewer: BackOfficeUser, client_factory=None, sooperfare_factory=None) -> Outcome:
    """Check with the supplier (read-only: pnr_info only, never ticketing) after an unknown or failed issuance.
    Ticket numbers found -> recorded as issued. Booked, no ticket, nothing paid -> safe to issue again. Unclear -> unknown
    (issuing stays blocked until verified or resolved). Never re-issues."""
    outcome = await _verify(reference, viewer, client_factory, sooperfare_factory)
    if outcome.result == "issued":  # the supplier shows the ticket issued (and charged): check the balance again
        await recheck_balance(reference, viewer, client_factory)
    return outcome


async def _verify(reference: str, viewer: BackOfficeUser, client_factory, sooperfare_factory) -> Outcome:
    _require(viewer)
    target = await run_in_threadpool(store.load, reference)
    _issuable_provider(target)
    previous = target.issuance_status
    if previous != "unknown":
        last = await run_in_threadpool(store.last_attempt, target.id)
        if not (previous in store.ISSUABLE and last and last[0] in ("issuance_failed", "issuance_aborted")):
            raise IssuanceRefused(409, "Checking with the supplier is for an unknown or failed issuance only.")
    if target.provider == "sooperfare":
        return await _verify_sooperfare(reference, viewer, target, sooperfare_factory)
    try:
        state = normalize.pnr_state(await (client_factory or configured_client)().pnr_info(target.pnr), target.pnr)
    except SouthairError:
        raise IssuanceRefused(502, "The PNR couldn't be checked with the supplier. Nothing was changed.") from None
    if state.kind == "issued" and (tickets := match_tickets(target, state)):
        await run_in_threadpool(store.mark_issued, target, viewer.user_id, tickets, "issuance_verified",
                                {"outcome": "issued", "pnr_status": state.status}, None, (previous,))
        return Outcome("issued", "issued", "The supplier shows the ticket as issued. Ticket numbers are recorded.", tickets)
    if state.kind == "not_issued":
        if previous == "unknown":
            await run_in_threadpool(store.resolve_not_issued, target, viewer.user_id, "issuance_verified",
                                    {"outcome": "not_issued", "pnr_status": state.status})
            return Outcome("not_issued", "not_issued", "The supplier shows no ticket and nothing paid. It's safe to issue again.")
        await run_in_threadpool(store.log_check, target, viewer.user_id, {"outcome": "not_issued", "pnr_status": state.status})
        return Outcome("not_issued", previous, "The supplier shows no ticket and nothing paid. It's safe to issue again.")
    if previous != "unknown":
        await run_in_threadpool(store.mark_unknown, target, viewer.user_id,
                                {"reason_code": "pnr_unclear_on_check", "pnr_status": state.status}, (previous,))
    return Outcome("unknown", "unknown", "The supplier's answer is unclear (PNR status "
                   f"{state.status or 'not given'}). Don't issue again: confirm with the supplier and resolve it with a reason.")


async def resolve(reference: str, viewer: BackOfficeUser, outcome: str, reason: str, tickets: dict[int, str]) -> Outcome:
    """Mark an unknown issuance as issued (ticket numbers for every traveler) or not issued, with a reason."""
    _require(viewer)
    target = await run_in_threadpool(store.load, reference)
    if target.issuance_status != "unknown":
        raise IssuanceRefused(409, "Only an unknown issuance can be resolved.")
    details = {"outcome": outcome, "reason": reason}
    if outcome == "issued":
        positions = {position for position, _ in target.traveler_types}
        if not positions or set(tickets) != positions:
            raise IssuanceRefused(422, "Give a ticket number for every traveler.")
        await run_in_threadpool(store.mark_issued, target, viewer.user_id, tickets, "issuance_resolved", details, None, ("unknown",))
        return Outcome("issued", "issued", "Marked as issued.", tickets)
    await run_in_threadpool(store.resolve_not_issued, target, viewer.user_id, "issuance_resolved", details)
    return Outcome("not_issued", "not_issued", "Marked as not issued. Issuing is possible again.")
