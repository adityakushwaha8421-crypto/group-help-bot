"""/bulk reminder: "Any update?" goes as a reply under EVERY posted case still waiting for an answer - and only those."""

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case
from app.followups.service import bulk_reminder
from tests.test_flow import GOOD, run_until_ready


async def posted(db, order_search, fake_poster):
    case_id, _ = await run_until_ready(db, order_search, GOOD)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        return case_id, (await get_case(s, case_id)).betix_root_message_id


async def test_every_pending_case_is_reminded_under_its_own_post(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id, root = await posted(db, order_search, fake_poster)
    before = len(fake_poster.texts)
    res = await bulk_reminder(lambda: fake_poster, actor="@me")
    assert len(res["sent"]) == 1 and not res["failed"]
    assert fake_poster.texts[before:] == [("Any update?", root)]  # a reply to the case's own post
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value and c.followups_sent == 0  # the chain is untouched
    # asked again: sent again (the operator's word, not the automatic chain)
    assert len((await bulk_reminder(lambda: fake_poster))["sent"]) == 1
    assert len(fake_poster.texts) == before + 2


async def test_a_case_escalated_for_silence_is_still_reminded(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id, root = await posted(db, order_search, fake_poster)
    async with db.session_scope() as s:
        await manager.escalate_case(s, await get_case(s, case_id), "No confirmation after 2 follow-up(s).")
    before = len(fake_poster.texts)
    assert len((await bulk_reminder(lambda: fake_poster))["sent"]) == 1
    assert fake_poster.texts[before:] == [("Any update?", root)]


async def test_solved_and_never_posted_cases_get_nothing(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, _ = await run_until_ready(db, order_search, GOOD)  # matched, never posted
    res = await bulk_reminder(lambda: fake_poster)
    assert not res["sent"] and not res["failed"] and not fake_poster.texts

    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    async with db.session_scope() as s:
        await manager.verify_case(s, await get_case(s, case_id), confirmed_by="@betix")
    before = len(fake_poster.texts)
    res = await bulk_reminder(lambda: fake_poster)
    assert not res["sent"] and len(fake_poster.texts) == before


def test_the_command_is_registered_and_documented():
    from app.telegram.input_bot import BOT_COMMANDS, help_text

    assert any(c == "bulk" for c, _ in BOT_COMMANDS) and "/bulk reminder" in help_text()
