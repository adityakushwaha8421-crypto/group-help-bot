"""A payment screenshot must actually SHOW a UTR / transaction id. If none is readable the operator is asked for a
clear screenshot, nothing is searched in Illunise and nothing is sent to Betix. A UTR is never taken from elsewhere
and never invented."""

from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence
from app.telegram.progress import progress_card
from tests.conftest import make_input
from tests.test_flow import GOOD, MOBILE, submit_four_messages

NO_UTR = {
    "amount": {"value": 6499.92, "confidence": 0.97},
    "payment_time": {"value": "2026-09-10 19:32:12", "confidence": 0.95},
    "payment_status": {"value": "Successful", "confidence": 0.9},
}
MESSAGE = "UTR Not Found ❌\n\nPlease send a clear payment screenshot where the UTR/Transaction ID is visible."


async def test_a_screenshot_without_a_utr_is_refused(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    fake_ai.screenshot = dict(NO_UTR)
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "utr_missing"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_INPUT.value  # still waiting, nothing searched
        assert c.betex_pay_order_id is None
        assert progress_card(c) == MESSAGE
    assert any(MESSAGE in t for _, t in fake_bot.sent)
    assert fake_poster.media == [] and fake_poster.texts == []  # nothing reached Betix


async def test_the_message_is_exactly_what_the_operator_asked_for():
    assert manager.UTR_NOT_FOUND == MESSAGE


async def test_a_clearer_screenshot_fixes_the_same_case(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    fake_ai.screenshot = dict(NO_UTR)
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "utr_missing"
    # the operator sends a clearer screenshot: SAME case, no new one
    fake_ai.screenshot = {**NO_UTR, "utr": {"value": "611532946151", "confidence": 0.96}}
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(9, "photo"))
        assert r.case.case_id == case_id and not r.created
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        c = await get_case(s, case_id)
        assert c.utr == "611532946151" and c.betex_pay_order_id == "ILLUN-178621243657290"


async def test_a_utr_on_the_statement_alone_is_not_enough(db, fake_bot, fake_ai, order_search, no_download):
    """The UTR must be visible on the SCREENSHOT: one read from another file does not count."""
    fake_ai.screenshot = dict(NO_UTR)
    case_id = await submit_four_messages(db)
    async with db.session_scope() as s:
        for ev in await list_evidence(s, case_id):
            if ev.type == "bank_statement":
                ev.analysis = {"extraction": {"utr": {"value": "611532946151", "confidence": 0.99}}}
        assert await manager.process_case(s, case_id, force=True) == "utr_missing"


async def test_force_send_does_not_skip_the_check(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    fake_ai.screenshot = dict(NO_UTR)
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", MOBILE))
        case_id = r.case.case_id
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True, force_send=True) == "utr_missing"
    assert fake_poster.media == []


async def test_a_readable_screenshot_still_flows(db, fake_bot, fake_ai, order_search, no_download):
    case_id = await submit_four_messages(db)  # the default fake screenshot carries a UTR
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"


async def test_the_check_can_be_switched_off(db, fake_bot, fake_ai, order_search, no_download, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("REQUIRE_SCREENSHOT_UTR", "false")
    reset_settings_cache()
    fake_ai.screenshot = dict(NO_UTR)
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) != "utr_missing"


async def test_the_operator_is_told_once(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    """The case's own live message carries the error - no second copy alongside it."""
    fake_ai.screenshot = dict(NO_UTR)
    case_id = await submit_four_messages(db)
    async with db.session_scope() as s:  # the case has its live message, as it does in real use
        c = await get_case(s, case_id)
        c.progress_chat_id, c.progress_message_id = 111, 4242
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "utr_missing"
    assert [t for _, t in fake_bot.sent if "UTR Not Found" in t] == []  # nothing sent separately
    said = [t for _, _, t in fake_bot.edits if "UTR Not Found" in t]
    assert said == [MESSAGE]  # the live message says it, exactly once


async def test_a_case_without_a_live_message_still_gets_told(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    fake_ai.screenshot = dict(NO_UTR)
    case_id = await submit_four_messages(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "utr_missing"
    assert [t for _, t in fake_bot.sent if "UTR Not Found" in t] == [MESSAGE]
