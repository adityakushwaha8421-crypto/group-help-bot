"""Case orchestration: evidence analysis -> order search -> matching -> Betix posting -> verification.
All external effects (browser, AI, Telegram) are injectable so the flow is unit-testable."""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.browser import AdminError, LayoutChanged, LoginFailed, ManualAuthRequired
from app.admin.matcher import Candidate, MatchResult, match_orders
from app.ai.extractor import Extraction, Field, extract_from_text
from app.cases.correlation import hard_missing, merge_case_extraction, missing_items
from app.cases.state_machine import transition
from app.config import get_settings
from app.db.models import KIND_WITHDRAWAL, Case, CaseStatus, EvidenceType, OrderCandidate, OrderMatch
from app.db.repository import (
    add_verification_event,
    audit,
    case_evidence_requested,
    case_has_out_kind,
    find_case_that_sent_order,
    get_case_for_update,
    latest_candidate,
    list_candidates,
    list_case_betix_messages,
    list_case_messages,
    list_evidence,
    list_verification_events,
    lock_order_id,
)
from app.evidence.manager import analyze_evidence, download_evidence
from app.followups.service import cancel_case_followups, schedule_case_followups
from app.telegram.confirmation import Classification, is_confirmed
from app.telegram.notifications import (
    format_confirmed,
    format_info,
    format_manual_review,
    format_withdrawal_reversed,
    get_bot,
    notify_admin,
)
from app.telegram.upi_check import ocr_upi, parse_pi_answer, upi_ending_match
from app.utils.logging import get_logger
from app.utils.timeutil import fmt_local, utcnow
from app.workers.queue import enqueue

log = get_logger("cases")

OrderSearch = Callable[..., Awaitable[list[Candidate]]]
_order_search: OrderSearch | None = None
_downloader = None


def set_order_search(fn: OrderSearch | None) -> None:
    global _order_search
    _order_search = fn


_payout_lookup = None
_statement_checker = None


def set_payout_lookup(fn) -> None:
    global _payout_lookup
    _payout_lookup = fn


def set_statement_checker(fn) -> None:
    global _statement_checker
    _statement_checker = fn


async def _find_payout(withdraw_id: str):
    if _payout_lookup is not None:
        return await _payout_lookup(withdraw_id)
    from app.admin.payouts import find_payout

    return await find_payout(withdraw_id)


async def _check_statement(path, account: str, password: str | None):
    """Open the statement (with the password when it is protected) and compare its account with `account`."""
    if _statement_checker is not None:
        return await _statement_checker(path, account)
    from pathlib import Path

    from app.evidence.pdf import decrypt_pdf, pdf_is_encrypted, pdf_view
    from app.evidence.statement_account import UNKNOWN, AccountCheck, check_statement

    if not path:
        return AccountCheck(UNKNOWN, "none")
    view = pdf_view(path) or Path(path)
    if pdf_is_encrypted(view):
        view = decrypt_pdf(path, password) if password else None
        if view is None:
            return AccountCheck(UNKNOWN, "none")
    return await check_statement(Path(view), account)


def _get_order_search() -> OrderSearch:
    if _order_search is not None:
        return _order_search
    from app.admin.orders import find_candidates

    return find_candidates


def set_downloader(fn) -> None:
    global _downloader
    _downloader = fn


async def _download(ev):
    if _downloader is not None:
        return await _downloader(ev)
    return await download_evidence(get_bot(), ev)


# ------------------------------------------------------------------ escalate / fail
async def escalate_case(session: AsyncSession, case: Case, reason: str, extra: str = "") -> None:
    await cancel_case_followups(session, case, f"escalated: {reason}")
    await transition(session, case, CaseStatus.ESCALATED, reason=reason, strict=False)
    case.failure_reason = reason
    await audit(session, "CASE_ESCALATED", case_id=case.case_id, result="escalated", details={"reason": reason})
    await notify_admin(
        session,
        kind="manual_review",
        text=format_manual_review(case, reason, extra),
        case=case,
        dedupe_suffix=reason[:60],
    )


TERMINAL_OK = {CaseStatus.VERIFIED.value, CaseStatus.ALREADY_SUCCESS.value, CaseStatus.ALREADY_SENT.value}


async def fail_case(session: AsyncSession, case: Case, reason: str, actor: str = "system") -> bool:
    """Mark the case FAILED. A case that already ended well (confirmed, or the order was already Success in
    Illunise) is never failed afterwards: returns False and leaves it untouched."""
    if case.status in TERMINAL_OK:
        return False
    await cancel_case_followups(session, case, f"failed: {reason}")
    await transition(session, case, CaseStatus.FAILED, reason=reason, actor=actor, strict=False)
    case.failure_reason = reason
    await audit(session, "CASE_FAILED", case_id=case.case_id, actor=actor, result="failed", details={"reason": reason})
    return True


# ------------------------------------------------------------------ processing
UTR_NOT_FOUND = "UTR Not Found ❌\n\nPlease send a clear payment screenshot where the UTR/Transaction ID is visible."


def screenshot_utr(evidence) -> str | None:
    """The UTR / transaction id actually read from a PAYMENT SCREENSHOT. None when no screenshot shows one."""
    for ev in evidence:
        if ev.type != EvidenceType.payment_screenshot.value:
            continue
        ex = ((ev.analysis or {}).get("extraction")) or {}
        payload = (ev.analysis or {}).get("payload") or {}
        for value in (
            (ex.get("utr") or {}).get("value"),
            (payload.get("utr") or {}).get("value"),
            (ex.get("transaction_reference") or {}).get("value"),
            (payload.get("transaction_reference") or {}).get("value"),
        ):
            if value:
                return str(value)
    return None


AI_UNAVAILABLE = "\U0001f9e0 Can't read the payment screenshot right now"


def ai_unavailable_reason(evidence) -> str | None:
    """Why the screenshot could not be READ (the AI service was down), or None when it was read."""
    for ev in evidence:
        if ev.type != EvidenceType.payment_screenshot.value:
            continue
        a = ev.analysis or {}
        if a.get("unavailable"):
            return str(a.get("error") or "AI service unavailable")
    return None


