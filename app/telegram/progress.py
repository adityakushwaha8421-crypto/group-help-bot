"""Live progress card in the operator's chat.

The message the bot sends when processing starts ("🚀 Forced processing…" / "🎯 All evidence received…") is
remembered on the case (progress_chat_id / progress_message_id) and EDITED at every status change, so the
operator watches one message go: searching → order matched → sent to the Betix group → waiting for update →
confirmed (or manual review). Best-effort: a failed edit never breaks the case flow."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.gateway import gateway_pending, is_native
from app.db.models import GATEWAY_BETIX, GATEWAY_NATIVE, Case, CaseStatus
from app.telegram.ui import case_label, code, esc, i
from app.utils.logging import get_logger

log = get_logger("progress")
S = CaseStatus


SENT_STATUSES = {
    S.POSTED_TO_BETIX.value,
    S.WAITING_FOR_CONFIRMATION.value,
    S.FOLLOWUP_1_SENT.value,
    S.FOLLOWUP_2_SENT.value,
}


def customer_rows(case: Case) -> list[str]:
    """Who the case is for: the customer (clickable) and their Telegram user id, when known."""
    rows = []
    name = " ".join(x for x in (case.original_first_name, case.original_last_name) if x).strip()
    if case.original_username:
        rows.append(f"\U0001f464 Customer: @{esc(case.original_username.lstrip('@'))}")
    elif case.original_user_id:
        link = f'<a href="tg://user?id={case.original_user_id}">{esc(name or "open chat")}</a>'
        rows.append(f"\U0001f464 Customer: {link}")
    if case.original_user_id:
        rows.append(f"\U0001f194 User ID: {code(str(case.original_user_id))}")
    return rows


def progress_card(case: Case) -> str:
    """The live message in the operator chat, rewritten at every step: heading, short detail lines, next step.
    Once the evidence was SENT (the "📤 SENT TO ..." card, while waiting for the answer) the card also names the
    customer and their user id; the earlier and later cards do not."""

    def card(head: str, rows: list[str], foot: str) -> str:
        return _card(head, rows + (customer_rows(case) if case.status in SENT_STATUSES else []), foot)

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
            return card("\u23f8 ON HOLD", [ident], esc(reason_text))
        return card("\u23f3 WAITING FOR EVIDENCE", [ident], "\U0001f4e9 Send the remaining files and I'll continue.")
    if st == S.ANALYZING_EVIDENCE.value:
        return card("\U0001f9e0 READING EVIDENCE", [ident], "\u23f3 Pulling out the payment details...")
    if st == S.SEARCHING_ORDER.value and case.kind == "withdrawal":
        return card(
            "\U0001f50e CHECKING WITHDRAWAL",
            [f"\U0001f4b8 Withdrawal: {code(case.withdrawal_id or '-')}"],
            "\u23f3 Reading the payout in Illunise and matching the statement's bank account...",
        )
    if st == S.SEARCHING_ORDER.value:
        return card("\U0001f50e SEARCHING ILLUNISE", [ident], "\u23f3 Looking for the matching order...")
    if st == S.CHECKING_ORDER_UPI.value:
        return card(
            "\U0001f3e6 CHECKING ORDER UPI",
            [ident],
            "\u23f3 A few orders look alike \u2014 checking each one's UPI with Betix.",
        )
    if st in (S.ORDER_MATCH_FOUND.value, S.READY_FOR_BETIX.value):
        if case.kind == "withdrawal":
            return card(
                "\U0001f4b8 WITHDRAWAL READY",
                [f"\U0001f9fe Withdrawal: {order}"],
                "\U0001f4e4 Sending it to the Betix group...",
            )
        if is_native(case):
            return card(
                "\U0001f3af ORDERS FOUND" if len(native_ids(case)) > 1 else "\U0001f3af ORDER MATCHED",
                [native_orders_line(case), "\U0001f500 Gateway: Native"],
                "\U0001f4e4 Sending the screenshot to the Native group...",
            )
        return card(
            "\U0001f3af ORDER MATCHED",
            [f"\U0001f9fe Order: {order}"],
            "\U0001f4e4 Sending the evidence to the Betix group...",
        )
    if st == S.ORDER_MATCH_AMBIGUOUS.value:
        return card(
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
        if is_native(case):
            return card(
                "\U0001f4e4 SENT TO NATIVE GROUP",
                [native_orders_line(case), "\U0001f500 Gateway: Native"],
                "\U0001f440 Waiting for the Native group to confirm...",
            )
        sent = case.followups_sent or 0
        rows = [f"\U0001f9fe Order: {order}"]
        if sent:
            rows.append(f"\U0001f504 Follow-ups sent: {sent}")
        return card("\U0001f4e4 SENT TO BETIX", rows, "\U0001f440 Waiting for Betix to confirm...")
    if st == S.VERIFIED.value and case.confirmation_type == "reversed":
        rows = [ident]
        if case.confirmed_by:
            rows.append(f"\U0001f464 Reported by: {esc(case.confirmed_by)}")
        return card(
            "\U0001f504 WITHDRAWAL REVERSED",
            rows,
            "\u2705 Solved \u2014 Betix reversed this withdrawal.\n\U0001f4b8 Please pay the customer manually.",
        )
    if st == S.VERIFIED.value:
        rows = [native_orders_line(case) if is_native(case) else f"\U0001f9fe Order: {order}"]
        if case.confirmed_by:
            rows.append(f"\u2705 Confirmed by: {esc(case.confirmed_by)}")
        return card("\u2705 PAYMENT CONFIRMED", rows, "\U0001f64f Payment successfully confirmed.")
    if st == S.ALREADY_SUCCESS.value:
        return card(
            "\u267b\ufe0f ALREADY SUCCESSFUL",
            [f"\U0001f9fe Order: {order}"],
            "\u2139\ufe0f Illunise already shows this order as paid \u2014 nothing sent to Betix.",
        )
    if st == S.ALREADY_SENT.value:
        return card(
            "\U0001f501 ALREADY WITH BETIX",
            [f"\U0001f9fe Order: {order}"],
            "\u2139\ufe0f Sent earlier from another case \u2014 not sent again.\n"
            "\U0001f4e4 Need it sent anyway? Tap the button or use /push.",
        )
    if st == S.ESCALATED.value and (case.failure_reason or "").startswith("ACCOUNT DOES NOT MATCH"):
        return card("\u274c ACCOUNT DOES NOT MATCH", [ident], f"\u2757 {i(reason)}\n\U0001f6ab Not sent to Betix.")
    if st == S.ESCALATED.value:
        return card("\U0001f6a8 MANUAL REVIEW NEEDED", [ident], f"\u2757 {i(reason)}" if reason else "")
    return card("\u26d4 CASE CLOSED", [ident], f"\u2757 {i(reason)}" if reason else "")


def native_ids(case: Case) -> list[str]:
    """The order id(s) of a Native case: all of them when several were created at the payment time."""
    ids = [x for x in (case.native_order_ids or []) if x]
    return ids or [x for x in (case.betex_pay_order_id or case.illunise_order_id,) if x]


def native_orders_line(case: Case) -> str:
    ids = native_ids(case)
    if len(ids) > 1:
        return "\U0001f9fe Orders: " + ", ".join(code(x) for x in ids)
    return f"\U0001f9fe Order: {code(ids[0]) if ids else '-'}"


def _card(head: str, rows: list[str], foot: str) -> str:
    """Heading, a blank line, one detail per line, a blank line, the next step."""
    blocks = ["<b>" + head + "</b>", "\n".join(x for x in rows if x), foot]
    return "\n\n".join(x for x in blocks if x)


def gateway_buttons(case: Case):
    """[Betix] [Native] - the question the card asks before anything is processed."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="\U0001f7e6 Betix", callback_data=f"gw:{GATEWAY_BETIX}:{case.case_id}"),
                InlineKeyboardButton(text="\U0001f7e9 Native", callback_data=f"gw:{GATEWAY_NATIVE}:{case.case_id}"),
            ]
        ]
    )


