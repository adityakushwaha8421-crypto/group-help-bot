"""Admin notifications (Telegram). Idempotent per (case, kind[, suffix]) through the notifications table."""

from __future__ import annotations

import html

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Case, Notification
from app.db.repository import audit, reserve_notification
from app.telegram.throttle import get_throttle
from app.telegram.ui import b, case_label, code, i, para
from app.utils.logging import get_logger
from app.utils.security import idempotency_key
from app.utils.timeutil import fmt_local

log = get_logger("notify")

_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


def get_bot():
    global _bot
    if _bot is None:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties

        s = get_settings()
        if not s.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
        _bot = Bot(token=s.telegram_bot_token, default=DefaultBotProperties(parse_mode=None))
    return _bot


def _money(amount: float | None, currency: str = "INR") -> str:
    if amount is None:
        return "-"
    sym = "₹" if currency == "INR" else currency + " "
    return f"{sym}{amount:,.2f}"


def evidence_line(evidence) -> str:
    types = {e.type for e in evidence}
    tick = lambda t: "✅" if t in types else "❌"  # noqa: E731
    return (
        f"Screenshot {tick('payment_screenshot')}\nBank Statement {tick('bank_statement')}\n"
        f"Video {tick('payment_video')}"
    )


def _esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def customer_link(case: Case) -> str:
    """The original customer as HTML the admin can click.

    @username        -> Telegram makes it clickable on its own
    no username      -> a tg://user?id=… text mention (opens the user when Telegram allows it) plus the
                        plain id, and an explicit note that the username is unavailable
    """
    name = " ".join(x for x in (case.original_first_name, case.original_last_name) if x).strip()
    if case.original_username:
        return f"@{_esc(case.original_username.lstrip('@'))}"
    if case.original_user_id:
        label = _esc(name or f"user {case.original_user_id}")
        return (
            f'<a href="tg://user?id={case.original_user_id}">{label}</a> '
            f"(User ID: <code>{case.original_user_id}</code>, no username)"
        )
    return f"{_esc(name) if name else 'unknown'} (no username, no user id)"


def confirmer_line(case: Case, confirmed_by: str | None) -> str:
    if case.confirmation_type == "system_bot":
        return "Betix System"
    if case.confirmation_username:
        return f"@{_esc(case.confirmation_username.lstrip('@'))}"
    if case.confirmation_user_id:
        return f'<a href="tg://user?id={case.confirmation_user_id}">Betix member {case.confirmation_user_id}</a>'
    return _esc(confirmed_by or "-")


def format_confirmed(case: Case, evidence, *, confirmed_by: str | None, confirmed_at, tz: str) -> str:
    """Sent when Betix confirms. Identifies the ORIGINAL customer so the admin can open the chat directly."""
    order = case.betex_pay_order_id or case.illunise_order_id or "-"
    who = " ".join(x for x in (case.original_first_name, case.original_last_name) if x).strip()
    facts = [
        f"\U0001f464 Customer: {customer_link(case)}" + (f" ({_esc(who)})" if who and case.original_username else ""),
        f"\U0001f4f1 Mobile: {code(case.mobile or '-')}",
        f"\U0001f4b0 Amount: {_money(case.amount, case.currency)}",
        f"\U0001f9fe Order: {code(order)}",
    ]
    bot_line = (  # only the Betix system bot gets its own line; a member is already named above
        f"\U0001f916 @{_esc(case.confirmation_username.lstrip('@'))}"
        if case.confirmation_type == "system_bot" and case.confirmation_username
        else ""
    )
    stamp = [
        f"\u2705 Confirmed by: {confirmer_line(case, confirmed_by)}",
        bot_line,
        f"\U0001f552 Time: {fmt_local(confirmed_at, tz, '%H:%M')}",
    ]
    return para(
        "\u2705 " + b("PAYMENT CONFIRMED"),
        facts,
        [x for x in stamp if x],
        "\U0001f64f Payment successfully confirmed.",
    )


def _ident_rows(case: Case) -> list[str]:
    """The case's identity, one fact per line. The order id is the case id once it is known."""
    order = case.betex_pay_order_id or case.illunise_order_id
    rows = [f"\U0001f5c2 Case: {code(case_label(case))}"]
    if order and order != case_label(case):
        rows.append(f"\U0001f9fe Order: {code(order)}")
    rows.append(f"\U0001f4f1 Mobile: {code(case.mobile or '-')}")
    if case.amount is not None:
        rows.append(f"\U0001f4b0 Amount: {_money(case.amount, case.currency)}")
    return rows


def format_manual_review(case: Case, reason: str, extra: str = "") -> str:
    return para(
        "\u26a0\ufe0f " + b("MANUAL REVIEW NEEDED"),
        _ident_rows(case),
        "\u2757 " + _esc(reason),
        i(extra) if extra else "",
    )


def format_info(case: Case, title_text: str, detail: str) -> str:
    return para(
        "\u2139\ufe0f " + b(title_text),
        _ident_rows(case),
        "\n".join(_esc(x) for x in (detail or "").split("\n") if x),
    )


async def notify_admin(
    session: AsyncSession,
    *,
    kind: str,
    text: str,
    case: Case | None = None,
    dedupe_suffix: str = "",
    parse_mode: str | None = "HTML",
) -> Notification | None:
    """Send a notification to EVERY chat in ADMIN_NOTIFY_CHAT_ID, exactly once per (case, kind, suffix)."""
    s = get_settings()
    chats = s.notify_chats
    key = idempotency_key(case.case_id if case else "-", kind, dedupe_suffix)
    row = await reserve_notification(
        session,
        idempotency_key=key,
        kind=kind,
        text=text,
        case_id=case.case_id if case else None,
        chat_id=",".join(str(c) for c in chats),
    )
    if row is None:
        log.info("notification already sent; skipping", kind=kind, case_id=case.case_id if case else None)
        return None
    if not chats:
        row.error = "ADMIN_NOTIFY_CHAT_ID not configured"
        log.warning("ADMIN_NOTIFY_CHAT_ID not configured; notification stored only", kind=kind)
        return row
    errors = []
    for chat in chats:  # one failure never stops the others: each admin gets their own copy
        try:
            msg = await get_throttle().run(chat, lambda: get_bot().send_message(chat, text, parse_mode=parse_mode))
            row.sent = True
            row.telegram_message_id = row.telegram_message_id or msg.message_id
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{chat}: {str(exc)[:160]}")
            log.error("notification send failed", kind=kind, chat=chat, error=str(exc))
    if row.sent:
        await audit(session, "ADMIN_NOTIFIED", case_id=case.case_id if case else None, result=kind)
    if errors:
        row.error = "; ".join(errors)[:500]
    return row