async def hold_for_ai(session: AsyncSession, case: Case, why: str, *, force_send: bool = False) -> str:
    """The screenshot was never read because the AI service failed (no credits, outage). That is not the
    operator's problem: keep the case, say what is wrong ONCE, and retry by ourselves. Never "UTR Not Found"."""
    s = get_settings()
    mins = max(1, s.ai_retry_seconds // 60)
    case.failure_reason = (
        f"{AI_UNAVAILABLE} \u2014 the AI service is unavailable ({why}).\n"
        f"\U0001f501 I'll retry automatically every {mins} min; nothing needs resending."
    )
    await transition(session, case, CaseStatus.WAITING_FOR_INPUT, reason=f"AI unavailable: {why}", strict=False)
    await audit(session, "AI_UNAVAILABLE", case_id=case.case_id, result=why[:120], source="payment_screenshot")
    await notify_admin(
        session,
        kind="ai_unavailable",
        case=case,
        dedupe_suffix=case.case_id,
        text=format_manual_review(
            case,
            f"The payment screenshot could not be read: the AI service is unavailable ({why}).",
            f"The case is on hold and retried every {mins} min. Fix the service and it continues on its own.",
        ),
    )
    case.processing_version += 1
    await enqueue(
        "process_case_job",
        case.case_id,
        case.processing_version,
        True,
        force_send,
        job_id=f"ai-retry-{case.case_id}-{case.processing_version}",
        defer_seconds=s.ai_retry_seconds,
    )
    return "ai_unavailable"


async def ask_for_a_readable_screenshot(session: AsyncSession, case: Case) -> str:
    """No UTR visible on the screenshot: tell the operator, keep the case waiting, search nothing."""
    case.failure_reason = UTR_NOT_FOUND
    await transition(
        session, case, CaseStatus.WAITING_FOR_INPUT, reason="no UTR visible on the screenshot", strict=False
    )
    await audit(session, "UTR_NOT_FOUND", case_id=case.case_id, result="screenshot", source="payment_screenshot")
    # ONE message: the status change above already rewrote the case's own live message with these words. Only a
    # case that has no live message (recovered after a restart, say) is told separately.
    if not (case.progress_chat_id and case.progress_message_id):
        await notify_admin(session, kind="utr_not_found", case=case, text=UTR_NOT_FOUND, dedupe_suffix=case.case_id)
    return "utr_missing"


async def ready_withdrawal(session: AsyncSession, case: Case) -> str:
    """WITHDRAWAL: nothing to read or match - the withdrawal id and the statement are all Betix needs. The case
    is labelled by the id the group will see ("BXWD-..."), which also makes the one-post-per-id guard, /status
    and /add work for it exactly as for an order id."""
    label = f"BX{case.withdrawal_id}"
    case.betex_pay_order_id = label
    first = await find_case_that_sent_order(session, label, exclude_case_id=case.case_id)
    if first is not None:
        await mark_already_sent(session, case, first)
        return "already_sent"
    await audit(session, "WITHDRAWAL_READY", case_id=case.case_id, result=label)
    await transition(
        session, case, CaseStatus.READY_FOR_BETIX, reason="withdrawal id + statement received", strict=False
    )
    return "ready"


ACCOUNT_MISMATCH = "ACCOUNT DOES NOT MATCH"


async def process_withdrawal(session: AsyncSession, case: Case, evidence, *, force_send: bool = False) -> str:
    """WITHDRAWAL VERIFICATION: find the exact withdrawal in Illunise payouts, read the bank account it was paid
    to, and send to Betix ONLY when the statement belongs to that same account. Anything else stays here."""
    from app.ai.analyzer import AIUnavailable
    from app.evidence.statement_account import MATCH, MISMATCH, mask_account

    wd = case.withdrawal_id
    await transition(session, case, CaseStatus.SEARCHING_ORDER, reason="looking up the withdrawal", strict=False)
    version0 = case.processing_version
    password = case.statement_password
    statements = [e for e in evidence if e.type == EvidenceType.bank_statement.value]
    await session.commit()  # unlocked while the browser and the PDF work

    problem: tuple[str, str] | None = None
    payout = None
    check = None
    ai_down: str | None = None
    try:
        payout = await _find_payout(wd)
    except ManualAuthRequired as exc:
        problem = (f"Admin login needs manual authentication: {exc}", "Run: python -m app.admin.login --manual")
    except (LoginFailed, LayoutChanged, AdminError) as exc:
        problem = (f"Illunise payouts could not be read: {exc}", "")
    except Exception as exc:  # noqa: BLE001
        log.exception("payout lookup crashed", case_id=case.case_id)
        problem = (f"Payout lookup error: {exc!r}", "")
    if problem is None and payout is not None and payout.account:
        try:
            for ev in statements:  # any one of the statements sent may be the right one
                check = await _check_statement(await _download(ev), payout.account, password)
                if check.ok:
                    break
        except AIUnavailable as exc:
            ai_down = str(exc)

    if await _relock(session, case, version0) is None:
        return "stale"
    if ai_down:
        return await hold_for_ai(session, case, ai_down, force_send=force_send)
    if problem is not None:
        await escalate_case(session, case, *problem)
        return "escalated"
    if payout is None:
        await escalate_case(
            session, case, f"Withdrawal {wd} was not found in Illunise payouts.", "Nothing was sent to Betix."
        )
        return "escalated"
    if payout.amount is not None and case.amount is None:
        case.amount = payout.amount
    details = {"payout": {**payout.as_dict(), "account": mask_account(payout.account)}}
    details["check"] = {"result": check.result, "how": check.how, "seen": mask_account(check.seen)} if check else None
    await audit(
        session,
        "WITHDRAWAL_ACCOUNT_CHECK",
        case_id=case.case_id,
        result=check.result if check else "unknown",
        details=details,
    )
    paid_to = f"Paid to: {payout.bank or 'bank'} a/c {mask_account(payout.account)}"
    if payout.beneficiary:
        paid_to += f" ({payout.beneficiary})"
    if check is not None and check.result == MATCH:
        return await ready_withdrawal(session, case)
    if check is not None and check.result == MISMATCH:
        await escalate_case(
            session,
            case,
            f"{ACCOUNT_MISMATCH} \u2014 the statement is for a/c {mask_account(check.seen)}, "
            f"the withdrawal went to a/c {mask_account(payout.account)}.",
            f"{paid_to}\nNot sent to Betix. Ask for the statement of the account the withdrawal was paid to.",
        )
        return "escalated"
    await escalate_case(
        session,
        case,
        "The statement's account number could not be verified against the withdrawal.",
        f"{paid_to}\nNot sent to Betix. Please check the statement by hand.",
    )
    return "escalated"


async def _relock(session: AsyncSession, case: Case, version0: int) -> Case | None:
    """Take the case's row lock back after an unlocked stretch (file reads, AI calls, the Illunise browser).

    Returns None when a newer message reached the case meanwhile (processing_version moved on): that message's
    own job re-runs the case from the top, so this run stops. What it produced is kept - the analysis lives on
    the evidence rows and is reused."""
    await session.refresh(case, with_for_update=True)  # SELECT ... FOR UPDATE on Postgres; re-reads the row
    if case.processing_version != version0:
        log.info("case changed while unlocked; this run stops", case_id=case.case_id)
        return None
    return case


async def process_case(session: AsyncSession, case_id: str, *, force: bool = False, force_send: bool = False) -> str:
    """Analyze evidence, search the admin panel, match, and mark READY_FOR_BETIX.
    Returns: deferred | not_ready | ready | ambiguous | escalated | already | failed"""
    s = get_settings()
    case = await get_case_for_update(session, case_id)
    if case is None:
        return "missing"
    if case.status not in (
        CaseStatus.WAITING_FOR_INPUT.value,
        CaseStatus.ANALYZING_EVIDENCE.value,
        CaseStatus.SEARCHING_ORDER.value,
    ):
        return "already"
    now = utcnow()
    if not force and case.last_input_at and (now - case.last_input_at) < timedelta(seconds=s.case_debounce_seconds):
        return "deferred"
    evidence = await list_evidence(session, case.case_id)
    missing = missing_items(case, evidence)
    if force_send and missing and not hard_missing(missing):
        # FORCE SEND: screenshot + mobile are in and the wait for the statement / video ran out. Go with what we have.
        await audit(session, "FORCE_SEND", case_id=case.case_id, result="without " + ", ".join(missing))
        missing = []
    if missing:
        # Give the submitter the whole collection window; then ask for the missing items once.
        if case.last_input_at and (now - case.last_input_at) > timedelta(minutes=s.case_collection_window_minutes):
            await notify_admin(
                session,
                kind="missing_evidence",
                case=case,
                text=format_manual_review(case, "Required evidence missing: " + ", ".join(missing)),
            )
        return "not_ready"
    if case.kind == KIND_WITHDRAWAL:
        return await process_withdrawal(session, case, evidence, force_send=force_send)

    # ---- 1. evidence analysis
    await transition(session, case, CaseStatus.ANALYZING_EVIDENCE, reason="evidence complete", strict=False)
    version0 = case.processing_version
    # Commit here: the status is visible, and the row lock - and the pool connection's transaction - are released
    # while files are downloaded and the AI reads them (up to a minute). Nothing else waits on this case, and one
    # slow case cannot hold the pool for the others. The case row itself is not touched until _relock.
    await session.commit()
    merged = Extraction.from_dict(case.extraction)
    # text messages first (deterministic)
    for m in await list_case_messages(session, case.case_id):
        if m.text:
            merged = merged.merge(
                extract_from_text(
                    m.text, registration_pattern=s.registration_pattern, betex_pattern=s.betex_order_id_pattern
                )
            )
    hint = f"Customer mobile supplied by the operator: {case.mobile}."
    order = {
        EvidenceType.payment_screenshot.value: 0,
        EvidenceType.bank_statement.value: 1,
        EvidenceType.payment_video.value: 2,
        EvidenceType.other.value: 3,
    }
    for ev in sorted(evidence, key=lambda e: order.get(e.type, 9)):
        await _download(ev)
        ex = await analyze_evidence(session, ev, context_hint=hint, statement_password=case.statement_password)
        # The screenshot is the payment itself; the video shows the same screen; a statement is a LIST of
        # transactions the model has to pick a row from. So for the same confidence: screenshot > video >
        # statement (merge keeps the higher confidence; later sources are nudged down).
        nudge = {EvidenceType.payment_video.value: 0.02, EvidenceType.bank_statement.value: 0.05}.get(ev.type, 0.03)
        if ev.type != EvidenceType.payment_screenshot.value:
            for k in Extraction.FIELDS:
                f = getattr(ex, k)
                f.confidence = max(0.0, f.confidence - nudge)
        if (
            ev.type == EvidenceType.bank_statement.value
            and ex.amount.value is not None
            and merged.amount.value is not None
            and abs(float(ex.amount.value) - float(merged.amount.value)) > s.order_amount_tolerance
        ):
            # The model read a DIFFERENT row of the statement (live 2026-09-15: Rs 140 at 08:30 for a Rs 3,949.52
            # payment at 23:09). Its amount, time and reference describe another transaction: drop them.
            log.info("statement row does not match the payment; its fields are ignored", case_id=case.case_id)
            await audit(
                session,
                "STATEMENT_ROW_MISMATCH",
                case_id=case.case_id,
                result=f"{ex.amount.value} vs {merged.amount.value}",
                source="bank_statement",
            )
            for k in ("amount", "payment_time", "utr", "transaction_reference", "payer_name", "upi_id"):
                setattr(ex, k, Field())
        merged = merged.merge(ex)
        if merged.amount.value is not None and merged.payment_time.value:
            hint += (
                f" Screenshot suggests amount {merged.amount.value} at "
                f"{fmt_local(merged.payment_time.value, s.timezone)}."
            )
    if await _relock(session, case, version0) is None:
        return "stale"
    case.extraction = merged.as_dict()
    merge_case_extraction(case, merged)
    await audit(
        session,
        "EVIDENCE_ANALYZED",
        case_id=case.case_id,
        result="ok",
        confidence=merged.amount.confidence,
        details={k: getattr(merged, k).as_dict() for k in Extraction.FIELDS if getattr(merged, k).value is not None},
    )
    why = ai_unavailable_reason(evidence)
    if why:
        # The reader did not run, so nothing is known about the screenshot yet - least of all that it lacks a UTR.
        return await hold_for_ai(session, case, why, force_send=force_send)
    if (case.failure_reason or "").startswith(AI_UNAVAILABLE) or case.failure_reason == UTR_NOT_FOUND:
        case.failure_reason = None  # the screenshot has been read now: the hold / "send a clearer one" is over
    if s.require_screenshot_utr and not screenshot_utr(evidence):
        # The screenshot must SHOW the UTR / transaction id. Never take one from anywhere else, never invent one.
        return await ask_for_a_readable_screenshot(session, case)
    if case.amount is None:
        await escalate_case(session, case, "Could not extract the payment amount from the evidence.")
        return "escalated"

    # ---- 2. order search
    await transition(session, case, CaseStatus.SEARCHING_ORDER, reason="searching admin orders")
    if not case.mobile:
        await escalate_case(session, case, "No customer mobile number on the case; cannot search Illunise.")
        return "escalated"
    await audit(
        session,
        "ORDER_SEARCH_STARTED",
        case_id=case.case_id,
        result=case.mobile[:2] + "******" + case.mobile[-2:],
        source="illunise_admin",
    )
    await session.commit()  # unlocked while the browser works: the slowest stage must not block anyone else
    search_error: tuple[str, str | None] | None = None
    candidates: list[Candidate] = []
    try:
        candidates = await _get_order_search()(case.mobile, case.amount, case.payment_time)
    except ManualAuthRequired as exc:
        search_error = (f"Admin login needs manual authentication: {exc}", "Run: python -m app.admin.login --manual")
    except LoginFailed as exc:
        search_error = (f"Admin login failed: {exc}", None)
    except LayoutChanged as exc:
        search_error = (f"Admin site layout changed / selectors not found: {exc}", None)
    except AdminError as exc:
        search_error = (f"Admin automation error: {exc}", None)
    except Exception as exc:  # noqa: BLE001
        log.exception("order search crashed", case_id=case.case_id)
        search_error = (f"Order search error: {exc!r}", None)
    if search_error is not None:
        if await _relock(session, case, version0) is None:
            return "stale"
        reason, hint = search_error
        await escalate_case(session, case, reason, hint) if hint else await escalate_case(session, case, reason)
        return "escalated"

    # FALLBACK SEARCHES. If nothing under the customer's number was created around the payment, the order may sit
    # under another registered number: the panel can be searched by UTR and by the exact padded amount, both of
    # which identify a single payment. Whatever they find is scored with the same matcher.
    def _kw():
        return dict(
            weights=s.match_weights,
            threshold=s.order_match_threshold,
            ambiguity_gap=s.order_match_ambiguity_gap,
            time_window_minutes=s.payment_time_window_minutes,
            amount_tolerance=s.order_amount_tolerance,
            time_rule=s.time_rule,
            gateway_name=s.betix_gateway_name,
            compatible_statuses=s.compatible_statuses,
            expired_statuses=s.expired_statuses,
            success_statuses=s.success_statuses,
            time_tiebreak_minutes=s.order_time_tiebreak_minutes,
        )

    first = match_orders(merged, case.mobile, candidates, **_kw())
    fallbacks_tried: list[str] = []

    def fits(sc) -> bool:
        """Right amount (within the tolerance) AND created before the payment inside the window."""
        return (sc.signals.get("amount") or {}).get("score") == 1.0 and (
            (sc.signals.get("time") or {}).get("score") or 0
        ) >= 0.7

    # The number given has no order that fits this payment (none at all, or only other amounts / other times): it
    # cannot be what identifies the order, so every order near the payment time with the right amount is looked at.
    if first.decision not in ("MATCHED", "AMBIGUOUS") and not any(fits(sc) for sc in first.scored):
        seen = {c.betex_order_id or c.illunise_order_id for c in candidates}
        queries = []
        if case.utr:
            queries.append(("utr", case.utr))
        if case.amount is not None:
            queries.append(("padded_amount", f"{case.amount:.2f}"))
            # The panel lists the ORDER amount (Rs 2,879), the customer paid the padded one (Rs 2,878.99): the
            # padded figure finds nothing there. The whole-rupee amount does - and since the search is limited to
            # the payment day and the minutes before the payment, it stays a handful of orders.
            # The same goes one rupee either way (a Rs 199.00 payment for a Rs 200 order): every whole-rupee
            # amount within the tolerance, nearest first, the higher one first on a tie (padding pays LESS).
            tol = s.order_amount_tolerance
            lo, hi = math.ceil(case.amount - tol - 1e-9), math.floor(case.amount + tol + 1e-9)
            nearby = [n for n in range(lo, hi + 1) if n > 0 and abs(n - case.amount) > 0.005]
            for n in sorted(nearby, key=lambda n: (round(abs(n - case.amount), 2), -n)):
                queries.append(("order_amount", str(n)))
        for how, q in queries:
            fallbacks_tried.append(how)
            try:
                extra = await _get_order_search()(q, case.amount, case.payment_time)
            except Exception as exc:  # noqa: BLE001
                log.warning("fallback search failed", case_id=case.case_id, by=how, error=str(exc)[:160])
                continue
            for c in extra:
                key = c.betex_order_id or c.illunise_order_id
                if key not in seen:
                    c.found_by = how
                    c.registration_waived = how != "utr"  # a UTR hit is pinned by the UTR itself
                    candidates.append(c)
                    seen.add(key)
            await audit(
                session,
                "ORDER_FALLBACK_SEARCH",
                case_id=case.case_id,
                result=how,
                source="illunise_admin",
                details={"found": len(extra)},
            )
            if match_orders(merged, case.mobile, candidates, **_kw()).decision in ("MATCHED", "AMBIGUOUS"):
                break  # found it: no need to search further

    if await _relock(session, case, version0) is None:
        return "stale"
    attempt = 1 + max([c.search_attempt for c in await list_candidates(session, case.case_id)] or [0])
    result: MatchResult = match_orders(
        merged,
        case.mobile,
        candidates,
        weights=s.match_weights,
        threshold=s.order_match_threshold,
        ambiguity_gap=s.order_match_ambiguity_gap,
        time_window_minutes=s.payment_time_window_minutes,
        amount_tolerance=s.order_amount_tolerance,
        time_rule=s.time_rule,
        gateway_name=s.betix_gateway_name,
        compatible_statuses=s.compatible_statuses,
        expired_statuses=s.expired_statuses,
        success_statuses=s.success_statuses,
        time_tiebreak_minutes=s.order_time_tiebreak_minutes,
    )
    cand_rows: dict[int, OrderCandidate] = {}
    for sc in result.scored:
        c = sc.candidate
        row = OrderCandidate(
            case_id=case.case_id,
            search_attempt=attempt,
            illunise_order_id=c.illunise_order_id,
            betex_order_id=c.betex_order_id,
            registration_number=c.registration_number,
            amount=c.amount,
            order_time=c.order_time,
            status=c.status,
            utr=c.utr,
            upi_id=c.upi_id,
            payer_name=c.payer_name,
            gateway=c.gateway,
            betix_plat_order_no=c.betix_plat_order_no,
            raw=c.raw,
            score=sc.score,
            signals=sc.signals,
        )
        session.add(row)
        await session.flush()
        cand_rows[id(sc)] = row
        await audit(
            session,
            "ORDER_CANDIDATE_FOUND",
            case_id=case.case_id,
            result=c.betex_order_id or c.illunise_order_id,
            confidence=sc.score,
            source="illunise_admin",
            details=sc.signals,
        )
    best_row = cand_rows.get(id(result.best)) if result.best else None
    session.add(
        OrderMatch(
            case_id=case.case_id,
            candidate_id=best_row.id if best_row else None,
            decision=result.decision,
            confidence=result.confidence,
            runner_up_confidence=result.runner_up.score if result.runner_up else None,
            illunise_order_id=result.best.candidate.illunise_order_id if result.best else None,
            betex_order_id=result.best.candidate.betex_order_id if result.best else None,
            details={"reason": result.reason, "candidates": len(candidates)},
        )
    )
    case.match_confidence = result.confidence

    if result.decision == "MATCHED":
        return await select_order(
            session,
            case,
            result.best.candidate,
            reason=result.reason,
            confidence=result.confidence,
            signals=result.best.signals,
        )
    if result.decision == "AMBIGUOUS":
        await audit(
            session,
            "ORDER_MATCH_AMBIGUOUS",
            case_id=case.case_id,
            result="ambiguous",
            confidence=result.confidence,
            details={"reason": result.reason},
        )
        close = close_candidates(result)
        note = ""
        if s.betix_pi_check and close:  # even one: a doubt is settled by its UPI, not by a manual review
            started, note = await start_pi_check(session, case, close, result.reason)
            if started:
                return "checking_upi"
        lines = [
            f"- {sc.candidate.betex_order_id or sc.candidate.illunise_order_id}: score {sc.score:.2f}"
            for sc in result.scored[:5]
        ]
        await transition(session, case, CaseStatus.ORDER_MATCH_AMBIGUOUS, reason=result.reason)
        await alert_ambiguous(
            session,
            case,
            "Multiple orders match: " + result.reason,
            "Candidates:\n" + "\n".join(lines) + (f"\n{note}" if note else "") + "\nCheck them in the Illunise panel.",
        )
        return "ambiguous"
    # ANY DOUBT -> ask Betix. No order cleared the bar, but some fit the amount and were created before the payment
    # inside the window: each one's UPI is checked with /pi, closest first, and the first that fits the
    # screenshot's receiver UPI is the order. Nothing fits -> manual review, as before.
    doubtful = doubt_candidates(result)
    pi_note = ""
    if s.betix_pi_check and doubtful:
        started, pi_note = await start_pi_check(
            session, case, doubtful, f"no order is a clear match ({result.reason}); {len(doubtful)} fit amount and time"
        )
        if started:
            return "checking_upi"
    tz = s.timezone
    when = fmt_local(case.payment_time, tz, "%d %b %Y %H:%M") if case.payment_time else "?"
    if doubtful:
        # Orders DO sit right before the payment with the right amount: never "no order". Say which, and why none
        # of them could be confirmed (no receiver UPI on the screenshot, /pi not possible ...).
        first_fit = doubtful[0].candidate
        reason = (
            f"{len(doubtful)} order(s) of about ₹{case.amount:,.2f} were created just before the payment ({when}), "
            f"nearest {first_fit.betex_order_id}, but none could be confirmed" + (f": {pi_note}" if pi_note else ".")
        )
        lines = [
            f"- {sc.candidate.betex_order_id}: ₹{(sc.candidate.amount or 0):,.2f} · created "
            f"{fmt_local(sc.candidate.order_time, tz, '%d %b %H:%M')} · {sc.candidate.status or '-'}"
            for sc in doubtful
        ]
        await escalate_case(
            session,
            case,
            reason,
            "Nearby orders (closest first):\n" + "\n".join(lines) + "\nPick the right one in Illunise.",
        )
        return "escalated"
    if result.decision == "NO_CANDIDATES":
        tol = s.order_amount_tolerance
        reason = (
            f"No order of ₹{case.amount:,.2f} (±₹{tol:,.0f}) was created before the payment ({when}) - "
            "searched by the mobile number and by the amount."
        )
    else:
        best = result.best
        sig = best.signals
        near = (sig.get("time", {}).get("score") or 0) >= 0.7 and sig.get("amount", {}).get("score") == 1.0
        if near:
            # An order DOES sit right before the payment with the right amount: say what held it back instead
            # of claiming there is none.
            blockers = {
                "registration": "it is registered to another mobile number",
                "utr": f"the panel already holds a different UTR for it ({sig.get('utr', {}).get('candidate')})",
                "gateway": f"it belongs to another gateway ({sig.get('gateway', {}).get('candidate')})",
                "status": f"its status is {best.candidate.status}",
            }
            why = [text for key, text in blockers.items() if sig.get(key, {}).get("score") == 0.0]
            lead = sig.get("time", {}).get("lead_minutes")
            reason = (
                f"Order {best.candidate.betex_order_id or best.candidate.illunise_order_id} was created "
                f"{lead:.0f} min before the payment ({when}, ₹{case.amount:,.2f}) with the right amount, but "
                + (" and ".join(why) if why else "the overall score stayed too low")
                + f" (score {best.score:.2f} of {s.order_match_threshold:.2f})."
            )
        else:
            reason = (
                f"No order was created around the payment ({when}, ₹{case.amount:,.2f}) for this number; "
                f"best candidate scored only {best.score:.2f} of the required {s.order_match_threshold:.2f}."
            )
    lines = []
    for sc in result.scored[:3]:
        c = sc.candidate
        lines.append(
            f"- {c.betex_order_id or c.illunise_order_id}: {sc.score:.2f} · ₹{(c.amount or 0):,.2f} · created "
            f"{fmt_local(c.order_time, tz, '%d %b %H:%M')} · {c.status or '-'}"
            + (f" · found by {c.found_by}" if c.found_by != "mobile" else "")
        )
    top = ("Nearest orders:\n" + "\n".join(lines)) if lines else ""
    if fallbacks_tried:
        top += (
            ("\n" if top else "")
            + "Also searched by "
            + " and ".join(
                {"utr": "UTR", "padded_amount": "padded amount", "order_amount": "order amount"}.get(x, x)
                for x in dict.fromkeys(fallbacks_tried)
            )
            + ": no order for this payment."
        )
    top += "\nLikely: the order was made under another registered number, or the payment was made without an order."
    await escalate_case(session, case, reason, top)
    return "escalated"


# ------------------------------------------------------------------ posting
async def mark_already_sent(session: AsyncSession, case: Case, first: Case) -> None:
    """This order id is already in the Betix group (posted by `first`): end this case without posting again."""
    tz = get_settings().timezone
    when = fmt_local(first.betix_posted_at, tz, "%d %b %H:%M") if first.betix_posted_at else "just now"
    case.failure_reason = (
        f"Order {case.betex_pay_order_id} was already sent to the Betix group ({when}). Not sent again."
    )
    await cancel_case_followups(session, case, "order already sent by another case")
    await transition(session, case, CaseStatus.ALREADY_SENT, reason=case.failure_reason)
    await audit(
        session,
        "ORDER_ALREADY_SENT",
        case_id=case.case_id,
        result=case.betex_pay_order_id,
        details={"first_case": first.case_id, "first_message_id": first.betix_root_message_id},
    )


# ------------------------------------------------------------------ order selection + the /pi UPI tie-break
async def select_order(session: AsyncSession, case: Case, b, *, reason: str, confidence: float | None, signals) -> str:
    """The order is identified: record it, then READY_FOR_BETIX (or ALREADY_SUCCESS / ALREADY_SENT).
    `b` is a matcher Candidate or a stored OrderCandidate row (same attribute names)."""
    s = get_settings()
    case.illunise_order_id = b.illunise_order_id
    case.betex_pay_order_id = b.betex_order_id
    case.order_created_at = b.order_time
    case.order_amount = b.amount
    case.gateway = b.gateway
    case.betix_plat_order_no = b.betix_plat_order_no or case.betix_plat_order_no
    if confidence is not None:
        case.match_confidence = confidence
    if b.utr and not case.utr:
        case.utr = b.utr
    await transition(session, case, CaseStatus.ORDER_MATCH_FOUND, reason=reason)
    await audit(
        session,
        "ORDER_MATCH_SELECTED",
        case_id=case.case_id,
        result=b.betex_order_id,
        confidence=confidence,
        source="matcher",
        details=signals,
    )
    same_utr = bool(case.utr and getattr(b, "utr", None) and str(b.utr).strip() == str(case.utr).strip())
    if (b.status or "").strip().lower() in s.success_statuses and same_utr:
        # Illunise shows this order as Success WITH THIS PAYMENT'S UTR: it has been credited. Nothing to send.
        # A Success status alone proves nothing about this payment - then the order still goes to Betix.
        case.failure_reason = f"Order already Success in Illunise with this UTR (status: {b.status})"
        await transition(session, case, CaseStatus.ALREADY_SUCCESS, reason=case.failure_reason)
        await audit(
            session,
            "ORDER_ALREADY_SUCCESS",
            case_id=case.case_id,
            result=b.betex_order_id,
            source="illunise_admin",
            details={"status": b.status},
        )
        return "already_success"
    first = await find_case_that_sent_order(session, b.betex_order_id, exclude_case_id=case.case_id)
    if first is not None:
        await mark_already_sent(session, case, first)
        return "already_sent"
    await transition(session, case, CaseStatus.READY_FOR_BETIX, reason="Betex Pay order id identified")
    return "ready"


async def alert_ambiguous(session: AsyncSession, case: Case, reason: str, extra: str) -> None:
    await notify_admin(
        session,
        kind="ambiguous_match",
        case=case,
        text=format_manual_review(case, reason, extra),
        dedupe_suffix=reason[:60],
    )


def doubt_candidates(result: MatchResult) -> list:
    """NO clear match: the orders still worth asking Betix about - the amount fits, created before the payment
    inside the window, a Betix order (never another gateway's) - CLOSEST to the payment first."""
    s = get_settings()

    def lead(sc) -> float:
        v = (sc.signals.get("time") or {}).get("lead_minutes")
        return v if v is not None and v >= 0 else float("inf")

    fit = [
        sc
        for sc in result.scored
        if sc.candidate.betex_order_id
        and (sc.signals.get("amount") or {}).get("score") == 1.0
        # created BEFORE the payment: inside the window, or up to twice the window before it (paid late). An order
        # created after the payment, or far too long before it, is not a reasonable candidate.
        and ((sc.signals.get("time") or {}).get("score") or 0) > 0.0
        and (sc.signals.get("gateway") or {}).get("score") != 0.0
    ]
    return sorted(fit, key=lambda sc: (lead(sc), -sc.score))[: s.pi_check_max_orders]


def close_candidates(result: MatchResult) -> list:
    """The orders the matcher could not separate, CLOSEST TO THE PAYMENT FIRST: within the ambiguity gap of the best, and each one
    already a match on AMOUNT and on TIME (created before the payment, inside the window). The correct order =
    amount match + order time close to the payment + UPI match. Ties: the order created closest to the payment."""
    s = get_settings()
    if not result.best:
        return []
    top = result.best.score

    def plausible(sc) -> bool:
        amount = (sc.signals.get("amount") or {}).get("score")
        when = (sc.signals.get("time") or {}).get("score")
        return amount == 1.0 and when is not None and when >= 0.7

    def lead(sc) -> float:
        v = (sc.signals.get("time") or {}).get("lead_minutes")
        return v if v is not None and v >= 0 else float("inf")

    close = [
        sc
        for sc in result.scored
        if sc.candidate.betex_order_id
        and sc.score >= 0.75
        and top - sc.score < s.order_match_ambiguity_gap + 1e-9
        and plausible(sc)
    ]

    # CLOSEST ORDER TIME FIRST. The score cannot separate these orders (that is why they are here), and a paid
    # order of somebody else easily outscores the customer's own expired one by a hair. The order created nearest
    # before the payment is the likeliest, so it is asked first; an order holding the payment's UTR leads anyway.
    def holds_utr(sc) -> int:
        return 0 if (sc.signals.get("utr") or {}).get("score") == 1.0 else 1

    close.sort(key=lambda sc: (holds_utr(sc), lead(sc), -sc.score))
    return close[: s.pi_check_max_orders]


async def _ask_pi(session: AsyncSession, case: Case, order_id: str) -> None:
    """Send `/pi <ORDER-ID>` for ONE order and arm its answer timeout."""
    await get_poster().send_pi_query(session, case, order_id)
    await enqueue(
        "pi_check_timeout_job",
        case.case_id,
        order_id,
        job_id=f"pi-timeout-{case.case_id}-{order_id}",
        defer_seconds=get_settings().pi_check_timeout_seconds,
    )


async def start_pi_check(session: AsyncSession, case: Case, close: list, why: str) -> tuple[bool, str]:
    """Begin the one-by-one /pi check with the best candidate. Returns (started, note-for-the-alert)."""
    shot, how = await read_screenshot_upi(session, case)
    if shot is None:
        return False, f"UPI check not possible: {how}."
    order_ids = [sc.candidate.betex_order_id for sc in close]
    case.pi_check_orders = order_ids
    await transition(session, case, CaseStatus.CHECKING_ORDER_UPI, reason=f"{why}; checking each order's UPI (/pi)")
    await audit(
        session,
        "PI_CHECK_STARTED",
        case_id=case.case_id,
        result=order_ids[0],
        details={"orders_in_order": order_ids, "screenshot_upi": shot},
    )
    try:
        await _ask_pi(session, case, order_ids[0])
    except Exception as exc:  # noqa: BLE001
        log.exception("pi query failed", case_id=case.case_id)
        await transition(session, case, CaseStatus.ORDER_MATCH_AMBIGUOUS, reason=f"/pi query failed: {exc}")
        return False, f"UPI check failed: could not send /pi ({str(exc)[:80]})."
    return True, ""


async def pi_answers(session: AsyncSession, case: Case) -> tuple[list[str], dict[str, str | None]]:
    """(orders asked so far, in the order asked; {order id: Order's UPI or None} for those the Betix bot answered)."""
    s = get_settings()
    msgs = await list_case_betix_messages(session, case.case_id)
    asked: dict[int, str] = {}
    for m in msgs:
        if m.direction == "out" and m.kind == "pi_query":
            found = s.betex_order_id_pattern.search(m.text or "")
            if found:
                asked[m.message_id] = found.group(0).upper()
    answers: dict[str, str | None] = {}
    for m in msgs:
        if m.direction == "in" and m.reply_to_message_id in asked and m.sender_is_bot:
            oid, upi = parse_pi_answer(m.text)
            answers[oid or asked[m.reply_to_message_id]] = upi
    return list(asked.values()), answers


async def cleanup_pi_messages(session: AsyncSession, case: Case, order_id: str) -> dict:
    """The order is confirmed by its UPI: delete the `/pi <ORDER-ID>` request and the Betix bot's DIRECT reply to it
    from the Betix group (PI_CLEANUP=matched; "all" = every /pi of the case; "off" = keep). Never blocks the flow."""
    s = get_settings()
    if s.pi_cleanup == "off":
        return {}
    msgs = await list_case_betix_messages(session, case.case_id)
    queries = [
        m
        for m in msgs
        if m.direction == "out"
        and m.kind == "pi_query"
        and (s.pi_cleanup == "all" or order_id.upper() in (m.text or "").upper())
    ]
    qids = {m.message_id for m in queries}
    replies = [m for m in msgs if m.direction == "in" and m.sender_is_bot and m.reply_to_message_id in qids]
    poster = get_poster()
    deleted, failed = [], {}
    for m in [*queries, *replies]:
        err = await poster.delete_message(m.message_id)
        if err is None:
            deleted.append(m.message_id)
        else:
            failed[m.message_id] = err
    if failed:
        log.warning("pi cleanup: could not delete", case_id=case.case_id, failed=failed)
    await audit(
        session,
        "BETIX_PI_CLEANUP",
        case_id=case.case_id,
        result="ok" if not failed else "partial",
        details={"order": order_id, "deleted": deleted, "failed": failed},
    )
    return {"deleted": deleted, "failed": failed}


async def resolve_pi_check(session: AsyncSession, case_id: str, *, timed_out: str | None = None) -> str:
    """Advance the one-by-one /pi check.

    The current order (the last one asked) is answered:
      its UPI fits the screenshot UPI -> STOP, that is the order (READY_FOR_BETIX); the rest are never asked
      it does not fit                 -> ask the next candidate, or manual review when none is left
    `timed_out` = the order whose answer wait ran out: if it is still the unanswered current one -> manual review.
    Returns ready | already_success | already_sent | next | waiting | ambiguous | stale | not_checking."""
    s = get_settings()
    case = await get_case_for_update(session, case_id)
    if case is None or case.status != CaseStatus.CHECKING_ORDER_UPI.value:
        return "not_checking"
    asked, answers = await pi_answers(session, case)
    if not asked:
        return "waiting"
    current = asked[-1]
    shot, _ = await read_screenshot_upi(session, case)

    def checked_lines() -> list[str]:
        out = []
        for oid in asked:
            if oid in answers:
                out.append(
                    f"- {oid}: Order's UPI {answers[oid] or '-'} - "
                    f"{upi_ending_match(shot, answers[oid], min_chars=s.upi_ending_min_chars)[1]}"
                )
            else:
                out.append(f"- {oid}: (no answer from Betix)")
        return out

    unanswered = current not in answers
    if unanswered:
        if timed_out is None or timed_out != current:
            return "waiting" if timed_out is None else "stale"
        # No answer for THIS order: that is not a reason to give up on the others. Move on to the next candidate;
        # manual review only when every one of them has been asked.
        ok, why = False, "no answer from Betix"
        await audit(session, "PI_CHECK_RESULT", case_id=case.case_id, result=f"{current}: no answer")
    elif timed_out is not None:
        return "stale"  # that order was answered in time; the answer already moved the check on
    else:
        ok, why = upi_ending_match(shot, answers[current], min_chars=s.upi_ending_min_chars)
        await audit(
            session,
            "PI_CHECK_RESULT",
            case_id=case.case_id,
            result=f"{current}: {'match' if ok else 'no match'}",
            details={"order": current, "order_upi": answers[current], "screenshot_upi": shot, "why": why},
        )
    if ok:
        row = await latest_candidate(session, case.case_id, current)
        if row is not None:
            reason = f"UPI check (/pi): {current}'s UPI {answers[current]} fits the screenshot UPI {shot}"
            await cleanup_pi_messages(session, case, current)  # the order is confirmed: remove the /pi exchange
            session.add(
                OrderMatch(
                    case_id=case.case_id,
                    candidate_id=row.id,
                    decision="MATCHED_BY_UPI",
                    confidence=row.score,
                    illunise_order_id=row.illunise_order_id,
                    betex_order_id=row.betex_order_id,
                    details={"reason": reason, "checked": asked},
                )
            )
            return await select_order(
                session, case, row, reason=reason, confidence=row.score, signals={"pi_checked": asked, "why": why}
            )
    remaining = [oid for oid in (case.pi_check_orders or []) if oid not in asked]
    if remaining:
        try:
            await _ask_pi(session, case, remaining[0])
            return "next"
        except Exception as exc:  # noqa: BLE001
            log.exception("pi query failed", case_id=case.case_id)
            reason = f"Several close orders; could not send /pi for {remaining[0]}: {str(exc)[:80]}"
    else:
        silent = [oid for oid in asked if oid not in answers]
        reason = f"UPI check (/pi): all {len(asked)} close order(s) checked, none fits the screenshot UPI" + (
            f" ({len(silent)} got no answer from Betix)" if silent else ""
        )
    await transition(session, case, CaseStatus.ORDER_MATCH_AMBIGUOUS, reason=reason)
    await alert_ambiguous(
        session,
        case,
        reason,
        f"Screenshot UPI (OCR): {shot or '-'}\n" + "\n".join(checked_lines()) + "\nCheck them in the Illunise panel.",
    )
    return "ambiguous"


async def force_push(session: AsyncSession, case_id: str, poster, *, actor: str = "operator") -> str:
    """FORCE SEND (/push, or the button under "ALREADY WITH BETIX"): the operator wants this case in the Betix
    group although another case sent the same order earlier. Only ever on the operator's word.
    Returns: posted | escalated | missing | not_applicable"""
    case = await get_case_for_update(session, case_id)
    if case is None:
        return "missing"
    if case.status != CaseStatus.ALREADY_SENT.value:
        return "not_applicable"
    await audit(session, "FORCE_PUSH", case_id=case.case_id, actor=actor, result=case.betex_pay_order_id)
    case.failure_reason = None
    await transition(session, case, CaseStatus.READY_FOR_BETIX, reason=f"force send by {actor}", actor=actor)
    return await post_case_to_betix(session, case_id, poster, force=True)


async def post_case_to_betix(session: AsyncSession, case_id: str, poster, *, force: bool = False) -> str:
    case = await get_case_for_update(session, case_id)
    if case is None:
        return "missing"
    if case.status not in (CaseStatus.READY_FOR_BETIX.value, CaseStatus.POSTED_TO_BETIX.value):
        return "already"
    if case.betex_pay_order_id and not case.betix_root_message_id and not force:
        # The last gate before the group: under a per-order lock, refuse when another case has posted this order
        # id in the meantime (two submissions of the same payment processed side by side).
        await lock_order_id(session, case.betex_pay_order_id)
        first = await find_case_that_sent_order(
            session, case.betex_pay_order_id, exclude_case_id=case.case_id, posted_only=True
        )
        if first is not None:
            await mark_already_sent(session, case, first)
            return "already_sent"
    evidence = await list_evidence(session, case.case_id)
    try:
        if case.kind == KIND_WITHDRAWAL:
            await poster.post_withdrawal(session, case, evidence)  # "BXWD-..." + the statement as its reply
        else:
            await poster.post_case(session, case, evidence)
    except Exception as exc:  # noqa: BLE001
        log.exception("betix post failed", case_id=case.case_id)
        await escalate_case(session, case, f"Telegram posting to Betix group failed: {exc}")
        return "escalated"
    await transition(session, case, CaseStatus.POSTED_TO_BETIX, reason="evidence posted", strict=False)
    await transition(session, case, CaseStatus.WAITING_FOR_CONFIRMATION, reason="monitoring Betix replies")
    await schedule_case_followups(session, case)
    if get_settings().betix_extra_evidence_policy == "always":
        await post_late_evidence(session, case, poster, requested=True)
    return "posted"


EXTRA_EVIDENCE_TYPES = (EvidenceType.bank_statement.value, EvidenceType.payment_video.value)
_poster_factory: Callable[[], object] | None = None


def set_poster_factory(factory: Callable[[], object] | None) -> None:
    global _poster_factory
    _poster_factory = factory


def get_poster():
    if _poster_factory is not None:
        return _poster_factory()
    from app.telegram.betix_poster import BetixPoster

    return BetixPoster()


async def post_added_evidence(session: AsyncSession, case: Case, poster) -> int:
    """Send everything added to an already-posted case (`/add`) to the Betix group as REPLIES to that case's
    original payment-screenshot message. Any type, including another screenshot. Never standalone."""
    if not case.betix_root_message_id:
        return 0
    pending = [
        e
        for e in await list_evidence(session, case.case_id)
        if not e.posted_to_betix_message_id and e.type != EvidenceType.other.value
    ]
    if not pending:
        return 0
    n = await poster.post_late_evidence(session, case, pending)
    if await post_statement_password(session, case, poster):
        n += 1
    from app.telegram.progress import push

    await push(session, case)
    return n


async def post_late_evidence(session: AsyncSession, case: Case, poster, *, requested: bool | None = None) -> int:
    """Send the bank statement / payment video to the Betix group as REPLIES to the screenshot post.
    Governed by BETIX_EXTRA_EVIDENCE_POLICY: always | on_request (only after Betix asked) | never.
    Never sends anything before the screenshot post exists, never re-sends a file. Returns files sent."""
    s = get_settings()
    if s.betix_extra_evidence_policy == "never" or not case.betix_root_message_id:
        return 0
    if s.betix_extra_evidence_policy == "on_request":
        if requested is None:
            requested = await case_evidence_requested(session, case.case_id)
        if not requested:
            return 0
    pending = [
        e
        for e in await list_evidence(session, case.case_id)
        if e.type in EXTRA_EVIDENCE_TYPES and not e.posted_to_betix_message_id
    ]
    if not pending:
        return 0
    try:
        return await poster.post_late_evidence(session, case, pending)
    except Exception as exc:  # noqa: BLE001
        log.exception("late evidence post failed", case_id=case.case_id)
        await notify_admin(
            session,
            kind="late_evidence_failed",
            case=case,
            dedupe_suffix=str(len(pending)),
            text=format_info(case, "Could not send statement/video to Betix", str(exc)[:200]),
        )
        return 0


async def post_statement_password(session: AsyncSession, case: Case, poster) -> bool:
    """The operator sent the PDF password after the statement had already gone to Betix (or the statement was
    sent with no caption): forward the password as a reply to the screenshot, once. Policy-gated like the PDF."""
    s = get_settings()
    if s.betix_extra_evidence_policy == "never" or not case.betix_root_message_id or not case.statement_password:
        return False
    posted = await case_has_out_kind(session, case.case_id, EvidenceType.bank_statement.value)
    if posted is None:
        return False  # the PDF itself has not gone out (yet): its caption will carry it
    if posted.text and case.statement_password in posted.text:
        return False  # already in the PDF caption
    try:
        return (await poster.send_statement_password(session, case)) is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("statement password post failed", case_id=case.case_id, error=str(exc)[:160])
        return False


# ------------------------------------------------------------------ verification
async def verify_case(
    session: AsyncSession,
    case: Case,
    *,
    confirmed_by: str,
    actor: str = "betix",
    confirmation_type: str | None = None,
    confirmation_message_id: int | None = None,
    confirmation_user_id: int | None = None,
    confirmation_username: str | None = None,
    reversed_quote: str | None = None,
) -> bool:
    """`reversed_quote`: Betix answered a WITHDRAWAL with "Reversed" - the issue is solved on their side
    (the payout came back) and the staff pay the customer by hand. Closed like a confirmation, told as it is."""
    if case.status in TERMINAL_OK:
        return False
    from app.cases.state_machine import can_transition

    if not can_transition(case.status, CaseStatus.VERIFIED):
        log.warning("verify refused: transition not allowed", case_id=case.case_id, status=case.status)
        return False
    await cancel_case_followups(session, case, "confirmation detected")
    case.confirmed_by = confirmed_by
    case.verified_at = utcnow()
    case.confirmation_type = confirmation_type or case.confirmation_type or "manual"
    if reversed_quote is not None:
        case.confirmation_type = "reversed"
    case.confirmation_message_id = confirmation_message_id
    case.confirmation_user_id = confirmation_user_id
    case.confirmation_username = confirmation_username
    case.confirmation_at = case.verified_at
    await transition(
        session,
        case,
        CaseStatus.VERIFIED,
        reason=f"{'reversed' if reversed_quote is not None else 'confirmed'} by {confirmed_by}",
        actor=actor,
        strict=False,
    )
    await audit(
        session,
        "CASE_VERIFIED",
        case_id=case.case_id,
        actor=actor,
        result="reversed" if reversed_quote is not None else "verified",
        details={"confirmed_by": confirmed_by},
    )
    if reversed_quote is not None:
        await notify_admin(
            session,
            kind="withdrawal_reversed",
            case=case,
            text=format_withdrawal_reversed(case, confirmed_by, reversed_quote),
        )
        return True
    evidence = await list_evidence(session, case.case_id)
    await notify_admin(
        session,
        kind="payment_confirmed",
        case=case,
        parse_mode="HTML",
        text=format_confirmed(
            case, evidence, confirmed_by=confirmed_by, confirmed_at=case.verified_at, tz=get_settings().timezone
        ),
    )
    return True


# ------------------------------------------------------------------ withdrawal reversed by Betix
# Betix says "Reversed" -> the payout is READ -> the operator gets a "Refund now" button -> only their tap makes
# the bot click Refund -> "solved" is reported only once the panel shows Refunded. No refund without a person.
_refunder = None


def set_refunder(fn) -> None:
    global _refunder
    _refunder = fn


async def _refund(withdraw_id: str):
    if _refunder is not None:
        return await _refunder(withdraw_id)
    from app.admin.payouts import refund_payout

    return await refund_payout(withdraw_id)


async def start_reversal(
    session: AsyncSession, case: Case, *, actor: str, betix_message_id, sender_id, sender_username, quote: str
) -> str:
    """Betix said "Reversed". Remember who said it (the audit row survives a restart); the check runs as a job."""
    await cancel_case_followups(session, case, "Betix reversed the withdrawal")  # Betix has answered
    await audit(
        session,
        "BETIX_REVERSED",
        case_id=case.case_id,
        actor=actor,
        result=case.withdrawal_id,
        source="betix_group",
        details={
            "betix_message_id": betix_message_id,
            "sender_id": sender_id,
            "sender_username": sender_username,
            "quote": quote,
        },
    )
    await enqueue("reversal_check_job", case.case_id, job_id=f"reversal-{case.case_id}-{betix_message_id}")
    return "reversal_check_queued"


async def _reversal_info(session: AsyncSession, case_id: str) -> tuple[str, dict] | None:
    from sqlalchemy import select

    from app.db.models import AuditLog

    row = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.case_id == case_id, AuditLog.action == "BETIX_REVERSED")
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return (row.actor, dict(row.details or {})) if row else None


