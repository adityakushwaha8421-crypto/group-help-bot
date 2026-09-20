"""An order that Illunise shows as Success WITH THIS PAYMENT'S UTR has been credited: it is never posted to Betix,
the case ends as ALREADY_SUCCESS. A Success status alone does not reject the order - it goes to Betix."""

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case
from app.telegram import order_search as osx
from app.telegram.progress import progress_card
from tests.conftest import make_input
from tests.test_flow import GOOD, MOBILE, run_until_ready

DONE = [{**GOOD[0], "status": "Success", "utr": "611532946151"}]  # the screenshot's UTR
SUCCESS_ONLY = [{**GOOD[0], "status": "Success"}]


async def test_success_order_is_not_posted(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, outcome = await run_until_ready(db, order_search, DONE)
    assert outcome == "already_success"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ALREADY_SUCCESS.value
        assert c.betex_pay_order_id == "ILLUN-178621243657290"  # the match itself is recorded
        assert "already Success" in c.failure_reason
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "already"  # refuses to post
    assert fake_poster.media == [] and fake_poster.texts == []
    assert not any("Payment confirmed" in t or "NEEDS A MANUAL CHECK" in t for _, t in fake_bot.sent)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        text = progress_card(c)
        assert "♻️ ALREADY SUCCESSFUL" in text and "nothing sent to Betix" in text


async def test_pending_order_still_posts(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, outcome = await run_until_ready(db, order_search, GOOD)  # GOOD[0] is Pending
    assert outcome == "ready"


async def test_status_list_is_configurable(db, fake_bot, fake_ai, order_search, no_download, monkeypatch):
    monkeypatch.setenv("ALREADY_SUCCESS_STATUSES", "completed")
    from app.config import reset_settings_cache

    reset_settings_cache()
    _, outcome = await run_until_ready(db, order_search, DONE)  # "Success" is no longer in the list
    assert outcome == "ready"


async def test_search_command_flags_an_already_successful_order(db, fake_ai, order_search, tmp_path):
    async def fake_download(file_id):
        p = tmp_path / "s.jpg"
        p.write_bytes(b"x")
        return p

    osx.set_downloader(fake_download)
    try:
        order_search(DONE)
        sess = osx.start(111, 111, MOBILE)
        osx.take(sess, make_input(1, "photo"))
        reply = await osx.run(sess)
        assert "♻️ Illunise already shows this order as successful" in reply
    finally:
        osx.set_downloader(None)
        osx._sessions.clear()


async def test_a_finished_case_cannot_be_failed_afterwards(db):
    """Live 2026-09-11: two /cancel commands sent while the case was being re-run landed after it ended in
    ALREADY_SUCCESS and flipped it to FAILED. Cancelling never touches a case that ended well."""
    from app.cases import manager
    from app.db.models import Case, CaseStatus

    async with db.session_scope() as s:
        c = Case(case_id="CASE-X", status=CaseStatus.WAITING_FOR_INPUT.value, source_chat_id=1, source_user_id=1)
        s.add(c)
        await s.flush()
        c.status = CaseStatus.ALREADY_SUCCESS.value
        assert await manager.fail_case(s, c, "cancelled by operator", actor="@op") is False
        assert c.status == CaseStatus.ALREADY_SUCCESS.value and c.failure_reason is None
        c.status = CaseStatus.VERIFIED.value
        assert await manager.fail_case(s, c, "x") is False and c.status == CaseStatus.VERIFIED.value


async def test_a_success_status_alone_does_not_stop_the_order(db, fake_bot, fake_ai, order_search, no_download):
    """Status must not by itself reject a strong match: without the payment's UTR on it, Betix verifies it."""
    case_id, outcome = await run_until_ready(db, order_search, SUCCESS_ONLY)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).betex_pay_order_id == "ILLUN-178621243657290"
