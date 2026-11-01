"""The processing message in the operator chat is edited live: searching -> order matched -> sent to the Betix
group -> waiting for update -> confirmed / manual review."""

from app.cases import manager
from app.db.repository import get_case
from app.telegram import progress
from app.telegram.betix_monitor import handle_group_message
from tests.conftest import make_group_msg, make_input
from tests.test_flow import GOOD, SYS_OK, submit_four_messages

CHAT, MSG = 111, 4242


async def tracked_case(db):
    case_id = await submit_four_messages(db)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        c.progress_chat_id, c.progress_message_id = CHAT, MSG
    return case_id


def texts(fake_bot):
    assert all(chat == CHAT and mid == MSG for chat, mid, _ in fake_bot.edits)
    return [t for _, _, t in fake_bot.edits]


async def test_progress_message_follows_the_case(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    from app.config import reset_settings_cache

    reset_settings_cache()
    case_id = await tracked_case(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
    t = texts(fake_bot)
    assert any("SEARCHING ILLUNISE" in x for x in t)
    assert (
        "🎯 ORDER MATCHED" in t[-1]
        and "🧾 Order: <code>ILLUN-178621243657290</code>" in t[-1]
        and "Sending the evidence to the Betix group" in t[-1]
    )
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
    last = texts(fake_bot)[-1]
    assert "📤 SENT TO BETIX" in last and "👀 Waiting for Betix to confirm" in last
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r["action"] == "verified"
    last = texts(fake_bot)[-1]
    assert "✅ PAYMENT CONFIRMED" in last and "Payment successfully confirmed" in last
    assert last.startswith("<b>✅ PAYMENT CONFIRMED</b>")


async def test_escalation_shows_manual_review(db, fake_bot, fake_ai, order_search, no_download):
    case_id = await tracked_case(db)
    order_search([])
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "escalated"
    last = texts(fake_bot)[-1]
    assert "🚨 MANUAL REVIEW NEEDED" in last


async def test_no_progress_message_means_no_edits(db, fake_bot, fake_ai, order_search, no_download):
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        await manager.process_case(s, case_id, force=True)
    assert fake_bot.edits == []


def test_card_renders_every_status(env):
    from app.db.models import Case, CaseStatus

    for st in CaseStatus:
        c = Case(
            case_id="CASE-1",
            status=st.value,
            mobile="7733931348",
            betex_pay_order_id="ILLUN-1",
            failure_reason="x",
            confirmed_by="Betix",
        )
        text = progress.progress_card(c)
        assert "ILLUN-1" in text and len(text) > 20  # the order id is the visible case id


async def test_the_sent_card_names_the_customer_and_their_user_id_the_others_do_not(db):
    """Operator request 2026-11-01: the user id on the card once the evidence was successfully sent - the
    "SENT TO BETIX" / "SENT TO NATIVE GROUP" card - and nowhere else."""
    from app.cases.correlation import ForwardOrigin, attach_message
    from app.telegram.progress import progress_card

    rahul = ForwardOrigin(user_id=90001, username="rahul_fa", first_name="Rahul", last_name="Sharma")
    priya = ForwardOrigin(user_id=90002, username=None, first_name="Priya", last_name=None)
    async with db.session_scope() as s:
        a = (await attach_message(s, make_input(1, "photo", chat_id=111, user_id=111, forward=rahul))).case
        await attach_message(s, make_input(2, "text", "9876543210", chat_id=111, user_id=111, forward=rahul))
        a.betex_pay_order_id = "ILLUN-179351597046463"
        for st in ("POSTED_TO_BETIX", "WAITING_FOR_CONFIRMATION", "FOLLOWUP_1_SENT", "FOLLOWUP_2_SENT"):
            a.status = st
            text = progress_card(a)
            assert "👤 Customer: @rahul_fa" in text and "🆔 User ID: <code>90001</code>" in text, st
            assert (
                text.index("Order: <code>ILLUN-179351597046463</code>") < text.index("Customer") < text.index("Waiting")
            )
        for st in ("WAITING_FOR_INPUT", "SEARCHING_ORDER", "ORDER_MATCH_FOUND", "READY_FOR_BETIX", "VERIFIED",
                   "ESCALATED", "ALREADY_SENT", "ALREADY_SUCCESS", "FAILED"):  # fmt: skip
            a.status = st
            assert "User ID" not in progress_card(a) and "Customer" not in progress_card(a), st
        a.status, a.gateway_choice = "WAITING_FOR_CONFIRMATION", "native"
        assert "SENT TO NATIVE GROUP" in progress_card(a) and "🆔 User ID: <code>90001</code>" in progress_card(a)
        b = (await attach_message(s, make_input(3, "photo", chat_id=222, user_id=222, forward=priya))).case
        b.status = "WAITING_FOR_CONFIRMATION"
        text = progress_card(b)
        assert '<a href="tg://user?id=90002">Priya</a>' in text and "🆔 User ID: <code>90002</code>" in text