def _reversal_open(case: Case | None) -> bool:
    return bool(
        case
        and case.kind == KIND_WITHDRAWAL
        and case.withdrawal_id
        and case.status not in TERMINAL_OK
        and case.status not in (CaseStatus.ESCALATED.value, CaseStatus.FAILED.value)
    )


async def _close_reversed(session: AsyncSession, case: Case, actor: str, info: dict, payout) -> None:
    if payout is not None and payout.amount is not None:
        case.amount = payout.amount
    await verify_case(
        session,
        case,
        confirmed_by=actor,
        confirmation_message_id=info.get("betix_message_id"),
        confirmation_user_id=info.get("sender_id"),
        confirmation_username=info.get("sender_username"),
        reversed_quote=info.get("quote") or "Reversed",
    )


async def reversal_check(case_id: str) -> str:
    """READ the payout Betix reversed. Already Refunded -> solved. Success -> ask the operator to approve the
    refund (button). Anything else -> Manual Review. Nothing is clicked here.
    Returns: reversed | asked | escalated | skipped"""
    from app.db.session import session_scope
    from app.telegram.notifications import format_refund_request, refund_button

    async with session_scope() as session:
        case = await get_case_for_update(session, case_id)
        found = await _reversal_info(session, case_id) if _reversal_open(case) else None
        if found is None:
            return "skipped"
        wd = case.withdrawal_id
    actor, info = found
    problem, payout = None, None
    try:
        payout = await _find_payout(wd)
    except Exception as exc:  # noqa: BLE001
        log.warning("payout read failed", case_id=case_id, error=str(exc)[:200])
        problem = f"Illunise payouts could not be read: {exc}"
    async with session_scope() as session:
        case = await get_case_for_update(session, case_id)
        if not _reversal_open(case):
            return "skipped"
        status = (payout.status or "").strip().lower() if payout else ""
        if status == "refunded":
            await audit(session, "WITHDRAWAL_REFUND", case_id=case_id, result="already_refunded", details={"id": wd})
            await _close_reversed(session, case, actor, info, payout)
            return "reversed"
        if status == "success":
            if payout.amount is not None:
                case.amount = payout.amount
            await notify_admin(
                session,
                kind="refund_request",
                case=case,
                dedupe_suffix=str(info.get("betix_message_id") or ""),
                text=format_refund_request(case, actor, payout),
                reply_markup=refund_button(case),
            )
            return "asked"
        why = problem or (
            f"withdrawal {wd} was not found in Illunise payouts."
            if payout is None
            else f"the payout's status is {payout.status or 'unknown'}, not Success \u2014 nothing was refunded."
        )
        await escalate_case(
            session,
            case,
            f"Betix reversed this withdrawal ({actor}), but {why}",
            "Check the payout in Illunise by hand.",
        )
        return "escalated"


