"""The reminder loop as it actually runs in production: app.followups.scheduler.run_forever, ticking on its own,
sending each follow-up as a reply to the SAME screenshot post, and stopping the moment Betix confirms.

The production schedule is 2 h / 8 h / 24 h / 48 h; these tests run the same code on a seconds-long
schedule (FOLLOWUP_SCHEDULE_SECONDS) so the whole chain is watched end to end in about a second."""

import asyncio

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case, list_followups
from app.followups.scheduler import run_forever
from app.telegram.betix_monitor import handle_group_message
from tests.conftest import make_group_msg
from tests.test_flow import SYS_OK, run_until_ready

ROOT = 501  # the screenshot post every follow-up must reply to


async def posted(db, order_search, fake_poster, monkeypatch, *, seconds="0.2,0.4,0.6,0.8,1.0", esc="0.4"):
    monkeypatch.setenv("FOLLOWUP_SCHEDULE_SECONDS", seconds)
    monkeypatch.setenv("ESCALATION_DELAY_SECONDS", esc)
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    from app.config import reset_settings_cache

    reset_settings_cache()
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    return case_id


async def loop_for(fake_poster, seconds: float, *, every: float = 0.05):
    """Run the real sweeper loop for a while, exactly as app.main starts it."""
    stop = asyncio.Event()
    task = asyncio.create_task(run_forever(lambda: fake_poster, every, stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_the_loop_sends_all_five_reminders_as_replies_to_the_screenshot(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    case_id = await posted(db, order_search, fake_poster, monkeypatch)
    await loop_for(fake_poster, 1.3)
    assert fake_poster.texts == [("Any update?", ROOT)] * 5  # same text, same anchor, five times
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.followups_sent == 5 and c.status == CaseStatus.FOLLOWUP_2_SENT.value
        assert [f.status for f in await list_followups(s, case_id)] == ["sent"] * 5 + ["scheduled"]


async def test_the_reminders_arrive_in_order_over_time(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    """Not all at once: each one only goes out once its own due time has passed."""
    await posted(db, order_search, fake_poster, monkeypatch, seconds="0.3,0.9,1.5,2.1,2.7", esc="1")
    stop = asyncio.Event()
    task = asyncio.create_task(run_forever(lambda: fake_poster, 0.05, stop))
    await asyncio.sleep(0.6)  # wide margins: the whole suite may be loading the machine
    assert len(fake_poster.texts) == 1  # only the 0.3s one is due
    await asyncio.sleep(0.6)
    assert len(fake_poster.texts) == 2
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_confirmation_stops_the_remaining_reminders_at_once(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    case_id = await posted(db, order_search, fake_poster, monkeypatch)
    stop = asyncio.Event()
    task = asyncio.create_task(run_forever(lambda: fake_poster, 0.05, stop))
    await asyncio.sleep(0.5)
    sent_before = len(fake_poster.texts)
    assert sent_before >= 1
    async with db.session_scope() as s:  # Betix confirms
        r = await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=ROOT)
        )
        assert r["action"] == "verified"
    await asyncio.sleep(0.8)  # well past every remaining due time
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(fake_poster.texts) == sent_before  # nothing more went out
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.VERIFIED.value and c.followup_cancelled
        assert not any(f.status == "scheduled" for f in await list_followups(s, case_id))


async def test_the_loop_escalates_when_no_confirmation_ever_comes(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    case_id = await posted(db, order_search, fake_poster, monkeypatch)
    await loop_for(fake_poster, 1.8)  # last reminder at 1.0s, manual review 0.4s after it
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.ESCALATED.value
    assert any("MANUAL REVIEW NEEDED" in t for _, t in fake_bot.sent)


async def test_a_failing_pass_does_not_kill_the_loop(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    """One bad send must not stop every later reminder - the live failure mode we are guarding against."""
    case_id = await posted(db, order_search, fake_poster, monkeypatch)
    calls = {"n": 0}
    real_send = fake_poster._real._send_text

    async def flaky(text, reply_to=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Telegram timed out")
        return await real_send(text, reply_to)

    fake_poster._real._send_text = flaky
    monkeypatch.setenv("FOLLOWUP_RETRY_SECONDS", "0")  # retry on the very next sweep
    from app.config import reset_settings_cache

    reset_settings_cache()
    await loop_for(fake_poster, 1.3)
    assert calls["n"] > 1  # it kept trying after the failure
    assert fake_poster.texts == [("Any update?", ROOT)] * 5  # and every reminder still went out
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status != CaseStatus.ESCALATED.value  # one hiccup never ends the chain


async def test_reminders_missed_while_offline_go_out_as_one(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    """Live 2026-09-13: the Mac slept through #2 and #3. On wake-up only ONE "Any update?" may be posted."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.db.models import Followup
    from app.followups.scheduler import sweep
    from app.utils.timeutil import utcnow

    case_id = await posted(db, order_search, fake_poster, monkeypatch, seconds="600,1200,1800,2400,3000")
    async with db.session_scope() as s:
        for fu in (await s.execute(select(Followup).where(Followup.number.in_([1, 2, 3])))).scalars():
            fu.due_at = utcnow() - timedelta(minutes=60 - fu.number)  # all three overdue
    assert await sweep(lambda: fake_poster) == {"collapsed": 2, "sent": 1}
    assert fake_poster.texts == [("Any update?", ROOT)]  # one reply, to the screenshot
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        by_n = {f.number: f.status for f in await list_followups(s, case_id)}
        assert c.followups_sent == 3
        assert by_n == {1: "cancelled", 2: "cancelled", 3: "sent", 4: "scheduled", 5: "scheduled", 99: "scheduled"}
    assert await sweep(lambda: fake_poster) == {}  # nothing left due: no second burst
