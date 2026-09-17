"""Several Betix groups: all of them are monitored, a case is always answered in the group it was posted to,
and a new case goes to the default (first) group until a routing rule says otherwise."""

from app.cases import manager
from app.db.repository import get_case
from app.telegram.betix_monitor import is_betix_chat
from tests.test_flow import GOOD, run_until_ready

A, B = -1001111, -1002222


def groups(monkeypatch, value):
    monkeypatch.setenv("BETIX_GROUP_CHAT_ID", value)
    from app.config import get_settings, reset_settings_cache

    reset_settings_cache()
    return get_settings()


def test_the_setting_takes_a_list(monkeypatch):
    s = groups(monkeypatch, f"{A}, {B}")
    assert s.betix_chats == [A, B] and s.betix_chat == A and s.betix_chat_ids == {A, B}
    assert is_betix_chat(A) and is_betix_chat(B) and not is_betix_chat(-1003333)


async def test_a_new_case_goes_to_the_default_group(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        assert (await get_case(s, case_id)).betix_chat_id == -1009999  # the fake poster's default group
    assert set(fake_poster.chats) == {-1009999}


async def test_a_posted_case_is_answered_in_its_own_group(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    """The case lives in group A; the poster's default is B (say the operator switched defaults later)."""
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        c.betix_chat_id = A  # pretend it was posted in A
    fake_poster._real.default_chat = fake_poster._real.chat = B
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        await fake_poster.send_followup(s, c, 1)
        await fake_poster.send_statement_password(s, c) if c.statement_password else None
    assert fake_poster.chats[-1] == A  # not B


async def test_late_evidence_follows_the_case_group(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, _ = await run_until_ready(db, order_search, [dict(GOOD[0])])
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
        c = await get_case(s, case_id)
        c.betix_chat_id = A
    fake_poster._real.default_chat = fake_poster._real.chat = B
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        from app.db.repository import list_evidence

        for e in await list_evidence(s, case_id):
            e.posted_to_betix_message_id = None if e.type != "payment_screenshot" else e.posted_to_betix_message_id
        n = await manager.post_late_evidence(s, c, fake_poster)
    assert n >= 1 and set(fake_poster.chats[-n:]) == {A}