async def approve_refund(session: AsyncSession, case_id: str, approver: str) -> str:
    """The operator tapped "Refund now". Recorded once; the refund itself runs as a job.
    Returns: queued | already | not_applicable"""
    case = await get_case_for_update(session, case_id)
    if not _reversal_open(case) or await _reversal_info(session, case_id) is None:
        return "not_applicable"
    from sqlalchemy import select

    from app.db.models import AuditLog

    seen = await session.execute(
        select(AuditLog.id).where(AuditLog.case_id == case_id, AuditLog.action == "REFUND_APPROVED").limit(1)
    )
    if seen.first() is not None:
        return "already"
    await audit(session, "REFUND_APPROVED", case_id=case_id, actor=approver, result=case.withdrawal_id)
    await enqueue("refund_withdrawal_job", case_id, job_id=f"refund-{case_id}")
    return "queued"


async def refund_approved_withdrawal(case_id: str) -> str:
    """Runs only after REFUND_APPROVED: refund the exact payout and report "solved" once the panel shows Refunded.
    Returns: reversed | escalated | skipped"""
    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.session import session_scope

    async with session_scope() as session:
        case = await get_case_for_update(session, case_id)
        found = await _reversal_info(session, case_id) if _reversal_open(case) else None
        approved = await session.execute(
            select(AuditLog.actor).where(AuditLog.case_id == case_id, AuditLog.action == "REFUND_APPROVED").limit(1)
        )
        approver = approved.scalar_one_or_none()
        if found is None or approver is None:
            return "skipped"  # no "Reversed" from Betix, or no approval from the operator: never refund
        wd = case.withdrawal_id  # the EXACT id this case was opened with
    actor, info = found
    problem, result = None, None
    try:
        result = await _refund(wd)  # up to a few minutes: no database transaction is held meanwhile
    except Exception as exc:  # noqa: BLE001
        log.exception("refund crashed", case_id=case_id)
        problem = f"the refund could not be run ({exc})."
    async with session_scope() as session:
        case = await get_case_for_update(session, case_id)
        if not _reversal_open(case):
            return "skipped"
        await audit(
            session,
            "WITHDRAWAL_REFUND",
            case_id=case_id,
            actor=approver,
            result=result.outcome if result else "error",
            source="illunise_admin",
            details={
                "id": wd,
                "before": result.status_before if result else None,
                "after": result.status_after if result else None,
                "detail": result.detail if result else problem,
            },
        )
        if result is not None and result.ok:
            await _close_reversed(session, case, actor, info, result.payout)
            return "reversed"
        why = problem or {
            "not_success": f"the payout's status is {result.status_before or 'unknown'}, not Success \u2014 "
            "Refund was NOT clicked.",
            "not_found": f"withdrawal {wd} was not found in Illunise payouts.",
            "not_confirmed": f"Refund was clicked, but the panel does not show Refunded yet ({result.detail}).",
            "refund_failed": f"the refund did not go through ({result.detail}).",
        }.get(result.outcome, result.detail)
        await escalate_case(
            session,
            case,
            f"Betix reversed this withdrawal ({actor}), but it is NOT refunded: {why}",
            "Check the payout in Illunise by hand. Do not pay the customer before the panel shows Refunded.",
        )
        return "escalated"


