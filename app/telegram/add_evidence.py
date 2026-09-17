"""`/add <mobile | registration | ORDER-ID>`: attach evidence to a case that is ALREADY in the Betix group.

A case that was force-sent without its bank statement or payment video can be completed later. `/add` names the
case; every file sent afterwards is attached to THAT case (never a new one) and posted to the Betix group as a
REPLY to the case's original payment-screenshot message. Nothing is ever sent standalone."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.extractor import normalize_mobile
from app.cases.correlation import IncomingInput
from app.db.models import Case, Evidence, EvidenceType
from app.db.repository import add_evidence, audit, list_evidence
from app.evidence.manager import classify_media
from app.utils.logging import get_logger
from app.utils.timeutil import utcnow

log = get_logger("add_evidence")


@dataclass
class AddSession:
    chat_id: int
    user_id: int
    case_id: str
    query: str
    customer_id: int | None = None  # the case's original customer: a forward from anyone else is not for it
    added: list[str] = field(default_factory=list)
    started_at: object = field(default_factory=utcnow)


_sessions: dict[int, AddSession] = {}


def active(chat_id: int) -> AddSession | None:
    """The open /add for this chat - none once it has expired. (Live 2026-09-15: an /add left open for an hour
    swallowed the next customer's screenshot, statement and video and posted them under the wrong case.)"""
    sess = _sessions.get(chat_id)
    if sess is None:
        return None
    from app.config import get_settings

    if (utcnow() - sess.started_at).total_seconds() > get_settings().add_session_seconds:
        log.info("/add expired", case_id=sess.case_id)
        _sessions.pop(chat_id, None)
        return None
    return sess


def start(chat_id: int, user_id: int, case: Case, query: str) -> AddSession:
    sess = AddSession(chat_id, user_id, case.case_id, query, customer_id=case.original_user_id)
    _sessions[chat_id] = sess
    return sess


def belongs_elsewhere(sess: AddSession, inp: IncomingInput) -> bool:
    """A forwarded file whose ORIGINAL sender is not the /add case's customer is another customer's evidence:
    it must take the normal path, never be added to this case."""
    fwd = inp.forward.user_id if inp.forward is not None else None
    return bool(sess.customer_id and fwd and fwd != sess.customer_id and fwd != inp.user_id)


def complete(case: Case, evidence: list[Evidence]) -> bool:
    """Nothing left to add: screenshot, statement and video are all on the case."""
    types = {e.type for e in evidence}
    return {
        EvidenceType.payment_screenshot.value,
        EvidenceType.bank_statement.value,
        EvidenceType.payment_video.value,
    } <= types


def finish(chat_id: int) -> AddSession | None:
    return _sessions.pop(chat_id, None)


async def find_case(session: AsyncSession, query: str) -> Case | None:
    """The case behind a mobile number, a registration number or an Illunise order id: the newest one, preferring
    a case whose evidence is already in the Betix group."""
    q = (query or "").strip().lstrip("#").strip()
    if not q:
        return None
    up = q.upper()
    mob = normalize_mobile(q)
    conds = [  # an order id and a registered mobile number are both accepted, in any case/spacing
        Case.case_id == up,
        Case.betex_pay_order_id == up,
        Case.illunise_order_id == up,
        func.upper(Case.registration_number) == up,
        func.upper(Case.betix_plat_order_no) == up,
    ]
    if mob:
        conds.append(Case.mobile == mob)
    res = await session.execute(
        select(Case).where(or_(*conds)).order_by(Case.betix_root_message_id.is_(None), Case.id.desc()).limit(1)
    )
    return res.scalar_one_or_none()


async def attach(session: AsyncSession, case: Case, inp: IncomingInput) -> tuple[Evidence | None, str]:
    """Attach one file to THIS case (no correlation, no new case). Returns (evidence, note)."""
    etype = classify_media(inp.kind, inp.mime_type, inp.filename, inp.text)
    if etype == EvidenceType.other.value:
        return None, "that file is not a screenshot, statement or video"
    existing = await list_evidence(session, case.case_id)
    if inp.file_unique_id and any(e.file_unique_id == inp.file_unique_id for e in existing):
        return None, "already on this case"
    ev = await add_evidence(
        session,
        case_id=case.case_id,
        telegram_chat_id=inp.chat_id,
        telegram_message_id=inp.message_id,
        file_id=inp.file_id,
        file_unique_id=inp.file_unique_id,
        type=etype,
        filename=inp.filename,
        mime_type=inp.mime_type,
        size=inp.size,
    )
    if ev is None:
        return None, "already on this case"
    case.last_input_at = utcnow()
    await audit(
        session,
        "EVIDENCE_ADDED",
        case_id=case.case_id,
        actor="telegram",
        result=etype,
        details={"message_id": inp.message_id, "filename": inp.filename, "via": "/add"},
    )
    log.info("evidence added to an existing case", case_id=case.case_id, type=etype)
    return ev, etype.replace("_", " ")