def card_buttons(case: Case, missing: list[str] | None = None):
    """The buttons under the card: [Betix] [Native] while the gateway question is open (`missing` = what the case
    still lacks, so the screenshot + mobile are known to be in), FORCE SEND under "ALREADY WITH BETIX". None
    removes any old button."""
    if missing is not None and case.status == S.WAITING_FOR_INPUT.value and gateway_pending(case, missing):
        return gateway_buttons(case)
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

    try:
        missing = None
        if case.status == S.WAITING_FOR_INPUT.value and case.kind != "withdrawal" and case.gateway_choice is None:
            from app.cases.correlation import missing_items
            from app.db.repository import list_evidence

            missing = missing_items(case, await list_evidence(session, case.case_id))
            if gateway_pending(case, missing):
                # The question is still open: the card keeps asking it (text from input_bot, buttons here).
                from app.telegram.input_bot import case_card

                text = case_card(
                    case, await list_evidence(session, case.case_id), missing, created=False, forwarded=False
                )
                await _edit(case, text, gateway_buttons(case))
                return True
        text = progress_card(case)
        await _edit(case, text, card_buttons(case))
        return True
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc):
            log.warning("progress edit failed", case_id=case.case_id, error=str(exc)[:200])
        return False


async def _edit(case: Case, text: str, reply_markup) -> None:
    from app.telegram import notifications
    from app.telegram.throttle import get_throttle

    await get_throttle().run(
        case.progress_chat_id,
        lambda: notifications.get_bot().edit_message_text(
            text,
            chat_id=case.progress_chat_id,
            message_id=case.progress_message_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
        ),
    )