async def apply_verification_signal(
    session: AsyncSession,
    case: Case,
    cls: Classification,
    *,
    authority: str,
    betix_message_id: int,
    actor: str,
    correlation_confidence: float,
    sender_id: int | None = None,
    sender_username: str | None = None,
    poster=None,
) -> str:
    """Record a classified Betix reply and act on it. Returns the action taken."""
    s = get_settings()
    label = {"system_bot": "SYSTEM", "group_member": "HUMAN", "self": "SELF"}.get(authority, "UNKNOWN")
    event_type = f"{label}_{cls.outcome}"
    ev = await add_verification_event(
        session,
        case_id=case.case_id,
        dedupe_key=f"msg:{betix_message_id}",
        event_type=event_type,
        authority=authority,
        betix_message_id=betix_message_id,
        actor=actor,
        confidence=cls.confidence,
        details={"classification": cls.as_dict(), "correlation_confidence": correlation_confidence},
    )
    if ev is None:
        return "duplicate"
    if cls.plat_order_nos and not case.betix_plat_order_no:
        case.betix_plat_order_no = cls.plat_order_nos[0]
    if case.status in TERMINAL_OK | {CaseStatus.FAILED.value}:
        return "terminal"
    # Only the Betix system bot and human members of the Betix group can change state.
    # Our own bot/account never confirms anything, and neither does anyone outside the group.
    if authority == "self":
        return "ignored_self"
    if authority == "unknown":
        if cls.outcome == "SUCCESS" and cls.confidence >= 0.7:
            await notify_admin(
                session,
                kind="unverified_confirmation",
                case=case,
                dedupe_suffix=str(betix_message_id),
                text=format_info(
                    case,
                    "Unverified confirmation (sender is not a Betix group member)",
                    f"From: {actor}\nText: {(cls.matched or '')[:120]}\nNot treated as authoritative.",
                ),
            )
        return "ignored_unknown_sender"
    if cls.outcome == "SUCCESS":
        if correlation_confidence < 0.8:
            await notify_admin(
                session,
                kind="weak_correlation_success",
                case=case,
                dedupe_suffix=str(betix_message_id),
                text=format_info(
                    case,
                    "Possible confirmation, weak correlation",
                    f"From: {actor}\nLinked by: low-confidence proximity.\nCheck the case if this belongs to it.",
                ),
            )
            return "success_weak_correlation"
        await audit(
            session,
            "BETIX_CONFIRMATION_DETECTED",
            case_id=case.case_id,
            actor=actor,
            result=authority,
            confidence=cls.confidence,
            source="betix_group",
            details=cls.as_dict(),
        )
        if authority == "system_bot":
            case.system_confirmed_at = case.system_confirmed_at or utcnow()
        else:
            case.reviewer_confirmed_at = case.reviewer_confirmed_at or utcnow()
            case.confirmed_by = case.confirmed_by or actor
        confirmed = is_confirmed(
            mode=s.confirmation_mode,
            system_success=case.system_confirmed_at is not None,
            reviewer_success=case.reviewer_confirmed_at is not None,
        )
        if confirmed:
            by = case.confirmed_by or actor
            if case.system_confirmed_at and case.reviewer_confirmed_at:
                by = f"{case.confirmed_by or 'reviewer'} + Betix system"
            elif case.system_confirmed_at:
                by = f"Betix system ({actor})"
            await verify_case(
                session,
                case,
                confirmed_by=by,
                confirmation_type=authority,
                confirmation_message_id=betix_message_id,
                confirmation_user_id=sender_id,
                confirmation_username=sender_username,
            )
            return "verified"
        # strict mode: half of the requirement satisfied -> inform admin once, keep monitoring & follow-ups
        which = "Betix system" if authority == "system_bot" else f"Betix group member {actor}"
        need = "a human Betix group member" if authority == "system_bot" else "the Betix system bot"
        await notify_admin(
            session,
            kind="partial_confirmation",
            case=case,
            dedupe_suffix=authority,
            text=format_info(
                case, f"Success reported by {which}", f"Strict mode: still waiting for {need} to confirm."
            ),
        )
        return "partial"
    if cls.outcome == "FAILED":
        await audit(
            session,
            "BETIX_FAILURE_REPORTED",
            case_id=case.case_id,
            actor=actor,
            result=authority,
            confidence=cls.confidence,
            source="betix_group",
            details=cls.as_dict(),
        )
        if "belong" in (cls.matched or "").lower():  # "❌ UPI Does not belong to us": manual review, no /upi
            await escalate_case(session, case, "Betix: UPI does not belong to us.", (cls.matched or "")[:200])
            return "escalated_failed"
        if case.kind == KIND_WITHDRAWAL and "revers" in (cls.matched or "").lower():
            # "Reversed" on a withdrawal: the payout came back. The payout is looked at (read-only) and the
            # operator is asked to approve the refund - see reversal_check / refund_approved_withdrawal.
            return await start_reversal(
                session,
                case,
                actor=actor,
                betix_message_id=betix_message_id,
                sender_id=sender_id,
                sender_username=sender_username,
                quote=(cls.matched or "")[:200],
            )
        await escalate_case(
            session, case, f"Betix reports the payment as FAILED / not received ({actor}).", (cls.matched or "")[:200]
        )
        return "escalated_failed"
    if cls.outcome == "NEED_MORE_EVIDENCE":
        # Betix asked: reply to the screenshot with whatever statement / video we hold (policy permitting),
        # and tell the admin what is still missing so they can send it to the bot.
        sent = await post_late_evidence(session, case, poster or get_poster(), requested=True)
        have = {e.type for e in await list_evidence(session, case.case_id)}
        missing = [t.replace("_", " ") for t in EXTRA_EVIDENCE_TYPES if t not in have]
        detail = (cls.matched or "")[:200]
        if sent:
            detail += f"\nSent {sent} file(s) to Betix as a reply to the screenshot."
        if missing:
            detail += "\nNot on file: " + ", ".join(missing) + ". Send it to the bot and it will be forwarded."
        await notify_admin(
            session,
            kind="need_more_evidence",
            case=case,
            dedupe_suffix=str(betix_message_id),
            text=format_info(case, "Betix asks for more evidence", detail),
        )
        return "need_more_evidence"
    return "recorded"


