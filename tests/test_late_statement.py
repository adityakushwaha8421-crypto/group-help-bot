"""A bank statement (or video) that arrives for an EXISTING case always reaches the Betix group as a REPLY to that
case's payment-screenshot post - never as a new case, never standalone, and never lost to another case.

Live 2026-09-13, ILLUN-178913660461696: the case was force-sent without its statement and confirmed by Betix four
seconds later. The statement then arrived and (a) a confirmed case was not a valid target, (b) the previous,
already-escalated case of the same submitter - which had its own statement - swallowed it instead."""

from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence
from app.telegram import input_bot
from app.telegram.betix_monitor import handle_group_message
from tests.conftest import make_group_msg, make_input
from tests.test_flow import GOOD, MOBILE, SYS_OK
from tests.test_force_send import jobs  # noqa: F401  (fixture: enqueue recorded, nothing runs)

ROOT = 501  # the screenshot post in FakePoster: every later file must reply to it


async def confirmed_case(db, order_search, fake_poster, monkeypatch):
    """Screenshot + mobile + video, force-sent without the statement, then confirmed by Betix."""
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    from app.config import reset_settings_cache

    reset_settings_cache()
    order_search(GOOD)
    async with db.session_scope() as s:
        case_id = (await attach_message(s, make_input(1, "photo"))).case.case_id
        await attach_message(s, make_input(2, "text", MOBILE))
        await attach_message(s, make_input(4, "video"))  # no statement: the 30 s force-send window ran out
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True, force_send=True) == "ready"
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        r = await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=ROOT)
        )
        assert r["action"] == "verified"
    return case_id


async def test_statement_after_confirmation_is_posted_as_a_reply_to_the_screenshot(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch, jobs
):
    case_id = await confirmed_case(db, order_search, fake_poster, monkeypatch)
    assert [k for k, _ in fake_poster.media_replies] == ["payment_screenshot", "payment_video"]
    r = await input_bot.ingest(make_input(9, "document"))  # the statement, after Betix confirmed
    assert r["case"].case_id == case_id and not r["created"] and r["late_post"]
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.VERIFIED.value  # confirmation untouched
        await manager.post_late_evidence(s, c, fake_poster)
        ev = await list_evidence(s, case_id)
        assert [e.type for e in ev if e.telegram_message_id == 9] == ["bank_statement"]
        assert all(e.posted_to_betix_message_id for e in ev)
    assert fake_poster.media_replies[-1] == ("bank_statement", ROOT)  # a reply to the screenshot, not standalone


async def test_statement_goes_to_the_case_missing_one_not_the_older_case_that_has_one(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    """Two submissions minutes apart. The first (with its own statement) escalated before posting; the second
    was posted without a statement. The next statement belongs to the second."""
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    from app.config import reset_settings_cache

    reset_settings_cache()
    order_search([])  # nothing in Illunise for the first one
    async with db.session_scope() as s:
        first = (await attach_message(s, make_input(1, "photo"))).case
        await attach_message(s, make_input(2, "text", "8739987133"))
        await attach_message(s, make_input(3, "document"))  # its own statement
        await attach_message(s, make_input(4, "video"))
    async with db.session_scope() as s:
        assert await manager.process_case(s, first.case_id, force=True) == "escalated"
    order_search(GOOD)
    async with db.session_scope() as s:
        second = (await attach_message(s, make_input(6, "photo"))).case
        assert second.case_id != first.case_id
        await attach_message(s, make_input(7, "text", MOBILE))
        await attach_message(s, make_input(8, "video"))
    async with db.session_scope() as s:
        assert await manager.process_case(s, second.case_id, force=True, force_send=True) == "ready"
        assert await manager.post_case_to_betix(s, second.case_id, fake_poster) == "posted"
    r = await input_bot.ingest(make_input(9, "document"))  # the second payment's statement
    assert r["case"].case_id == second.case_id and r["late_post"]
    async with db.session_scope() as s:
        assert [e.telegram_message_id for e in await list_evidence(s, first.case_id) if e.type == "bank_statement"] == [
            3
        ]
        c = await get_case(s, second.case_id)
        await manager.post_late_evidence(s, c, fake_poster)
    assert fake_poster.media_replies[-1] == ("bank_statement", ROOT)


async def test_a_closed_case_that_never_reached_betix_never_takes_late_evidence(
    db, fake_bot, fake_ai, order_search, no_download
):
    order_search([])
    async with db.session_scope() as s:
        first = (await attach_message(s, make_input(1, "photo"))).case
        await attach_message(s, make_input(2, "text", MOBILE))
        await attach_message(s, make_input(4, "video"))
    async with db.session_scope() as s:
        assert await manager.process_case(s, first.case_id, force=True, force_send=True) == "escalated"
    r = await input_bot.ingest(make_input(9, "document"))
    assert r["created"] and r["case"].case_id != first.case_id  # a fresh case, not the dead one


async def test_batch_handles_the_screenshot_before_a_statement_sent_first(db, fake_bot, jobs):
    """The operator forwards the PDF a moment before the screenshot: still one case, with everything on it."""
    sink = []

    class Chat:
        def __init__(self, inp):
            self.chat = type("c", (), {"id": inp.chat_id})()
            self.message_id = inp.message_id

        async def answer(self, text, **kw):
            sink.append(text)
            return type("m", (), {"message_id": 900})()

    for i in (make_input(3, "document"), make_input(1, "photo"), make_input(2, "text", MOBILE), make_input(4, "video")):
        input_bot.hold(Chat(i), i)
    input_bot._flush_tasks[111].cancel()
    touched = await input_bot.flush_chat(111)
    assert len(touched) == 1 and len(sink) == 1
    async with db.session_scope() as s:
        ev = await list_evidence(s, touched[0])
        assert sorted(e.type for e in ev) == ["bank_statement", "payment_screenshot", "payment_video"]
