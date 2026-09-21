"""Live progress card in the operator's chat.

The message the bot sends when processing starts ("🚀 Forced processing…" / "🎯 All evidence received…") is
remembered on the case (progress_chat_id / progress_message_id) and EDITED at every status change, so the
operator watches one message go: searching → order matched → sent to the Betix group → waiting for update →
confirmed (or manual review). Best-effort: a failed edit never breaks the case flow."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Case, CaseStatus
from app.telegram.ui import case_label, code, esc, i
from app.utils.logging import get_logger

log = get_logger("progress")
S = CaseStatus


def progress_card(case: Case) -> str:
    """The live message in the operator chat, rewritten at every step: heading, short detail lines, next step."""
    label = code(case_label(case))
    order = code(case.betex_pay_order_id or case.illunise_order_id or "-")
    ident = (
        f"\U0001f9fe Order: {order}"
        if case.betex_pay_order_id or case.illunise_order_id
        else f"\U0001f5c2 Case: {label}"
    )
    st = case.status
    reason = esc((case.failure_reason or "")[:200])

    if st == S.WAITING_FOR_INPUT.value:
        from app.cases.manager import AI_UNAVAILABLE, UTR_NOT_FOUND

        reason_text = case.failure_reason or ""
        if reason_text == UTR_NOT_FOUND:
            return UTR_NOT_FOUND
        if reason_text.startswith("NEWER STATEMENT NEEDED"):
            return esc(reason_text)  # the withdrawal date, where the statement ends, what to send
        if reason_text.startswith(AI_UNAVAILABLE):  # on hold: the AI service is down, the bot retries by itself
            return _card("\u23f8 ON HOLD", [ident], esc(reason_text))
        return _card("\u23f3 WAITING FOR EVIDENCE", [ident], "\U0001f4e9 Send the remaining files and I'll continue.")
    if st == S.ANALYZING_EVIDENCE.value:
        return _card("\U0001f9e0 READING EVIDENCE", [ident], "\u23f3 Pulling out the payment details...")
    if st == S.SEARCHING_ORDER.value and case.kind == "withdrawal":
        return _card(
            "\U0001f50e CHECKING WITHDRAWAL",
            [f"\U0001f4b8 Withdrawal: {code(case.withdrawal_id or '-')}"],
            "\u23f3 Reading the payout in Illunise and matching the statement's bank account...",
        )
    if st == S.SEARCHING_ORDER.value:
        return _card("\U0001f50e SEARCHING ILLUNISE", [ident], "\u23f3 Looking for the matching order...")
    if st == S.CHECKING_ORDER_UPI.value:
        return _card(
            "\U0001f3e6 CHECKING ORDER UPI",
            [ident],
            "\u23f3 A few orders look alike \u2014 checking each one's UPI with Betix.",
        )
    if st in (S.ORDER_MATCH_FOUND.value, S.READY_FOR_BETIX.value):
        if case.kind == "withdrawal":
            return _card(
                "\U0001f4b8 WITHDRAWAL READY",
                [f"\U0001f9fe Withdrawal: {order}"],
                "\U0001f4e4 Sending it to the Betix group...",
            )
        return _card(
            "\U0001f3af ORDER MATCHED",
            [f"\U0001f9fe Order: {order}"],
            "\U0001f4e4 Sending the evidence to the Betix group...",
        )
    if st == S.ORDER_MATCH_AMBIGUOUS.value:
        return _card(
            "\u26a0\ufe0f MULTIPLE ORDERS MATCH",
            [ident],
            "\U0001f440 Please pick the right one \u2014 the candidates are in the manual-review message.",
        )
    if st in (
        S.POSTED_TO_BETIX.value,
        S.WAITING_FOR_CONFIRMATION.value,
        S.FOLLOWUP_1_SENT.value,
        S.FOLLOWUP_2_SENT.value,
    ):
        sent = case.followups_sent or 0
        rows = [f"\U0001f9fe Order: {order}"]
        if sent:
            rows.append(f"\U0001f504 Follow-ups sent: {sent}")
        return _card("\U0001f4e4 SENT TO BETIX", rows, "\U0001f440 Waiting for Betix to confirm...")
    if st == S.VERIFIED.value and case.confirmation_type == "reversed":
        rows = [ident]
        if case.confirmed_by:
            rows.append(f"\U0001f464 Reported by: {esc(case.confirmed_by)}")
        return _card(
            "\U0001f504 WITHDRAWAL REVERSED",
            rows,
            "\u2705 Solved \u2014 Betix reversed this withdrawal.\n\U0001f4b8 Please pay the customer manually.",
        )
    if st == S.VERIFIED.value:
        rows = [f"\U0001f9fe Order: {order}"]
        if case.confirmed_by:
            rows.append(f"\u2705 Confirmed by: {esc(case.confirmed_by)}")
        return _card("\u2705 PAYMENT CONFIRMED", rows, "\U0001f64f Payment successfully confirmed.")
    if st == S.ALREADY_SUCCESS.value:
        return _card(
            "\u267b\ufe0f ALREADY SUCCESSFUL",
            [f"\U0001f9fe Order: {order}"],
            "\u2139\ufe0f Illunise already shows this order as paid \u2014 nothing sent to Betix.",
        )
    if st == S.ALREADY_SENT.value:
        return _card(
            "\U0001f501 ALREADY WITH BETIX",
            [f"\U0001f9fe Order: {order}"],
            "\u2139\ufe0f Sent earlier from another case \u2014 not sent again.\n"
            "\U0001f4e4 Need it sent anyway? Tap the button or use /push.",
        )
    if st == S.ESCALATED.value and (case.failure_reason or "").startswith("ACCOUNT DOES NOT MATCH"):
        return _card("\u274c ACCOUNT DOES NOT MATCH", [ident], f"\u2757 {i(reason)}\n\U0001f6ab Not sent to Betix.")
    if st == S.ESCALATED.value:
        return _card("\U0001f6a8 MANUAL REVIEW NEEDED", [ident], f"\u2757 {i(reason)}" if reason else "")
    return _card("\u26d4 CASE CLOSED", [ident], f"\u2757 {i(reason)}" if reason else "")


def _card(head: str, rows: list[str], foot: str) -> str:
    """Heading, a blank line, one detail per line, a blank line, the next step."""
    blocks = ["<b>" + head + "</b>", "\n".join(x for x in rows if x), foot]
    return "\n\n".join(x for x in blocks if x)


def card_buttons(case: Case):
    """The buttons under the card. Only "ALREADY WITH BETIX" has one: FORCE SEND. None removes any old button."""
    if case.status != S.ALREADY_SENT.value:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f4e4 Force send to Betix", callback_data=f"push:{case.case_id}")]
        ]
    )


async def push(session: AsyncSession, case: Case) -> bool:
    """Edit the case's progress message to reflect its current status. Returns True when an edit was sent."""
    if not case.progress_chat_id or not case.progress_message_id:
        return False
    from app.telegram import notifications

    try:
        text = progress_card(case)
        from app.telegram.throttle import get_throttle

        await get_throttle().run(
            case.progress_chat_id,
            lambda: notifications.get_bot().edit_message_text(
                text,
                chat_id=case.progress_chat_id,
                message_id=case.progress_message_id,
                parse_mode="HTML",
                reply_markup=card_buttons(case),
            ),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc):
            log.warning("progress edit failed", case_id=case.case_id, error=str(exc)[:200])
        return False
