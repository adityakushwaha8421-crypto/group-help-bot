"""Repository helpers: thin, explicit query functions used by services and workers."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MONITORING_STATUSES,
    OPEN_STATUSES,
    AuditLog,
    BetixMessage,
    Case,
    CaseMessage,
    CaseStatus,
    CaseStatusHistory,
    Evidence,
    Followup,
    Notification,
    OrderCandidate,
    VerificationEvent,
)
from app.utils.logging import get_logger
from app.utils.timeutil import utcnow

log = get_logger("repo")


# ---------------- audit ----------------
async def audit(
    session: AsyncSession,
    action: str,
    *,
    case_id: str | None = None,
    actor: str = "system",
    result: str | None = None,
    confidence: float | None = None,
    source: str | None = None,
    details: dict | None = None,
) -> AuditLog:
    row = AuditLog(
        case_id=case_id,
        action=action,
        actor=actor,
        result=result,
        confidence=confidence,
        source=source,
        details=details,
    )
    session.add(row)
    await session.flush()
    log.info("audit", action=action, case_id=case_id, actor=actor, result=result, confidence=confidence, source=source)
    return row


# ---------------- cases ----------------
async def next_case_id(session: AsyncSession, now: datetime | None = None) -> str:
    now = now or utcnow()
    prefix = f"CASE-{now:%Y%m%d}-"
    res = await session.execute(
        select(Case.case_id).where(Case.case_id.like(prefix + "%")).order_by(Case.case_id.desc()).limit(1)
    )
    last = res.scalar_one_or_none()
    n = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{n:06d}"


async def get_case(session: AsyncSession, case_id: str) -> Case | None:
    res = await session.execute(select(Case).where(Case.case_id == case_id))
    return res.scalar_one_or_none()


async def find_case_by_order_id(session: AsyncSession, order_id: str) -> Case | None:
    """The case behind an Illunise order id (the id the operator sees as the case id once matched)."""
    oid = order_id.strip().upper()
    res = await session.execute(
        select(Case)
        .where((Case.betex_pay_order_id == oid) | (Case.illunise_order_id == oid))
        .order_by(Case.betix_root_message_id.is_(None), Case.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


# A case in one of these statuses has sent its order id to the Betix group, or is about to.
SENDING_STATUSES = (
    CaseStatus.READY_FOR_BETIX,
    CaseStatus.POSTED_TO_BETIX,
    CaseStatus.WAITING_FOR_CONFIRMATION,
    CaseStatus.FOLLOWUP_1_SENT,
    CaseStatus.FOLLOWUP_2_SENT,
    CaseStatus.VERIFIED,
)


async def find_case_that_sent_order(
    session: AsyncSession, order_id: str, *, exclude_case_id: str | None = None, posted_only: bool = False
) -> Case | None:
    """The earliest OTHER case that already sent this Illunise order id to the Betix group (its screenshot post
    exists), or - unless posted_only - is about to send it. One order id goes to the group once, ever."""
    from sqlalchemy import or_

    ids = {order_id.strip(), order_id.strip().upper()}
    sent = Case.betix_root_message_id.is_not(None)
    cond = sent if posted_only else or_(sent, Case.status.in_([s.value for s in SENDING_STATUSES]))
    stmt = select(Case).where(Case.betex_pay_order_id.in_(ids), cond)
    if exclude_case_id:
        stmt = stmt.where(Case.case_id != exclude_case_id)
    res = await session.execute(stmt.order_by(Case.id).limit(1))
    return res.scalar_one_or_none()


async def latest_candidate(session: AsyncSession, case_id: str, order_id: str) -> OrderCandidate | None:
    """The case's most recent stored candidate row for this order id."""
    res = await session.execute(
        select(OrderCandidate)
        .where(OrderCandidate.case_id == case_id, OrderCandidate.betex_order_id == order_id)
        .order_by(OrderCandidate.search_attempt.desc(), OrderCandidate.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def lock_order_id(session: AsyncSession, order_id: str) -> None:
    """Serialise 'check, then post' for one order id across concurrent jobs (Postgres transaction-level advisory
    lock, released when the posting transaction commits). No-op on SQLite (tests, single writer)."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        from sqlalchemy import text

        await session.execute(
            text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"betix-post:{order_id.strip().upper()}"}
        )


async def get_case_for_update(session: AsyncSession, case_id: str) -> Case | None:
    stmt = select(Case).where(Case.case_id == case_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def find_open_collecting_case(
    session: AsyncSession, chat_id: int, user_id: int, window_minutes: int, original_user_id: int | None = None
) -> Case | None:
    """The open case a new message belongs to.

    original_user_id given (a forwarded message): the open case of THAT customer in this chat.
    None (a plain message such as the admin typing the mobile): the newest open case in this chat."""
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    stmt = select(Case).where(
        Case.source_chat_id == chat_id,
        Case.source_user_id == user_id,
        Case.status == CaseStatus.WAITING_FOR_INPUT.value,
        Case.last_input_at >= cutoff,
    )
    if original_user_id is not None:
        stmt = stmt.where(Case.original_user_id == original_user_id)
    res = await session.execute(stmt.order_by(Case.id.desc()).limit(1))
    return res.scalar_one_or_none()


async def find_open_case_for_late_evidence(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    window_minutes: int,
    original_user_id: int | None = None,
    evidence_type: str | None = None,
) -> Case | None:
    """Where a late statement / video belongs: this submitter's newest recent case that can still take it.

    That is a case still being collected or in flight, or one already in the Betix group - a CONFIRMED case
    included, since the file is still posted as a reply to its screenshot. A closed case that never reached
    Betix (escalated or failed before posting) is never a target: nothing could be posted under it.

    When several qualify, the newest one still MISSING this kind of file wins over one that already has it.
    (Live 2026-09-13: a statement landed on the previous, escalated case that already had its own statement,
    while the case it was for - posted without one seconds earlier - went without.)"""
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    stmt = select(Case).where(
        Case.source_chat_id == chat_id,
        Case.source_user_id == user_id,
        Case.last_input_at >= cutoff,
        or_(Case.status.in_([s.value for s in OPEN_STATUSES]), Case.betix_root_message_id.is_not(None)),
    )
    if original_user_id is not None:
        stmt = stmt.where(Case.original_user_id == original_user_id)
    rows = list((await session.execute(stmt.order_by(Case.id.desc()).limit(5))).scalars())
    if not rows:
        return None
    if evidence_type:
        for c in rows:
            if not any(e.type == evidence_type for e in await list_evidence(session, c.case_id)):
                return c
    return rows[0]


async def find_evidence_by_unique_id(session: AsyncSession, case_id: str, file_unique_id: str) -> Evidence | None:
    res = await session.execute(
        select(Evidence).where(Evidence.case_id == case_id, Evidence.file_unique_id == file_unique_id).limit(1)
    )
    return res.scalar_one_or_none()


async def latest_case_for_user(session: AsyncSession, chat_id: int, user_id: int) -> Case | None:
    res = await session.execute(
        select(Case)
        .where(Case.source_chat_id == chat_id, Case.source_user_id == user_id)
        .order_by(Case.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def list_open_cases(session: AsyncSession) -> list[Case]:
    res = await session.execute(select(Case).where(Case.status.in_([s.value for s in OPEN_STATUSES])).order_by(Case.id))
    return list(res.scalars())


async def list_monitoring_cases(session: AsyncSession) -> list[Case]:
    res = await session.execute(
        select(Case).where(Case.status.in_([s.value for s in MONITORING_STATUSES])).order_by(Case.id.desc())
    )
    return list(res.scalars())


async def find_case_by_order_ids(
    session: AsyncSession,
    betex_order_id: str | None = None,
    plat_order_no: str | None = None,
    registration: str | None = None,
    utr: str | None = None,
) -> list[Case]:
    conds = []
    if betex_order_id:
        conds.append(Case.betex_pay_order_id == betex_order_id)
    if plat_order_no:
        conds.append(Case.betix_plat_order_no == plat_order_no)
    if registration:
        conds.append(Case.registration_number == registration)
    if utr:
        conds.append(Case.utr == utr)
    if not conds:
        return []
    from sqlalchemy import or_

    res = await session.execute(
        select(Case)
        .where(or_(*conds), Case.status.in_([s.value for s in MONITORING_STATUSES]))
        .order_by(Case.id.desc())
    )
    return list(res.scalars())


async def record_status_change(
    session: AsyncSession, case: Case, to_status: CaseStatus, reason: str | None, actor: str
) -> None:
    session.add(
        CaseStatusHistory(
            case_id=case.case_id, from_status=case.status, to_status=to_status.value, reason=reason, actor=actor
        )
    )
    case.status = to_status.value
    await session.flush()


async def status_history(session: AsyncSession, case_id: str) -> list[CaseStatusHistory]:
    res = await session.execute(
        select(CaseStatusHistory).where(CaseStatusHistory.case_id == case_id).order_by(CaseStatusHistory.id)
    )
    return list(res.scalars())


# ---------------- messages / evidence ----------------
async def add_case_message(session: AsyncSession, **kw) -> CaseMessage | None:
    """Insert; return None if this (chat,message) was already recorded (idempotent)."""
    res = await session.execute(
        select(CaseMessage).where(CaseMessage.chat_id == kw["chat_id"], CaseMessage.message_id == kw["message_id"])
    )
    if res.scalar_one_or_none():
        return None
    row = CaseMessage(**kw)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None
    return row


async def add_evidence(session: AsyncSession, **kw) -> Evidence | None:
    res = await session.execute(
        select(Evidence).where(
            Evidence.telegram_chat_id == kw["telegram_chat_id"],
            Evidence.telegram_message_id == kw["telegram_message_id"],
        )
    )
    if res.scalar_one_or_none():
        return None
    row = Evidence(**kw)
    session.add(row)
    await session.flush()
    return row


async def list_evidence(session: AsyncSession, case_id: str) -> list[Evidence]:
    res = await session.execute(select(Evidence).where(Evidence.case_id == case_id).order_by(Evidence.id))
    return list(res.scalars())


async def list_case_messages(session: AsyncSession, case_id: str) -> list[CaseMessage]:
    res = await session.execute(select(CaseMessage).where(CaseMessage.case_id == case_id).order_by(CaseMessage.id))
    return list(res.scalars())


async def list_candidates(session: AsyncSession, case_id: str):
    from app.db.models import OrderCandidate

    res = await session.execute(
        select(OrderCandidate).where(OrderCandidate.case_id == case_id).order_by(OrderCandidate.id)
    )
    return list(res.scalars())


# ---------------- betix messages ----------------
async def add_betix_message(session: AsyncSession, **kw) -> BetixMessage | None:
    res = await session.execute(
        select(BetixMessage).where(BetixMessage.chat_id == kw["chat_id"], BetixMessage.message_id == kw["message_id"])
    )
    existing = res.scalar_one_or_none()
    if existing:
        return None
    row = BetixMessage(**kw)
    session.add(row)
    await session.flush()
    return row


async def get_betix_message(session: AsyncSession, chat_id: int, message_id: int) -> BetixMessage | None:
    res = await session.execute(
        select(BetixMessage).where(BetixMessage.chat_id == chat_id, BetixMessage.message_id == message_id)
    )
    return res.scalar_one_or_none()


async def list_case_betix_messages(session: AsyncSession, case_id: str) -> list[BetixMessage]:
    res = await session.execute(select(BetixMessage).where(BetixMessage.case_id == case_id).order_by(BetixMessage.id))
    return list(res.scalars())


async def case_has_out_kind(session: AsyncSession, case_id: str, kind: str) -> BetixMessage | None:
    res = await session.execute(
        select(BetixMessage)
        .where(BetixMessage.case_id == case_id, BetixMessage.direction == "out", BetixMessage.kind == kind)
        .limit(1)
    )
    return res.scalar_one_or_none()


# ---------------- verification events ----------------
async def add_verification_event(session: AsyncSession, **kw) -> VerificationEvent | None:
    res = await session.execute(
        select(VerificationEvent).where(
            VerificationEvent.case_id == kw["case_id"], VerificationEvent.dedupe_key == kw["dedupe_key"]
        )
    )
    if res.scalar_one_or_none():
        return None
    row = VerificationEvent(**kw)
    session.add(row)
    await session.flush()
    return row


async def case_evidence_requested(session: AsyncSession, case_id: str) -> bool:
    """True once the Betix system bot or a group member asked for more evidence on this case."""
    res = await session.execute(
        select(VerificationEvent.id)
        .where(VerificationEvent.case_id == case_id, VerificationEvent.event_type.like("%NEED_MORE_EVIDENCE"))
        .limit(1)
    )
    return res.scalar_one_or_none() is not None


async def list_verification_events(session: AsyncSession, case_id: str) -> list[VerificationEvent]:
    res = await session.execute(
        select(VerificationEvent).where(VerificationEvent.case_id == case_id).order_by(VerificationEvent.id)
    )
    return list(res.scalars())


# ---------------- followups ----------------
async def schedule_followup(session: AsyncSession, case_id: str, number: int, due_at: datetime) -> Followup | None:
    res = await session.execute(select(Followup).where(Followup.case_id == case_id, Followup.number == number))
    if res.scalar_one_or_none():
        return None
    row = Followup(case_id=case_id, number=number, due_at=due_at, status="scheduled")
    session.add(row)
    await session.flush()
    return row


async def cancel_followups(session: AsyncSession, case_id: str) -> int:
    res = await session.execute(
        update(Followup).where(Followup.case_id == case_id, Followup.status == "scheduled").values(status="cancelled")
    )
    return res.rowcount or 0


async def due_followups(session: AsyncSession, now: datetime | None = None) -> list[Followup]:
    now = now or utcnow()
    res = await session.execute(
        select(Followup).where(Followup.status == "scheduled", Followup.due_at <= now).order_by(Followup.due_at)
    )
    return list(res.scalars())


async def list_followups(session: AsyncSession, case_id: str) -> list[Followup]:
    res = await session.execute(select(Followup).where(Followup.case_id == case_id).order_by(Followup.number))
    return list(res.scalars())


# ---------------- notifications ----------------
async def reserve_notification(
    session: AsyncSession, *, idempotency_key: str, kind: str, text: str, case_id: str | None, chat_id: str | None
) -> Notification | None:
    res = await session.execute(select(Notification).where(Notification.idempotency_key == idempotency_key))
    if res.scalar_one_or_none():
        return None
    row = Notification(idempotency_key=idempotency_key, kind=kind, text=text, case_id=case_id, chat_id=chat_id)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        return None
    return row
