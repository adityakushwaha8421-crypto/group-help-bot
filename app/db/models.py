"""SQLAlchemy models. Every table required by the spec plus case_status_history for full status history."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class TZDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo; we put it back)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from datetime import timezone as _tz

        if value.tzinfo is not None:
            value = value.astimezone(_tz.utc)
            if dialect.name == "sqlite":
                value = value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from datetime import timezone as _tz

        if value.tzinfo is None:
            value = value.replace(tzinfo=_tz.utc)
        return value


class Base(DeclarativeBase):
    pass


class CaseStatus(str, enum.Enum):
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    ANALYZING_EVIDENCE = "ANALYZING_EVIDENCE"
    SEARCHING_ORDER = "SEARCHING_ORDER"
    ORDER_MATCH_FOUND = "ORDER_MATCH_FOUND"
    ORDER_MATCH_AMBIGUOUS = "ORDER_MATCH_AMBIGUOUS"
    CHECKING_ORDER_UPI = "CHECKING_ORDER_UPI"  # several close orders: asking Betix (/pi) for each order's UPI
    READY_FOR_BETIX = "READY_FOR_BETIX"
    POSTED_TO_BETIX = "POSTED_TO_BETIX"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    FOLLOWUP_1_SENT = "FOLLOWUP_1_SENT"
    FOLLOWUP_2_SENT = "FOLLOWUP_2_SENT"
    VERIFIED = "VERIFIED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    ALREADY_SUCCESS = "ALREADY_SUCCESS"  # the matched Illunise order was already Success: nothing sent to Betix
    ALREADY_SENT = "ALREADY_SENT"  # another case already sent this order id to the Betix group: never sent twice


OPEN_STATUSES = {
    CaseStatus.WAITING_FOR_INPUT,
    CaseStatus.ANALYZING_EVIDENCE,
    CaseStatus.SEARCHING_ORDER,
    CaseStatus.ORDER_MATCH_FOUND,
    CaseStatus.ORDER_MATCH_AMBIGUOUS,
    CaseStatus.CHECKING_ORDER_UPI,
    CaseStatus.READY_FOR_BETIX,
    CaseStatus.POSTED_TO_BETIX,
    CaseStatus.WAITING_FOR_CONFIRMATION,
    CaseStatus.FOLLOWUP_1_SENT,
    CaseStatus.FOLLOWUP_2_SENT,
}
MONITORING_STATUSES = {
    CaseStatus.POSTED_TO_BETIX,
    CaseStatus.WAITING_FOR_CONFIRMATION,
    CaseStatus.FOLLOWUP_1_SENT,
    CaseStatus.FOLLOWUP_2_SENT,
    CaseStatus.ESCALATED,
}
TERMINAL_STATUSES = {CaseStatus.VERIFIED, CaseStatus.FAILED, CaseStatus.ALREADY_SUCCESS, CaseStatus.ALREADY_SENT}


class EvidenceType(str, enum.Enum):
    payment_screenshot = "payment_screenshot"
    bank_statement = "bank_statement"
    payment_video = "payment_video"
    other = "other"


def _ts():
    return mapped_column(TZDateTime(), server_default=func.now(), nullable=False)


KIND_PAYMENT, KIND_WITHDRAWAL = "payment", "withdrawal"


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(40), default=CaseStatus.WAITING_FOR_INPUT.value, index=True)
    # Who SUBMITTED the evidence to our bot (the admin, or the customer directly).
    source_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_username: Mapped[str | None] = mapped_column(String(100))
    # The ORIGINAL customer: the sender of the forwarded evidence, never the person forwarding it.
    original_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    original_username: Mapped[str | None] = mapped_column(String(100))
    original_first_name: Mapped[str | None] = mapped_column(String(200))
    original_last_name: Mapped[str | None] = mapped_column(String(200))
    original_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    original_message_id: Mapped[int | None] = mapped_column(BigInteger)
    evidence_forwarded: Mapped[bool] = mapped_column(Boolean, default=False)
    # the operator-chat message that is edited live as the case progresses (see telegram/progress.py)
    progress_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    progress_message_id: Mapped[int | None] = mapped_column(BigInteger)

    mobile: Mapped[str | None] = mapped_column(String(20), index=True)  # PRIMARY identifier (Illunise search key)
    registration_number: Mapped[str | None] = mapped_column(String(100), index=True)  # optional extra reference
    illunise_order_id: Mapped[str | None] = mapped_column(String(100), index=True)
    order_created_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    order_amount: Mapped[float | None] = mapped_column(Float)
    gateway: Mapped[str | None] = mapped_column(String(60))
    betex_pay_order_id: Mapped[str | None] = mapped_column(String(100), index=True)
    # payment (default) or withdrawal: a withdrawal case carries a WD-xxxxx-xxxxx id + the bank statement only
    kind: Mapped[str] = mapped_column(String(20), default="payment", server_default="payment", index=True)
    withdrawal_id: Mapped[str | None] = mapped_column(String(60), index=True)
    betix_plat_order_no: Mapped[str | None] = mapped_column(String(100), index=True)
    # /pi UPI check: the close order ids, best first. Asked ONE BY ONE; the first whose UPI fits wins.
    pi_check_orders: Mapped[list | None] = mapped_column(JSON)
    amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    payment_time: Mapped[datetime | None] = mapped_column(TZDateTime())
    utr: Mapped[str | None] = mapped_column(String(64), index=True)
    upi_id: Mapped[str | None] = mapped_column(String(120))
    payer_name: Mapped[str | None] = mapped_column(String(200))
    statement_password: Mapped[str | None] = mapped_column(String(120))

    extraction: Mapped[dict | None] = mapped_column(JSON)  # merged AI extraction (field->value/confidence/source)
    match_confidence: Mapped[float | None] = mapped_column(Float)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    betix_posted_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    betix_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    betix_root_message_id: Mapped[int | None] = mapped_column(
        BigInteger
    )  # our screenshot message id: the anchor every follow-up replies to
    followup_1_message_id: Mapped[int | None] = mapped_column(BigInteger)
    followup_2_message_id: Mapped[int | None] = mapped_column(BigInteger)
    confirmation_type: Mapped[str | None] = mapped_column(String(30))  # system_bot | group_member | manual
    confirmation_message_id: Mapped[int | None] = mapped_column(BigInteger)
    confirmation_user_id: Mapped[int | None] = mapped_column(BigInteger)
    confirmation_username: Mapped[str | None] = mapped_column(String(100))
    confirmation_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    system_confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    reviewer_confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    confirmed_by: Mapped[str | None] = mapped_column(String(200))
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    followup_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    followups_sent: Mapped[int] = mapped_column(Integer, default=0)
    last_input_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    processing_version: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    messages: Mapped[list["CaseMessage"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    candidates: Mapped[list["OrderCandidate"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    followups: Mapped[list["Followup"]] = relationship(back_populates="case", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Case {self.case_id} {self.status}>"


class CaseStatusHistory(Base):
    __tablename__ = "case_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(40), ForeignKey("cases.case_id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), default="system")
    created_at: Mapped[datetime] = _ts()


class CaseMessage(Base):
    """Every Telegram message received by the input bot that was attached to a case."""

    __tablename__ = "case_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_case_messages_chat_msg"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(40), ForeignKey("cases.case_id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(30))  # text|photo|document|video|other
    text: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSON)
    received_at: Mapped[datetime] = _ts()
    case: Mapped[Case] = relationship(back_populates="messages")


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("telegram_chat_id", "telegram_message_id", name="uq_evidence_msg"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(40), ForeignKey("cases.case_id"), index=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    file_id: Mapped[str | None] = mapped_column(String(300))
    file_unique_id: Mapped[str | None] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(30), default=EvidenceType.other.value)
    filename: Mapped[str | None] = mapped_column(String(300))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int | None] = mapped_column(BigInteger)
    local_path: Mapped[str | None] = mapped_column(String(500))
    downloaded: Mapped[bool] = mapped_column(Boolean, default=False)
    analysis: Mapped[dict | None] = mapped_column(JSON)
    posted_to_betix_message_id: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    case: Mapped[Case] = relationship(back_populates="evidence")


class OrderCandidate(Base):
    __tablename__ = "order_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(40), ForeignKey("cases.case_id"), index=True)
    search_attempt: Mapped[int] = mapped_column(Integer, default=1)
    illunise_order_id: Mapped[str | None] = mapped_column(String(100))
    betex_order_id: Mapped[str | None] = mapped_column(String(100))
    registration_number: Mapped[str | None] = mapped_column(String(100))
    amount: Mapped[float | None] = mapped_column(Float)
    order_time: Mapped[datetime | None] = mapped_column(TZDateTime())
    status: Mapped[str | None] = mapped_column(String(80))
    utr: Mapped[str | None] = mapped_column(String(64))
    upi_id: Mapped[str | None] = mapped_column(String(120))
    payer_name: Mapped[str | None] = mapped_column(String(200))
    gateway: Mapped[str | None] = mapped_column(String(60))
    betix_plat_order_no: Mapped[str | None] = mapped_column(String(100))
    raw: Mapped[dict | None] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(Float)
    signals: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()
    case: Mapped[Case] = relationship(back_populates="candidates")


class OrderMatch(Base):
    __tablename__ = "order_matches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(40), ForeignKey("cases.case_id"), index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("order_candidates.id"))
    decision: Mapped[str] = mapped_column(String(30))  # MATCHED|AMBIGUOUS|NO_MATCH|NO_CANDIDATES
    confidence: Mapped[float | None] = mapped_column(Float)
    runner_up_confidence: Mapped[float | None] = mapped_column(Float)
    illunise_order_id: Mapped[str | None] = mapped_column(String(100))
    betex_order_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()


class BetixMessage(Base):
    """Messages in the Betix group: both what we posted (direction=out) and what we observed (direction=in)."""

    __tablename__ = "betix_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_betix_messages_chat_msg"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("cases.case_id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in|out
    kind: Mapped[str | None] = mapped_column(
        String(40)
    )  # evidence_screenshot|order_details|bank_statement|payment_video|followup_1|followup_2|status_query|reply
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_username: Mapped[str | None] = mapped_column(String(100))
    sender_name: Mapped[str | None] = mapped_column(String(200))
    sender_is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str | None] = mapped_column(Text)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[dict | None] = mapped_column(JSON)
    correlation: Mapped[dict | None] = mapped_column(JSON)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    created_at: Mapped[datetime] = _ts()


class VerificationEvent(Base):
    __tablename__ = "verification_events"
    __table_args__ = (UniqueConstraint("case_id", "dedupe_key", name="uq_verification_event"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(40), ForeignKey("cases.case_id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(
        String(40)
    )  # SYSTEM_SUCCESS|REVIEWER_SUCCESS|SYSTEM_FAILED|SYSTEM_PENDING|REVIEWER_CHECKING|...
    authority: Mapped[str] = mapped_column(String(30))  # system_bot|group_member|self|unknown
    betix_message_id: Mapped[int | None] = mapped_column(BigInteger)
    actor: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()


class Followup(Base):
    __tablename__ = "followups"
    __table_args__ = (UniqueConstraint("case_id", "number", name="uq_followup_case_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(40), ForeignKey("cases.case_id"), index=True)
    number: Mapped[int] = mapped_column(Integer)  # 1, 2 ... ; 99 = escalation check
    due_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)  # scheduled|sent|cancelled|failed
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    betix_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(
        Integer, default=0
    )  # failed sends so far; retried until FOLLOWUP_MAX_ATTEMPTS
    created_at: Mapped[datetime] = _ts()
    case: Mapped[Case] = relationship(back_populates="followups")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_notification_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(80))
    chat_id: Mapped[str | None] = mapped_column(String(50))
    text: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    actor: Mapped[str] = mapped_column(String(100), default="system")
    result: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(60))
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()


Index("ix_cases_open_lookup", Case.source_chat_id, Case.status, Case.last_input_at)