async def read_screenshot_upi(session: AsyncSession, case: Case) -> tuple[str | None, str]:
    """The payee ("Paid to") UPI OCR'd from the payment screenshot - the screenshot we posted to Betix. The ONLY
    source: never the video, the statement, or what the Betix bot says it recognised. Returns (upi, how) or
    (None, why)."""
    from pathlib import Path

    from app.ai.analyzer import get_analyzer

    shots = [e for e in await list_evidence(session, case.case_id) if e.type == EvidenceType.payment_screenshot.value]
    if not shots:
        return None, "no payment screenshot on the case"
    root = case.betix_root_message_id
    shot = next((e for e in shots if root and e.posted_to_betix_message_id == root), shots[0])
    analysis = dict(shot.analysis or {})
    reading = analysis.get("receiver_upi_ocr") or (analysis.get("payload") or {}).get("receiver_upi")
    if not (isinstance(reading, dict) and reading.get("value")) and "receiver_upi_ocr" not in analysis:
        # Read before the field existed, or the full read found none: one focused OCR pass on the image.
        path = Path(shot.local_path) if shot.local_path else None
        if path is None or not path.exists():
            return None, "screenshot file not available for OCR"
        try:
            reading = await get_analyzer().read_receiver_upi(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("receiver UPI OCR failed", case_id=case.case_id, error=repr(exc)[:160])
            return None, "screenshot OCR failed"
        shot.analysis = {**analysis, "receiver_upi_ocr": reading}
    return ocr_upi(reading)


async def summary_lines(session: AsyncSession, case: Case) -> str:
    """What /status shows: where the case stands, in a few lines."""
    from app.telegram.ui import b, case_label, code, esc, money, para

    ev = await list_evidence(session, case.case_id)
    types = [e.type.replace("_", " ") for e in ev]
    events = await list_verification_events(session, case.case_id)
    tz = get_settings().timezone
    order = case.betex_pay_order_id or case.illunise_order_id
    facts = [
        f"\U0001f4cc {b('Status')}: {esc(case.status.replace('_', ' ').lower())}",
        f"\U0001f4f1 Mobile: {code(case.mobile or '-')}",
        f"\U0001f4b0 Amount: {money(case.amount)}",
        f"\U0001f552 Payment: {esc(fmt_local(case.payment_time, tz, '%d %b %Y %H:%M'))}",
    ]
    extra = [
        f"\U0001f4ce Evidence: {esc(', '.join(types) or 'none yet')}",
        f"\U0001f4ac Betix replies: {len(events)}"
        + (f" \u00b7 \U0001f514 {case.followups_sent} follow-up(s) sent" if case.followups_sent else ""),
    ]
    if order != case_label(case):  # no order yet: say so where the order id would be
        facts.insert(2, f"\U0001f9fe Order: {code(order or 'not matched yet')}")
    return para(
        f"\U0001f5c2 {b('Case')}: {code(case_label(case))}",
        facts,
        extra,
        f"\u2757 {esc(case.failure_reason)}" if case.failure_reason else "",
    )
