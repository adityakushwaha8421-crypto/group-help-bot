"""Post evidence + order details into the Betix Pay support group and send follow-ups.
Supports Bot API (default) or a Telethon user session. Every send is recorded in betix_messages and
guarded by kind so a retry never posts twice."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BetixMessage, Case, Evidence, EvidenceType
from app.db.repository import add_betix_message, audit, case_has_out_kind, list_case_betix_messages
from app.telegram.notifications import get_bot
from app.telegram.throttle import get_throttle
from app.utils.logging import get_logger
from app.utils.timeutil import utcnow

log = get_logger("betix.poster")


class BetixPostError(Exception):
    pass


def followup_text(case: Case, number: int) -> str:  # noqa: ARG001
    """Short and plain: the follow-up is a REPLY to our screenshot post, so the order id is already in view."""
    return get_settings().betix_followup_text.strip() or "Any update?"


class BetixPoster:
    """Posts for ONE case at a time (a job makes a fresh poster). `self.chat` is the group the current case lives
    in: a posted case is always answered in the group it was posted to, a new case goes to `choose_group`."""

    def __init__(self, chat: int | str | None = None):
        self.s = get_settings()
        self.default_chat = chat if chat is not None else self.s.betix_chat
        if self.default_chat is None:
            raise BetixPostError("BETIX_GROUP_CHAT_ID / BETIX_GROUP_USERNAME not configured")
        self.chat = self.default_chat

    def choose_group(self, case: Case) -> int | str:
        """Which group a NEW case is posted to. Today: the default (first configured) group. This is the one
        place to put a routing rule (per operator, per customer, round-robin) when more groups are in use."""
        return self.default_chat

    def bind(self, case: Case) -> int | str:
        """Point this poster at the case's group: the one it was posted to, else the one chosen for it."""
        self.chat = case.betix_chat_id if case.betix_chat_id else self.choose_group(case)
        return self.chat

    # ---- low level senders (bot mode) ----
    async def _send_text(self, text: str, reply_to: int | None = None) -> int:
        if text.strip().lower().startswith("/upi"):
            raise BetixPostError("/upi is never sent to the Betix group")
        if self.s.betix_post_mode == "user":
            from app.telegram.user_client import get_user_client

            client = await get_user_client()
            m = await client.send_message(self.chat, text, reply_to=reply_to)
            return m.id
        m = await get_throttle().run(
            self.chat, lambda: get_bot().send_message(self.chat, text, reply_to_message_id=reply_to)
        )
        return m.message_id

    async def _send_media(self, ev: Evidence, caption: str | None, reply_to: int | None = None) -> int:
        if self.s.betix_post_mode == "user":
            from app.telegram.user_client import get_user_client

            client = await get_user_client()
            if not ev.local_path or not Path(ev.local_path).exists():
                raise BetixPostError("user mode requires the file to be downloaded locally")
            m = await client.send_file(
                self.chat,
                ev.local_path,
                caption=caption or None,
                reply_to=reply_to,
                force_document=(ev.type == EvidenceType.bank_statement.value),
            )
            return m.id
        bot = get_bot()
        from aiogram.types import FSInputFile

        source = ev.file_id or (FSInputFile(ev.local_path) if ev.local_path else None)
        if source is None:
            raise BetixPostError("evidence has neither file_id nor local file")
        kw = {"caption": caption or None, "reply_to_message_id": reply_to}
        send = get_throttle().run
        if ev.type == EvidenceType.payment_video.value:
            m = await send(self.chat, lambda: bot.send_video(self.chat, source, **kw))
        elif ev.type == EvidenceType.payment_screenshot.value and (ev.mime_type or "").startswith("image/"):
            m = await send(self.chat, lambda: bot.send_photo(self.chat, source, **kw))
        elif ev.type == EvidenceType.payment_screenshot.value and not ev.mime_type:
            m = await send(self.chat, lambda: bot.send_photo(self.chat, source, **kw))
        else:
            m = await send(self.chat, lambda: bot.send_document(self.chat, source, **kw))
        return m.message_id

    async def _delete(self, message_id: int) -> None:
        if self.s.betix_post_mode == "user":
            from app.telegram.user_client import get_user_client

            client = await get_user_client()
            await client.delete_messages(self.chat, [message_id])
            return
        await get_throttle().run(self.chat, lambda: get_bot().delete_message(self.chat, message_id))

    async def delete_message(self, message_id: int) -> str | None:
        """Delete one message from the Betix group. Returns None on success, else the reason (never raises).
        Our own messages can always be deleted; another member's (the Betix bot's) needs admin 'Delete messages'."""
        try:
            await self._delete(message_id)
            return None
        except Exception as exc:  # noqa: BLE001
            return str(exc)[:200] or exc.__class__.__name__

    async def _record(
        self,
        session: AsyncSession,
        case: Case,
        message_id: int,
        kind: str,
        text: str | None,
        reply_to: int | None = None,
        has_media: bool = False,
    ) -> BetixMessage | None:
        chat_id = self.chat if isinstance(self.chat, int) else 0
        return await add_betix_message(
            session,
            case_id=case.case_id,
            chat_id=chat_id,
            message_id=message_id,
            reply_to_message_id=reply_to,
            direction="out",
            kind=kind,
            text=text,
            has_media=has_media,
            sent_at=utcnow(),
            sender_username="me",
        )

    # ---- high level ----
    async def post_case(self, session: AsyncSession, case: Case, evidence: list[Evidence]) -> None:
        """POLICY: the Betix group receives exactly ONE message per case — the payment screenshot with the plain
        Illunise order id as its caption. No details, no statement, no video, no URL, no Betix-side id.
        That message is the anchor every follow-up replies to. Idempotent."""
        self.bind(case)
        if not case.betex_pay_order_id:
            raise BetixPostError("case has no Illunise order id; refusing to post")
        shots = [e for e in evidence if e.type == EvidenceType.payment_screenshot.value]
        if not shots:
            raise BetixPostError("no payment screenshot to post")

        caption = self.s.betix_screenshot_caption_template.format(
            betex_order_id=case.betex_pay_order_id,
            registration_number=case.mobile or "",
            amount=f"{case.amount:,.2f}" if case.amount is not None else "",
            case_id=case.case_id,
        )

        root = await case_has_out_kind(session, case.case_id, "evidence_screenshot")
        if root is None:
            mid = await self._send_media(shots[0], caption)
            root = await self._record(session, case, mid, "evidence_screenshot", caption, has_media=True)
            shots[0].posted_to_betix_message_id = mid
            case.betix_root_message_id = mid  # the anchor: every follow-up is a Telegram reply to this
            case.betix_chat_id = self.chat if isinstance(self.chat, int) else None
            await audit(
                session, "BETIX_EVIDENCE_POSTED", case_id=case.case_id, result="screenshot", details={"message_id": mid}
            )
            await session.commit()
        case.betix_posted_at = case.betix_posted_at or utcnow()

    async def post_withdrawal(self, session: AsyncSession, case: Case, evidence: list[Evidence]) -> None:
        """WITHDRAWAL: the group receives the id as "BX<withdrawal id>" (one plain text message, the case's
        anchor), and the bank statement as a reply to it. Nothing else. Idempotent."""
        self.bind(case)
        if not case.withdrawal_id:
            raise BetixPostError("case has no withdrawal id; refusing to post")
        text = f"BX{case.withdrawal_id}"
        root = await case_has_out_kind(session, case.case_id, "withdrawal_root")
        if root is None:
            mid = await self._send_text(text)
            root = await self._record(session, case, mid, "withdrawal_root", text)
            case.betix_root_message_id = mid  # the statement and every follow-up reply to this
            case.betix_chat_id = self.chat if isinstance(self.chat, int) else None
            await audit(
                session,
                "BETIX_EVIDENCE_POSTED",
                case_id=case.case_id,
                result="withdrawal_id",
                details={"message_id": mid, "text": text},
            )
            await session.commit()
        case.betix_posted_at = case.betix_posted_at or utcnow()
        await self.post_late_evidence(session, case, evidence)  # the bank statement, as a reply

    async def post_late_evidence(self, session: AsyncSession, case: Case, evidence: list[Evidence]) -> int:
        """Statement / video are ONLY ever sent as replies to the screenshot post (never standalone)."""
        self.bind(case)
        root_id = case.betix_root_message_id
        if not root_id:
            raise BetixPostError("no Betix screenshot message to reply to; refusing to send extra evidence")
        n = 0
        for ev in evidence:
            if ev.posted_to_betix_message_id or ev.type == EvidenceType.other.value:
                continue
            cap = None
            if ev.type == EvidenceType.bank_statement.value and case.statement_password:
                cap = f"Password:- {case.statement_password}"
            mid = await self._send_media(ev, cap, reply_to=root_id)
            ev.posted_to_betix_message_id = mid
            await self._record(session, case, mid, ev.type, cap, root_id, True)
            await audit(
                session,
                "BETIX_EVIDENCE_POSTED",
                case_id=case.case_id,
                result=ev.type + "_late",
                details={"message_id": mid},
            )
            await session.commit()
            n += 1
        return n

    async def send_statement_password(self, session: AsyncSession, case: Case) -> int | None:
        """'Password:- xxxx' as a REPLY to the screenshot post, once, when the statement went out without it."""
        self.bind(case)
        if not case.statement_password or not case.betix_root_message_id:
            return None
        if await case_has_out_kind(session, case.case_id, "statement_password"):
            return None
        text = f"Password:- {case.statement_password}"
        mid = await self._send_text(text, reply_to=case.betix_root_message_id)
        await self._record(session, case, mid, "statement_password", text, case.betix_root_message_id)
        await audit(
            session,
            "BETIX_EVIDENCE_POSTED",
            case_id=case.case_id,
            result="statement_password",
            details={"message_id": mid},
        )
        return mid

    async def send_pi_query(self, session: AsyncSession, case: Case, order_id: str) -> int:
        """Ask the Betix bot for one order's details (its "Order's UPI"): `/pi <ORDER-ID>`, once per order."""
        self.bind(case)
        text = self.s.betix_pi_command_template.format(order_id=order_id)
        for m in await list_case_betix_messages(session, case.case_id):
            if m.direction == "out" and m.kind == "pi_query" and m.text == text:
                return m.message_id
        mid = await self._send_text(text)
        await self._record(session, case, mid, "pi_query", text)
        await audit(session, "BETIX_PI_QUERY_SENT", case_id=case.case_id, result=order_id, details={"message_id": mid})
        await session.commit()
        return mid

    async def send_followup(self, session: AsyncSession, case: Case, number: int) -> int:
        self.bind(case)
        kind = f"followup_{number}"
        existing = await case_has_out_kind(session, case.case_id, kind)
        if existing:
            return existing.message_id
        if not case.betix_root_message_id:
            raise BetixPostError("no Betix screenshot message to reply to; refusing a standalone follow-up")
        text = followup_text(case, number)
        # ALWAYS a reply to the original screenshot post, so the group sees it attached to the payment proof.
        mid = await self._send_text(text, reply_to=case.betix_root_message_id)
        await self._record(session, case, mid, kind, text, case.betix_root_message_id)
        if number == 1:
            case.followup_1_message_id = mid
        elif number == 2:
            case.followup_2_message_id = mid
        return mid
