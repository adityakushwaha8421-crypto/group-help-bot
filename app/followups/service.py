"""Follow-up scheduling and execution. Schedule is persisted in the followups table so it survives restarts.
Numbers 1..N are follow-up messages (FOLLOWUP_SCHEDULE_MINUTES offsets after the post); number 99 is the escalation check."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.state_machine import transition
from app.config import get_settings
from app.db.models import Case, CaseStatus, Followup
from app.db.repository import audit, cancel_followups, get_case_for_update, schedule_followup
from app.utils.logging import get_logger
from app.utils.timeutil import utcnow

log = get_logger("followups")
ESCALATION_NUMBER = 99
WAITING = {
    CaseStatus.POSTED_TO_BETIX.value,
    CaseStatus.WAITING_FOR_CONFIRMATION.value,
    CaseStatus.FOLLOWUP_1_SENT.value,
    CaseStatus.FOLLOWUP_2_SENT.value,
}


async def schedule_case_followups(session: AsyncSession, case: Case) -> list[Followup]:
    s = get_settings()
    base = case.betix_posted_at or utcnow()
    created: list[Followup] = []
    t = base
    for n, delay in enumerate(s.followup_delays, start=1):
        t = base + delay  # every offset counts from the POST, not from the previous follow-up
        row = await schedule_followup(session, case.case_id, n, t)
        if row:
            created.append(row)
    esc = t + s.escalation_delay
    row = await schedule_followup(session, case.case_id, ESCALATION_NUMBER, esc)
    if row:
        created.append(row)
    if created:
        await audit(
            session,
            "FOLLOWUPS_SCHEDULED",
            case_id=case.case_id,
            result=str(len(created)),
            details={"due": {r.number: r.due_at.isoformat() for r in created}},
        )
    return created


async def cancel_case_followups(session: AsyncSession, case: Case, reason: str) -> int:
    n = await cancel_followups(session, case.case_id)
    case.followup_cancelled = True
    if n:
        await audit(session, "FOLLOWUPS_CANCELLED", case_id=case.case_id, result=str(n), details={"reason": reason})
    return n


async def execute_followup(session: AsyncSession, fu: Followup, poster) -> str:
    """Run one due follow-up row. Returns what happened (for logs/tests)."""
    case = await get_case_for_update(session, fu.case_id)
    if case is None:
        fu.status = "cancelled"
        return "no_case"
    # Never follow up after confirmation / termination.
    if case.status not in WAITING or case.followup_cancelled or case.verified_at:
        fu.status = "cancelled"
        await audit(
            session,
            "FOLLOWUP_SKIPPED",
            case_id=case.case_id,
            result=str(fu.number),
            details={"status": case.status, "followup_cancelled": case.followup_cancelled},
        )
        return "skipped"
    if fu.number == ESCALATION_NUMBER:
        from app.cases.manager import escalate_case

        fu.status = "sent"
        fu.sent_at = utcnow()
        await escalate_case(session, case, f"No confirmation after {case.followups_sent} follow-up(s).")
        return "escalated"
    try:
        mid = await poster.send_followup(session, case, fu.number)
    except Exception as exc:  # noqa: BLE001
        # A send can fail for reasons that pass on their own (a Telegram timeout, a rate limit, a flaky
        # connection). Retry it on a later sweep and leave the rest of the chain alone; only give up - and
        # escalate - once it has failed FOLLOWUP_MAX_ATTEMPTS times.
        s = get_settings()
        fu.attempts = (fu.attempts or 0) + 1
        fu.error = str(exc)[:500]
        last = fu.attempts >= s.followup_max_attempts
        fu.status = "failed" if last else "scheduled"
        if not last:
            fu.due_at = utcnow() + timedelta(seconds=s.followup_retry_seconds)
        await audit(
            session,
            f"FOLLOWUP_{fu.number}_FAILED",
            case_id=case.case_id,
            result="error",
            details={"error": str(exc)[:200], "attempt": fu.attempts, "giving_up": last},
        )
        if not last:
            log.warning(
                "follow-up send failed; will retry",
                case_id=case.case_id,
                number=fu.number,
                attempt=fu.attempts,
                retry_in_seconds=s.followup_retry_seconds,
            )
            return "retry"
        from app.cases.manager import escalate_case

        await escalate_case(session, case, f"Telegram posting failed for follow-up #{fu.number}: {exc}")
        return "failed"
    fu.status = "sent"
    fu.sent_at = utcnow()
    fu.betix_message_id = mid
    case.followups_sent = max(case.followups_sent, fu.number)
    target = CaseStatus.FOLLOWUP_1_SENT if fu.number == 1 else CaseStatus.FOLLOWUP_2_SENT
    await transition(session, case, target, reason=f"follow-up #{fu.number} sent", strict=False)
    await audit(session, f"FOLLOWUP_{fu.number}_SENT", case_id=case.case_id, result="ok", details={"message_id": mid})
    return "sent"
