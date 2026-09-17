"""Correlation logic.

1. Input side: group separate Telegram messages (screenshot / registration text / statement / video) from the
   same submitter into ONE case using an open 'collecting' case within CASE_COLLECTION_WINDOW_MINUTES.
2. Betix side: link a group message to a case via reply chain, order ids, plat order no, registration, UTR,
   and (low confidence) proximity."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.extractor import Extraction, Field, extract_from_text
from app.config import get_settings
from app.db.models import KIND_WITHDRAWAL, Case, CaseStatus, Evidence, EvidenceType
from app.db.repository import (
    add_case_message,
    add_evidence,
    audit,
    find_case_by_order_ids,
    find_evidence_by_unique_id,
    find_open_case_for_late_evidence,
    find_open_collecting_case,
    get_betix_message,
    list_evidence,
    list_monitoring_cases,
    next_case_id,
)
from app.evidence.manager import classify_media
from app.evidence.pdf import pdf_is_encrypted
from app.utils.logging import get_logger
from app.utils.timeutil import utcnow

log = get_logger("correlation")


@dataclass
class ForwardOrigin:
    """Who ORIGINALLY sent a forwarded message. Telegram exposes the user for normal forwards, only a
    display name for privacy-restricted forwards, and chat+message ids for channel/chat forwards."""

    user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    chat_id: int | None = None
    message_id: int | None = None
    forwarded_at: datetime | None = None
    hidden_name: str | None = None  # privacy-restricted forward: name only


@dataclass
class IncomingInput:
    chat_id: int
    message_id: int
    user_id: int
    username: str | None
    kind: str  # text|photo|document|video|other
    text: str | None = None  # message text or media caption
    file_id: str | None = None
    file_unique_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    size: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    forward: ForwardOrigin | None = None  # set when the message was forwarded to the bot
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def customer(self) -> ForwardOrigin:
        """The customer this evidence belongs to: the forwarded sender, else the sender themselves."""
        if self.forward is not None:
            return self.forward
        return ForwardOrigin(
            user_id=self.user_id,
            username=self.username,
            first_name=self.first_name,
            last_name=self.last_name,
            chat_id=self.chat_id,
            message_id=self.message_id,
        )


@dataclass
class AttachResult:
    case: Case
    created: bool
    duplicate: bool
    evidence: Evidence | None
    extraction: Extraction
    new_mobile: str | None


def merge_case_extraction(case: Case, ex: Extraction) -> None:
    current = Extraction.from_dict(case.extraction)
    merged = current.merge(ex)
    case.extraction = merged.as_dict()
    if merged.mobile.value and not case.mobile:
        case.mobile = merged.mobile.value
    if merged.registration_number.value and not case.registration_number:
        case.registration_number = merged.registration_number.value
    if merged.statement_password.value:
        case.statement_password = merged.statement_password.value
    if merged.withdrawal_id.value and not case.withdrawal_id:
        case.withdrawal_id = merged.withdrawal_id.value
        if not case.mobile:  # a withdrawal case: WD id + bank statement, nothing else is asked for
            case.kind = KIND_WITHDRAWAL
    if merged.utr.value and merged.utr.confidence >= 0.7:
        case.utr = merged.utr.value
    if merged.upi_id.value:
        case.upi_id = merged.upi_id.value
    if merged.payer_name.value:
        case.payer_name = merged.payer_name.value
    if merged.amount.value is not None and merged.amount.confidence >= 0.6:
        case.amount = merged.amount.value
    if merged.payment_time.value and merged.payment_time.confidence >= 0.6:
        case.payment_time = merged.payment_time.value


async def create_case(session: AsyncSession, inp: IncomingInput, actor: str = "telegram") -> Case:
    cust = inp.customer
    case = Case(
        case_id=await next_case_id(session),
        status=CaseStatus.WAITING_FOR_INPUT.value,
        source_chat_id=inp.chat_id,
        source_user_id=inp.user_id,
        source_username=inp.username,
        original_user_id=cust.user_id,
        original_username=cust.username,
        original_first_name=cust.first_name or cust.hidden_name,
        original_last_name=cust.last_name,
        original_chat_id=cust.chat_id,
        original_message_id=cust.message_id,
        evidence_forwarded=inp.forward is not None,
        last_input_at=utcnow(),
        extraction=Extraction().as_dict(),
    )
    session.add(case)
    await session.flush()
    await audit(
        session,
        "CASE_CREATED",
        case_id=case.case_id,
        actor=actor,
        result="ok",
        details={
            "submitter_chat_id": inp.chat_id,
            "submitter_user_id": inp.user_id,
            "forwarded": inp.forward is not None,
            "original_user_id": cust.user_id,
            "original_username": cust.username,
        },
    )
    return case


def _is_self_forward(case_or_submitter_id: int | None, inp: IncomingInput) -> bool:
    """A forward whose original sender is the submitter themself carries no customer identity."""
    return inp.forward is not None and inp.forward.user_id is not None and inp.forward.user_id == case_or_submitter_id


def _set_original(case: Case, inp: IncomingInput) -> None:
    """A case created from a plain message (or from the submitter's own forward) learns its customer from the
    first forwarded message that names a real customer."""
    if inp.forward is None or _is_self_forward(inp.user_id, inp):
        return
    owned_by_submitter = case.original_user_id is None or case.original_user_id == case.source_user_id
    if case.evidence_forwarded and not owned_by_submitter:
        return
    f = inp.forward
    case.original_user_id, case.original_username = f.user_id, f.username
    case.original_first_name, case.original_last_name = f.first_name or f.hidden_name, f.last_name
    case.original_chat_id, case.original_message_id = f.chat_id, f.message_id
    case.evidence_forwarded = True


async def attach_message(session: AsyncSession, inp: IncomingInput, *, force_new: bool = False) -> AttachResult:
    """Attach a Telegram message to the open case for this submitter (or create a new one)."""
    s = get_settings()
    text = (inp.text or "").strip()
    ex = extract_from_text(text, registration_pattern=s.registration_pattern, betex_pattern=s.betex_order_id_pattern)
    reg = ex.registration_number.value
    mob = ex.mobile.value

    # OWNERSHIP. A forwarded message belongs to the customer who originally sent it: it joins that customer's
    # open case, or starts one. A plain message (the admin typing the mobile number) joins the newest open case
    # in the chat. If nothing is open, a plain message starts a case owned by its sender (direct submission).
    fwd_user = inp.forward.user_id if inp.forward is not None else None
    if _is_self_forward(inp.user_id, inp):
        fwd_user = None  # the submitter forwarding their own message: no customer identity
    case = None
    if force_new:
        pass
    elif inp.forward is None or _is_self_forward(inp.user_id, inp):
        case = await find_open_collecting_case(session, inp.chat_id, inp.user_id, s.case_collection_window_minutes)
    elif fwd_user is not None:
        case = await find_open_collecting_case(
            session, inp.chat_id, inp.user_id, s.case_collection_window_minutes, original_user_id=fwd_user
        )
    else:
        # Privacy-restricted forward (display name only): it may join the newest open case only when that case
        # is about the same name (or has no customer identity yet). A different name is a different customer.
        candidate = await find_open_collecting_case(session, inp.chat_id, inp.user_id, s.case_collection_window_minutes)
        hidden = (inp.forward.hidden_name or "").strip().lower()
        if candidate is not None:
            known = (candidate.original_first_name or "").strip().lower()
            if candidate.original_user_id is None and (not known or known == hidden):
                case = candidate
    if case is None and not force_new and inp.forward is not None and inp.file_id:
        # THE BATCH RULE. The operator forwards a customer's messages together, and those forwards may carry
        # different original senders (relayed by different people, or by the operator themself). Anything that
        # lands within BATCH_JOIN_SECONDS of the collecting case's last input belongs to that case. The
        # second-screenshot and second-mobile rules below still split genuinely different payments.
        candidate = await find_open_collecting_case(session, inp.chat_id, inp.user_id, s.case_collection_window_minutes)
        if (
            candidate is not None
            and candidate.last_input_at
            and (utcnow() - candidate.last_input_at).total_seconds() <= s.batch_join_seconds
        ):
            log.info("forward with a different origin joins the batch", case_id=candidate.case_id)
            case = candidate
    if case is None and not force_new and inp.file_id and inp.kind in ("document", "video", "video_note"):
        # A statement / video arriving after the case was posted to Betix belongs to that case (late evidence),
        # not to a brand-new one. Screenshots and mobile numbers always start fresh once a case is posted.
        case = await find_open_case_for_late_evidence(
            session,
            inp.chat_id,
            inp.user_id,
            s.case_collection_window_minutes,
            original_user_id=fwd_user,
            evidence_type=classify_media(inp.kind, inp.mime_type, inp.filename),
        )
    created = False
    # A NEW payment screenshot while the collecting case already has one => another payment, another case
    # (the same file sent twice is caught by the duplicate check below and stays where it is).
    if (
        case
        and inp.file_id
        and classify_media(inp.kind, inp.mime_type, inp.filename) == EvidenceType.payment_screenshot.value
        and not (inp.file_unique_id and await find_evidence_by_unique_id(session, case.case_id, inp.file_unique_id))
        and any(e.type == EvidenceType.payment_screenshot.value for e in await list_evidence(session, case.case_id))
    ):
        from app.cases.manager import AI_UNAVAILABLE, UTR_NOT_FOUND

        reason_text = case.failure_reason or ""
        if reason_text == UTR_NOT_FOUND or reason_text.startswith(AI_UNAVAILABLE):
            # The case is waiting for a READABLE screenshot (or for the reader to come back): this one replaces
            # the earlier one, same case - the operator is not opening a second submission.
            log.info("clearer screenshot for a case with no visible UTR; same case", case_id=case.case_id)
            case.failure_reason = None
        else:
            log.info("second screenshot while collecting; starting new case", case_id=case.case_id)
            case = None
    # A different mobile number while a case already has one => this is a new case.
    if case and mob and case.mobile and mob != case.mobile:
        log.info("new mobile while collecting; starting new case", old=case.mobile[-4:], new=mob[-4:])
        case = None
    elif case and mob and not inp.file_id and case.mobile == mob and case.last_input_at:
        # The SAME number typed again. Inside the batch window it is simply a repeat of the number for the case
        # being collected. Later than that, the case already has this customer's number AND a screenshot, so this
        # is the operator opening the NEXT submission: start a case, so the evidence that follows joins THIS
        # number instead of being added to the older case. (Live 2026-09-12: the number was swallowed by an
        # 8-minute-old case and the new case kept asking for a mobile.)
        quiet = (utcnow() - case.last_input_at).total_seconds() > s.batch_join_seconds
        has_shot = any(
            e.type == EvidenceType.payment_screenshot.value for e in await list_evidence(session, case.case_id)
        )
        if quiet and has_shot:
            log.info("mobile typed again for an older complete case; starting new case", case_id=case.case_id)
            case = None
    is_password = False
    if (
        not force_new
        and not inp.file_id
        and not (mob or ex.utr.value or ex.betex_order_id.value or ex.withdrawal_id.value)
    ):
        # A text that carries no mobile / UTR / order id is either the STATEMENT PASSWORD ("Password:- 1234",
        # or a bare token such as "lata2812") for a case whose PDF password is still unknown - the collecting
        # case, or the newest open case already in Betix - or plain chatter ("ok", "please check"), which never
        # opens a case and never joins one. (The registration regex accepts almost any token, so it does not
        # count as evidence on its own here.)
        candidate = case or await find_open_case_for_late_evidence(
            session, inp.chat_id, inp.user_id, s.case_collection_window_minutes
        )
        awaits = candidate is not None and statement_awaits_password(
            candidate, await list_evidence(session, candidate.case_id)
        )
        if awaits and not ex.statement_password.value and not looks_like_bare_password(text):
            # The wording is not one the rules recognise ("wo jo bheja tha na, 4321 daal dena"). The case is
            # waiting for a password, so let the model read the sentence before the message is thrown away.
            guess = await ai_password(text)
            if guess:
                ex.statement_password = Field(guess, 0.8, "ai_text")
        if awaits and (ex.statement_password.value or looks_like_bare_password(text)):
            case, is_password = candidate, True
        elif not reg:
            log.info("text with no evidence; ignored", chat_id=inp.chat_id, open_case=case.case_id if case else None)
            return AttachResult(case, False, True, None, ex, None)
    if case is None:
        case = await create_case(session, inp)
        created = True
    else:
        _set_original(case, inp)

    # DUPLICATE FORWARD: the same file forwarded twice arrives with a new message id but the same
    # file_unique_id. Record nothing twice.
    if inp.file_unique_id and await find_evidence_by_unique_id(session, case.case_id, inp.file_unique_id):
        await audit(
            session,
            "DUPLICATE_EVIDENCE_IGNORED",
            case_id=case.case_id,
            actor="telegram",
            details={"message_id": inp.message_id, "file_unique_id": inp.file_unique_id},
        )
        return AttachResult(case, created, True, None, ex, None)

    kind = inp.kind
    msg = await add_case_message(
        session,
        case_id=case.case_id,
        chat_id=inp.chat_id,
        message_id=inp.message_id,
        user_id=inp.user_id,
        kind=kind,
        text=text or None,
        raw={**(inp.raw or {}), "forwarded": inp.forward is not None, "original_user_id": inp.customer.user_id},
    )
    if msg is None:
        return AttachResult(case, created, True, None, ex, None)

    evidence = None
    if inp.file_id:
        etype = classify_media(kind, inp.mime_type, inp.filename, text)
        evidence = await add_evidence(
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
        await audit(
            session,
            "EVIDENCE_RECEIVED",
            case_id=case.case_id,
            actor="telegram",
            result=etype,
            details={"message_id": inp.message_id, "filename": inp.filename, "size": inp.size},
        )
    if is_password:
        # The token IS the password - not a registration number (registration is never required anyway).
        ex.registration_number = Field()
        reg = None
        if not ex.statement_password.value:
            ex.statement_password = Field(text.strip(), 0.9, "telegram_text")
        await audit(session, "STATEMENT_PASSWORD_RECEIVED", case_id=case.case_id, actor="telegram", result="ok")
    merge_case_extraction(case, ex)
    case.last_input_at = utcnow()
    if mob:
        await audit(
            session,
            "MOBILE_RECEIVED",
            case_id=case.case_id,
            actor="telegram",
            result=mob[:2] + "******" + mob[-2:],
            confidence=ex.mobile.confidence,
            source="telegram_text",
        )
    if reg:
        await audit(
            session,
            "REGISTRATION_RECEIVED",
            case_id=case.case_id,
            actor="telegram",
            result=reg,
            confidence=ex.registration_number.confidence,
            source="telegram_text",
        )
    return AttachResult(case, created, False, evidence, ex, mob)


def statement_awaits_password(case: Case, evidence: list[Evidence]) -> bool:
    """The operator may still send the statement password: there is a statement, no password yet, and the PDF is
    not known to be open (encrypted, or not downloaded yet so we cannot tell)."""
    if case.statement_password:
        return False
    for e in evidence:
        if e.type != EvidenceType.bank_statement.value:
            continue
        if e.local_path and Path(e.local_path).exists():
            if pdf_is_encrypted(e.local_path):
                return True
            continue
        return True
    return False


def statement_needs_password(case: Case, evidence: list[Evidence]) -> bool:
    """A downloaded, password-protected statement PDF with no password on the case yet."""
    if case.statement_password:
        return False
    return any(
        e.type == EvidenceType.bank_statement.value and e.local_path and pdf_is_encrypted(e.local_path)
        for e in evidence
    )


# Items a FORCE SEND may go without (the screenshot and the mobile are always required).
SOFT_ITEMS = ("bank statement", "payment video", "statement password")


def hard_missing(missing: list[str]) -> list[str]:
    return [m for m in missing if m not in SOFT_ITEMS]


def missing_items(case: Case, evidence: list[Evidence], require_all: bool | None = None) -> list[str]:
    """What is still needed before the case can be processed. REQUIRED (REQUIRE_ALL_EVIDENCE, default):
    payment screenshot, customer MOBILE, bank statement, payment video (+ the statement password when the PDF
    is protected). A registration number is never required."""
    s = get_settings()
    require_all = s.require_all_evidence if require_all is None else require_all
    types = {e.type for e in evidence}
    missing = []
    if case.kind == KIND_WITHDRAWAL:
        # WITHDRAWAL: the withdrawal id and the bank statement (+ its password when protected). No screenshot,
        # mobile or video is ever asked for, and nothing is force-sent without the statement.
        if not case.withdrawal_id:
            missing.append("withdrawal id")
        if EvidenceType.bank_statement.value not in types:
            missing.append("bank statement")
        elif statement_needs_password(case, evidence):
            missing.append("statement password")
        return missing
    if EvidenceType.payment_screenshot.value not in types:
        missing.append("payment screenshot")
    if not case.mobile:
        missing.append("mobile number")
    if require_all and EvidenceType.bank_statement.value not in types:
        missing.append("bank statement")
    elif EvidenceType.bank_statement.value in types and statement_needs_password(case, evidence):
        missing.append("statement password")
    if require_all and EvidenceType.payment_video.value not in types:
        missing.append("payment video")
    return missing


PASSWORD_STOPWORDS = {
    "ok",
    "okay",
    "done",
    "yes",
    "no",
    "hi",
    "hii",
    "hello",
    "thanks",
    "thank",
    "sir",
    "please",
    "check",
    "checked",
    "sent",
    "paid",
    "noted",
    "sure",
    "bro",
    "wait",
    "update",
    "pending",
}
PASSWORD_TOKEN = re.compile(r"^[A-Za-z0-9@#$_\-]{3,40}$")


def looks_like_bare_password(text: str) -> bool:
    t = (text or "").strip()
    return bool(PASSWORD_TOKEN.match(t)) and t.lower() not in PASSWORD_STOPWORDS


async def ai_password(text: str) -> str | None:
    """Last resort for wording the rules do not cover: ask the model what password this message states.

    Only ever called for a case that IS waiting for a statement password. A failure is not an error: the
    message simply carries no password. The model may answer null, and often should."""
    t = (text or "").strip()
    if not (3 <= len(t) <= 300) or not re.search(r"[A-Za-z0-9]{3,}", t):
        return None
    try:
        from app.ai.analyzer import get_analyzer

        field = await get_analyzer().read_password(t)
    except Exception as exc:  # noqa: BLE001  (AI unavailable, bad JSON, network)
        log.info("ai password read failed; ignored", error=str(exc)[:120])
        return None
    value = (field or {}).get("value")
    if not value or (field or {}).get("confidence", 0) < 0.6:
        return None
    value = str(value).strip()
    return value if looks_like_bare_password(value) else None


# ---------------------------------------------------------------- Betix side


@dataclass
class Correlation:
    case_id: str | None
    confidence: float
    method: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"case_id": self.case_id, "confidence": self.confidence, "method": self.method, "details": self.details}


async def correlate_betix_message(
    session: AsyncSession,
    *,
    chat_id: int,
    reply_to_message_id: int | None,
    order_ids: list[str],
    plat_order_nos: list[str],
    utrs: list[str],
    text: str,
    sender_authority: str,
    sent_at,
) -> Correlation:
    s = get_settings()
    # 1. reply chain -> our message or an already-correlated inbound message
    if reply_to_message_id:
        parent = await get_betix_message(session, chat_id, reply_to_message_id)
        if parent and parent.case_id:
            return Correlation(parent.case_id, 0.98, "reply_chain", {"parent_message_id": reply_to_message_id})
    # 2. explicit ids
    for oid in order_ids:
        cases = await find_case_by_order_ids(session, betex_order_id=oid)
        if cases:
            return Correlation(cases[0].case_id, 0.97, "betex_order_id", {"order_id": oid})
    for pno in plat_order_nos:
        cases = await find_case_by_order_ids(session, plat_order_no=pno)
        if cases:
            return Correlation(cases[0].case_id, 0.95, "plat_order_no", {"plat_order_no": pno})
    for u in utrs:
        cases = await find_case_by_order_ids(session, utr=u)
        if cases:
            return Correlation(cases[0].case_id, 0.9, "utr", {"utr": u})
    # 3. registration number mentioned in text
    monitoring = await list_monitoring_cases(session)
    for c in monitoring:
        if c.registration_number and c.registration_number.lower() in (text or "").lower():
            return Correlation(c.case_id, 0.85, "registration", {"registration": c.registration_number})
    # 4. proximity: exactly one case posted recently and the sender is authoritative
    if sender_authority in ("system_bot", "group_member") and sent_at:
        window = timedelta(minutes=s.betix_proximity_window_minutes)
        recent = [
            c
            for c in monitoring
            if c.betix_posted_at and abs((sent_at - c.betix_posted_at).total_seconds()) <= window.total_seconds()
        ]
        if len(recent) == 1:
            return Correlation(
                recent[0].case_id, 0.55, "proximity", {"window_minutes": s.betix_proximity_window_minutes}
            )
    # No reply chain, no identifier, nothing posted recently: this message is not about any of our cases.
    return Correlation(None, 0.0, "none", {})
