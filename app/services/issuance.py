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


#: Providers whose tickets the gateway can issue. SooperFare's /v1/booking/confirm takes payment from Travelian's
#: SooperFare account and its contract (txn_ref_id, the answer, ticket numbers) isn't confirmed yet, so SooperFare
#: bookings are refused here before anything is sent; staff issue them in SooperFare's portal (docs/roadmap.md).
ISSUING_PROVIDERS = ("southair",)
NOT_ISSUABLE_HERE = "SooperFare tickets can't be issued from Travelian yet. Issue this ticket in SooperFare's portal. Nothing was issued."


def ticketing_on(provider: str | None) -> bool:
    """Issuing is possible for this booking's provider (and switched on)."""
    return (provider or "southair") in ISSUING_PROVIDERS and settings.southair_ticketing_enabled


def _issuable_provider(target) -> None:
    if (target.provider or "southair") not in ISSUING_PROVIDERS:
        raise IssuanceRefused(409, NOT_ISSUABLE_HERE, "provider_not_supported")


def _require(viewer: BackOfficeUser) -> None:
    if not viewer.can(ISSUE_TICKETS):
        raise IssuanceRefused(403, "Issuing tickets requires the issue tickets permission.")


async def request(reference: str, viewer: BackOfficeUser, note: str | None = None) -> Outcome:
    await run_in_threadpool(store.request, reference, viewer.user_id, _now(), settings.ticketing_deadline_margin_seconds, note)
    return Outcome("requested", "requested", "Issuance requested. An approver can now issue the ticket.")


async def issue(reference: str, viewer: BackOfficeUser, client_factory=None, accepted_cost_minor: int | None = None) -> Outcome:
    _require(viewer)
    if not settings.southair_ticketing_enabled:
        raise IssuanceRefused(409, "Ticketing is turned off. Nothing was issued.")
    target = await run_in_threadpool(store.claim, reference, viewer.user_id, _now(), settings.ticketing_deadline_margin_seconds)
    previous = target.issuance_status

    async def abort(status: int, message: str, reason_code: str, **extra) -> None:
        await run_in_threadpool(store.release, target, viewer.user_id, previous, "issuance_aborted", {"reason_code": reason_code})
        raise IssuanceRefused(status, message, reason_code, **extra)

    if (target.provider or "southair") not in ISSUING_PROVIDERS:  # never ask Southair about another supplier's PNR
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


async def verify(reference: str, viewer: BackOfficeUser, client_factory=None) -> Outcome:
    """Check with the supplier (read-only: pnr_info only, never ticketing) after an unknown or failed issuance.
    Ticket numbers found -> recorded as issued. Booked, no ticket, nothing paid -> safe to issue again. Unclear -> unknown
    (issuing stays blocked until verified or resolved). Never re-issues."""
    _require(viewer)
    target = await run_in_threadpool(store.load, reference)
    _issuable_provider(target)
    previous = target.issuance_status
    if previous != "unknown":
        last = await run_in_threadpool(store.last_attempt, target.id)
        if not (previous in store.ISSUABLE and last and last[0] in ("issuance_failed", "issuance_aborted")):
            raise IssuanceRefused(409, "Checking with the supplier is for an unknown or failed issuance only.")
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
