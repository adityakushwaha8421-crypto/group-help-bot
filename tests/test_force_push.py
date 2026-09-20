"""FORCE SEND: a case that ended as "ALREADY WITH BETIX" is sent anyway - but only on the operator's word
(/push, or the button under the card). Nothing else can be pushed, and never twice."""

from sqlalchemy import select

from app.cases import manager
from app.db.models import AuditLog, CaseStatus, Followup
from app.db.repository import get_case
from app.telegram.progress import card_buttons, progress_card
from tests.test_flow import GOOD, run_until_ready
from tests.test_one_post_per_order import ORDER, post, process, screenshot_posts, submit_again


async def held_back_case(db, order_search, fake_poster) -> tuple[str, str]:
    first, _ = await run_until_ready(db, order_search, GOOD)
    assert await post(db, first, fake_poster) == "posted"
    second = await submit_again(db, 20)
    order_search(GOOD)
    assert await process(db, second) == "already_sent"
    return first, second


async def test_the_card_offers_the_button_only_when_already_with_betix(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    first, second = await held_back_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        held, sent = await get_case(s, second), await get_case(s, first)
        button = card_buttons(held).inline_keyboard[0][0]
        assert button.callback_data == f"push:{second}" and "Force send" in button.text
        assert "/push" in progress_card(held)
        assert card_buttons(sent) is None  # a case that is simply waiting for Betix has no button


async def test_force_push_sends_the_case_and_follows_it_up(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    _, second = await held_back_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        assert await manager.force_push(s, second, fake_poster, actor="@me") == "posted"
    assert screenshot_posts(fake_poster) == [("payment_screenshot", ORDER)] * 2  # sent again, on request
    async with db.session_scope() as s:
        c = await get_case(s, second)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value and c.betix_root_message_id
        assert c.failure_reason is None and card_buttons(c) is None  # the button is gone
        assert "SENT TO BETIX" in progress_card(c)
        assert (await s.execute(select(Followup).where(Followup.case_id == second))).scalars().first() is not None
        audit = (await s.execute(select(AuditLog).where(AuditLog.action == "FORCE_PUSH"))).scalars().one()
        assert audit.actor == "@me" and audit.result == ORDER


async def test_a_second_tap_does_nothing(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    _, second = await held_back_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        assert await manager.force_push(s, second, fake_poster) == "posted"
    async with db.session_scope() as s:
        assert await manager.force_push(s, second, fake_poster) == "not_applicable"
    assert len(screenshot_posts(fake_poster)) == 2


async def test_only_an_already_sent_case_can_be_pushed(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    first, _ = await held_back_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        assert await manager.force_push(s, first, fake_poster) == "not_applicable"  # it is already in the group
        assert await manager.force_push(s, "CASE-NOPE", fake_poster) == "missing"
    assert len(screenshot_posts(fake_poster)) == 1


async def test_the_automatic_path_still_never_sends_twice(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    _, second = await held_back_case(db, order_search, fake_poster)
    assert await post(db, second, fake_poster) == "already"
    assert len(screenshot_posts(fake_poster)) == 1
