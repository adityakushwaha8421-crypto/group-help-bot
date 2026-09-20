"""Case status machine. Every transition is validated and written to case_status_history + audit_logs."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Case, CaseStatus
from app.db.repository import audit, record_status_change
from app.utils.timeutil import utcnow

S = CaseStatus
ALLOWED: dict[CaseStatus, set[CaseStatus]] = {
    # A withdrawal skips the reading: WAITING -> SEARCHING (the payout lookup) -> READY / ALREADY_SENT / ESCALATED
    S.WAITING_FOR_INPUT: {S.ANALYZING_EVIDENCE, S.SEARCHING_ORDER, S.READY_FOR_BETIX, S.ALREADY_SENT, S.FAILED},
    S.ANALYZING_EVIDENCE: {S.SEARCHING_ORDER, S.WAITING_FOR_INPUT, S.FAILED, S.ESCALATED},
    S.SEARCHING_ORDER: {
        S.ORDER_MATCH_FOUND,
        S.ORDER_MATCH_AMBIGUOUS,
        S.CHECKING_ORDER_UPI,
        S.FAILED,
        S.ESCALATED,
        S.WAITING_FOR_INPUT,
        S.READY_FOR_BETIX,  # withdrawal: the statement's account matches the payout
        S.ALREADY_SENT,
    },
    S.CHECKING_ORDER_UPI: {S.ORDER_MATCH_FOUND, S.ORDER_MATCH_AMBIGUOUS, S.ESCALATED, S.FAILED, S.WAITING_FOR_INPUT},
    S.ORDER_MATCH_FOUND: {S.READY_FOR_BETIX, S.FAILED, S.ESCALATED, S.ALREADY_SUCCESS, S.ALREADY_SENT},
    S.ORDER_MATCH_AMBIGUOUS: {
        S.ORDER_MATCH_FOUND,
        S.SEARCHING_ORDER,
        S.ESCALATED,
        S.FAILED,
        S.READY_FOR_BETIX,
        S.WAITING_FOR_INPUT,
        S.VERIFIED,
    },
    S.READY_FOR_BETIX: {S.POSTED_TO_BETIX, S.FAILED, S.ESCALATED, S.ALREADY_SENT},
    S.POSTED_TO_BETIX: {S.WAITING_FOR_CONFIRMATION, S.VERIFIED, S.ESCALATED, S.FAILED},
    S.WAITING_FOR_CONFIRMATION: {S.FOLLOWUP_1_SENT, S.VERIFIED, S.ESCALATED, S.FAILED},
    S.FOLLOWUP_1_SENT: {S.FOLLOWUP_2_SENT, S.VERIFIED, S.ESCALATED, S.FAILED},
    S.FOLLOWUP_2_SENT: {S.VERIFIED, S.ESCALATED, S.FAILED},
    S.ESCALATED: {
        S.VERIFIED,
        S.FAILED,
        S.SEARCHING_ORDER,
        S.READY_FOR_BETIX,
        S.WAITING_FOR_CONFIRMATION,
        S.WAITING_FOR_INPUT,
    },
    S.VERIFIED: set(),
    S.ALREADY_SUCCESS: set(),
    S.ALREADY_SENT: {S.READY_FOR_BETIX},  # the operator's FORCE SEND (/push) - never automatic
    S.FAILED: {S.SEARCHING_ORDER, S.WAITING_FOR_INPUT, S.READY_FOR_BETIX},
}


class InvalidTransition(Exception):
    pass


def can_transition(current: str | CaseStatus, new: CaseStatus) -> bool:
    cur = CaseStatus(current)
    return new in ALLOWED.get(cur, set())


async def transition(
    session: AsyncSession,
    case: Case,
    new: CaseStatus,
    *,
    reason: str | None = None,
    actor: str = "system",
    strict: bool = True,
) -> bool:
    """Move a case to a new status. Returns False (no-op) when already in that status."""
    if case.status == new.value:
        return False
    if not can_transition(case.status, new):
        if strict:
            raise InvalidTransition(f"{case.case_id}: {case.status} -> {new.value} not allowed")
        return False
    old = case.status
    await record_status_change(session, case, new, reason, actor)
    if new == S.VERIFIED:
        case.verified_at = case.verified_at or utcnow()
    await audit(
        session,
        "STATUS_CHANGED",
        case_id=case.case_id,
        actor=actor,
        result=new.value,
        details={"from": old, "to": new.value, "reason": reason},
    )
    from app.telegram.progress import push  # live card in the operator chat (best-effort)

    await push(session, case)
    return True
