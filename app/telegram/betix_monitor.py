"""Monitor the Betix Pay support group. Works with the Bot API (bot must see group messages: make it an
admin or disable privacy mode in @BotFather) or a Telethon user session (BETIX_MONITOR_MODE=user)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.analyzer import AIUnavailable, get_analyzer
from app.cases.correlation import correlate_betix_message
from app.cases.manager import apply_verification_signal
from app.config import get_settings
from app.db.repository import add_betix_message, get_betix_message, get_case
from app.db.session import session_scope
from app.telegram.confirmation import (
    AUTHORITY_SELF,
    AUTHORITY_SYSTEM_BOT,
    Classification,
    authority_for_sender,
    classify_human_message,
    classify_system_message,
)
from app.utils.logging import get_logger
from app.utils.timeutil import utcnow

log = get_logger("betix.monitor")


@dataclass
class IncomingGroupMessage:
    chat_id: int
    message_id: int
    sender_id: int | None
    sender_username: str | None
    sender_name: str | None
    sender_is_bot: bool
    text: str
    reply_to_message_id: int | None = None
    has_media: bool = False
    sent_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["sent_at"] = self.sent_at.isoformat() if self.sent_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "IncomingGroupMessage":
        d = dict(d)
        if d.get("sent_at"):
            d["sent_at"] = datetime.fromisoformat(d["sent_at"])
        return cls(**d)


# ----------------------------------------------------------------- identity & membership
# Our own accounts, learned at runtime (the input bot's id from get_me) in addition to OUR_TELEGRAM_* config.
_self_ids: set[int] = set()
_self_usernames: set[str] = set()


def register_self(user_id: int | None = None, username: str | None = None) -> None:
    if user_id is not None:
        _self_ids.add(user_id)
    if username:
        _self_usernames.add(username.lstrip("@").lower())


def self_ids() -> set[int]:
    return _self_ids | get_settings().our_ids


def self_usernames() -> set[str]:
    return _self_usernames | get_settings().our_usernames


MembershipChecker = Callable[[int, int], Awaitable[bool | None]]
_membership_checker: MembershipChecker | None = None
_membership_cache: dict[tuple[int, int], tuple[bool, datetime]] = {}
_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


def set_membership_checker(fn: MembershipChecker | None) -> None:
    """Inject a membership lookup (tests) ; None restores the Bot API default."""
    global _membership_checker
    _membership_checker = fn
    _membership_cache.clear()


async def _bot_api_membership(chat_id: int, user_id: int) -> bool | None:
    """True/False when the Bot API answers, None when it cannot (not a member itself, user mode, error)."""
    try:
        from app.telegram.notifications import get_bot

        member = await get_bot().get_chat_member(chat_id, user_id)
        return member.status in _MEMBER_STATUSES
    except Exception as exc:  # noqa: BLE001
        log.warning("membership lookup failed; using message origin", chat_id=chat_id, error=str(exc)[:120])
        return None


async def is_group_member(chat_id: int, user_id: int | None) -> bool | None:
    """Cached membership answer for a sender in the Betix group. None = could not be determined."""
    s = get_settings()
    if user_id is None:
        return None
    if not s.betix_verify_membership and _membership_checker is None:
        return None
    key = (chat_id, user_id)
    cached = _membership_cache.get(key)
    if cached and utcnow() - cached[1] < timedelta(minutes=s.betix_membership_cache_minutes):
        return cached[0]
    checker = _membership_checker or _bot_api_membership
    result = await checker(chat_id, user_id)
    if result is not None:
        _membership_cache[key] = (result, utcnow())
    return result


def is_betix_chat(chat_id: int, username: str | None = None) -> bool:
    """Is this one of the Betix groups the bot works with (BETIX_GROUP_CHAT_ID may list several)?"""
    s = get_settings()
    if chat_id in s.betix_chat_ids:
        return True
    if s.betix_group_username and username:
        return s.betix_group_username.lstrip("@").lower() == username.lstrip("@").lower()
    return False


async def handle_group_message(session: AsyncSession, msg: IncomingGroupMessage) -> dict:
    """Store, correlate, classify and act on one inbound group message. Idempotent per (chat, message)."""
    s = get_settings()
    in_chat = is_betix_chat(msg.chat_id)
    is_member = await is_group_member(msg.chat_id, msg.sender_id) if in_chat else False
    authority = authority_for_sender(
        sender_id=msg.sender_id,
        username=msg.sender_username,
        is_bot=msg.sender_is_bot,
        in_betix_chat=in_chat,
        is_member=is_member,
        system_bot_ids=s.system_bot_ids,
        system_bot_usernames=s.system_bot_usernames,
        our_ids=self_ids(),
        our_usernames=self_usernames(),
        reviewer_ids=s.reviewer_ids,
        reviewer_usernames=s.reviewer_usernames,
    )
    if authority == AUTHORITY_SYSTEM_BOT:
        cls = classify_system_message(msg.text, s.betex_order_id_pattern, s.plat_order_pattern)
    else:
        cls = classify_human_message(msg.text, s.betex_order_id_pattern, s.plat_order_pattern, has_media=msg.has_media)

    corr = await correlate_betix_message(
        session,
        chat_id=msg.chat_id,
        reply_to_message_id=msg.reply_to_message_id,
        order_ids=cls.order_ids,
        plat_order_nos=cls.plat_order_nos,
        utrs=cls.utrs,
        text=msg.text,
        sender_authority=authority,
        sent_at=msg.sent_at or utcnow(),
    )

    # AI as a secondary signal for unclassified human text on a correlated case (never changes authority).
    if (
        cls.outcome == "UNKNOWN"
        and authority not in (AUTHORITY_SYSTEM_BOT, AUTHORITY_SELF)
        and corr.case_id
        and msg.text
        and not msg.has_media
        and s.ai_classify_unknown_betix_replies
    ):
        try:
            case = await get_case(session, corr.case_id)
            ctx = (
                f"Merchant order {case.betex_pay_order_id}, amount {case.amount}, awaiting confirmation."
                if case
                else ""
            )
            payload = await get_analyzer().classify_betix_reply(msg.text, context=ctx)
            cls = Classification(
                payload.get("outcome", "UNKNOWN"),
                min(float(payload.get("confidence", 0)), 0.85),
                "ai",
                msg.text[:60],
                cls.order_ids,
                cls.plat_order_nos,
                cls.utrs,
                {"reasoning": payload.get("reasoning")},
            )
        except AIUnavailable:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("ai classification failed", error=repr(exc))

    row = await add_betix_message(
        session,
        case_id=corr.case_id,
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        reply_to_message_id=msg.reply_to_message_id,
        direction="in",
        kind="reply",
        sender_id=msg.sender_id,
        sender_username=msg.sender_username,
        sender_name=msg.sender_name,
        sender_is_bot=msg.sender_is_bot,
        text=msg.text,
        has_media=msg.has_media,
        classification=cls.as_dict(),
        correlation=corr.as_dict(),
        sent_at=msg.sent_at,
    )
    if row is None:
        return {"action": "duplicate"}
    if msg.reply_to_message_id:
        parent = await get_betix_message(session, msg.chat_id, msg.reply_to_message_id)
        if parent is not None and parent.direction == "out" and parent.kind == "pi_query":
            # The Betix bot's answer to our `/pi <ORDER-ID>`: data for the UPI tie-break. Its "Paid | Success" is
            # about THAT order, not a confirmation of this payment - never fed to the verification logic.
            action = "pi_reply"
            if authority == AUTHORITY_SYSTEM_BOT:
                from app.cases.manager import resolve_pi_check

                action = "pi_" + await resolve_pi_check(session, corr.case_id)
            log.info("betix /pi answer", case_id=corr.case_id, action=action)
            return {"action": action, "outcome": cls.outcome, "authority": authority, "case_id": corr.case_id}
    if not corr.case_id or cls.outcome in ("IRRELEVANT",):
        return {
            "action": "unlinked" if not corr.case_id else "irrelevant",
            "outcome": cls.outcome,
            "authority": authority,
        }
    case = await get_case(session, corr.case_id)
    actor = ("@" + msg.sender_username) if msg.sender_username else (msg.sender_name or str(msg.sender_id))
    action = await apply_verification_signal(
        session,
        case,
        cls,
        authority=authority,
        betix_message_id=msg.message_id,
        actor=actor,
        correlation_confidence=corr.confidence,
        sender_id=msg.sender_id,
        sender_username=msg.sender_username,
    )
    log.info(
        "betix message handled",
        case_id=case.case_id,
        outcome=cls.outcome,
        authority=authority,
        method=corr.method,
        action=action,
    )
    return {
        "action": action,
        "outcome": cls.outcome,
        "authority": authority,
        "case_id": case.case_id,
        "correlation": corr.method,
    }


# ----------------------------------------------------------------- aiogram (bot mode)
def aiogram_message_to_incoming(message) -> IncomingGroupMessage:
    u = message.from_user
    text = message.text or message.caption or ""
    return IncomingGroupMessage(
        chat_id=message.chat.id,
        message_id=message.message_id,
        sender_id=u.id if u else None,
        sender_username=u.username if u else None,
        sender_name=(u.full_name if u else None),
        sender_is_bot=bool(u and u.is_bot),
        text=text,
        reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
        has_media=bool(message.photo or message.document or message.video),
        sent_at=message.date,
    )


def build_router():
    from aiogram import F, Router

    router = Router(name="betix_group")

    @router.message(F.chat.type.in_({"group", "supergroup"}))
    async def _on_group(message):  # noqa: ANN001
        if not is_betix_chat(message.chat.id, message.chat.username):
            return
        inc = aiogram_message_to_incoming(message)
        from app.workers.queue import enqueue

        await enqueue("betix_message_job", inc.as_dict(), job_id=f"betix-{inc.chat_id}-{inc.message_id}")

    return router


# ----------------------------------------------------------------- Telethon (user mode)
async def run_user_monitor(stop_event=None) -> None:
    from telethon import events

    from app.telegram.user_client import get_user_client
    from app.workers.queue import enqueue

    s = get_settings()
    client = await get_user_client()
    entities = [await client.get_entity(c) for c in s.betix_chats]  # every configured group
    entity = entities[0]
    chat_id = (
        int(f"-100{entity.id}")
        if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False)
        else -entity.id
    )

    @client.on(events.NewMessage(chats=entities))
    async def _handler(event):  # noqa: ANN001
        sender = await event.get_sender()
        inc = IncomingGroupMessage(
            chat_id=chat_id,
            message_id=event.message.id,
            sender_id=getattr(sender, "id", None),
            sender_username=getattr(sender, "username", None),
            sender_name=" ".join(
                filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)])
            )
            or None,
            sender_is_bot=bool(getattr(sender, "bot", False)),
            text=event.message.message or "",
            reply_to_message_id=event.message.reply_to_msg_id,
            has_media=event.message.media is not None,
            sent_at=event.message.date,
        )
        await enqueue("betix_message_job", inc.as_dict(), job_id=f"betix-{inc.chat_id}-{inc.message_id}")

    log.info("telethon monitor started", chat_id=chat_id)
    if stop_event is not None:
        await stop_event.wait()
        await client.disconnect()
    else:
        await client.run_until_disconnected()


async def backfill_user_mode(limit: int = 200) -> int:
    """After a restart in user mode, re-read recent group history so nothing was missed while down."""
    from app.telegram.user_client import get_user_client

    s = get_settings()
    client = await get_user_client()
    entity = await client.get_entity(s.betix_chat)
    chat_id = int(f"-100{entity.id}") if getattr(entity, "megagroup", False) else -entity.id
    n = 0
    async for m in client.iter_messages(entity, limit=limit, reverse=False):
        sender = await m.get_sender()
        inc = IncomingGroupMessage(
            chat_id=chat_id,
            message_id=m.id,
            sender_id=getattr(sender, "id", None),
            sender_username=getattr(sender, "username", None),
            sender_name=getattr(sender, "first_name", None),
            sender_is_bot=bool(getattr(sender, "bot", False)),
            text=m.message or "",
            reply_to_message_id=m.reply_to_msg_id,
            has_media=m.media is not None,
            sent_at=m.date,
        )
        async with session_scope() as session:
            r = await handle_group_message(session, inc)
        if r.get("action") == "pi_ready":  # a /pi answer that arrived while we were down settled the order
            from app.workers.queue import enqueue

            await enqueue("post_case_job", r["case_id"], job_id=f"post-{r['case_id']}")
        if r.get("action") != "duplicate":
            n += 1
    return n
