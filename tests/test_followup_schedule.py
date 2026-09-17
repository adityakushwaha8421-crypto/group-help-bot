"""Follow-up timing: replies to the SAME screenshot post at fixed offsets after posting (2 h, 8 h, 24 h, 48 h
in production), all cancelled the moment the payment is confirmed."""

from datetime import timedelta

from sqlalchemy import select

from app.cases import manager
from app.db.models import CaseStatus, Followup
from app.db.repository import get_case, list_followups
from app.followups.scheduler import sweep
from app.telegram.betix_monitor import handle_group_message
from app.telegram.progress import progress_card
from app.utils.timeutil import utcnow
from tests.conftest import make_group_msg
from tests.test_flow import SYS_OK, run_until_ready


async def posted(db, order_search, fake_poster, monkeypatch):
    monkeypatch.setenv("FOLLOWUP_SCHEDULE_MINUTES", "120,480,1440,2880")
    monkeypatch.setenv("ESCALATION_DELAY_MINUTES", "480")
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    from app.config import reset_settings_cache

    reset_settings_cache()
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    return case_id


async def fire(db, fake_poster, number):
    async with db.session_scope() as s:
        fu = (await s.execute(select(Followup).where(Followup.number == number))).scalar_one()
        fu.due_at = utcnow() - timedelta(seconds=1)
    return await sweep(lambda: fake_poster)


async def test_production_schedule_is_five_offsets_from_the_post(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    case_id = await posted(db, order_search, fake_poster, monkeypatch)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        fus = await list_followups(s, case_id)
        assert [f.number for f in fus] == [1, 2, 3, 4, 99]
        offsets = [(f.due_at - c.betix_posted_at) for f in fus]
        assert offsets[:4] == [timedelta(minutes=m) for m in (120, 480, 1440, 2880)]
        assert offsets[4] == timedelta(minutes=2880 + 480)  # manual review 8 h after the last follow-up


async def test_all_four_are_replies_to_the_screenshot_then_escalation(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    case_id = await posted(db, order_search, fake_poster, monkeypatch)
    for n in range(1, 5):
        assert await fire(db, fake_poster, n) == {"sent": 1}
        async with db.session_scope() as s:
            c = await get_case(s, case_id)
            assert c.followups_sent == n and c.status in (
                CaseStatus.FOLLOWUP_1_SENT.value,
                CaseStatus.FOLLOWUP_2_SENT.value,
            )
            assert f"🔄 Follow-ups sent: {n}" in progress_card(c)
    assert [t for t in fake_poster.texts] == [("Any update?", 501)] * 4  # same text, same anchor, four times
    assert await fire(db, fake_poster, 99) == {"escalated": 1}
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.ESCALATED.value
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW NEEDED" in t][0]
    assert "No confirmation after 4 follow-up(s)" in alert


async def test_confirmation_stops_the_remaining_followups_immediately(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    case_id = await posted(db, order_search, fake_poster, monkeypatch)
    assert await fire(db, fake_poster, 1) == {"sent": 1}
    assert await fire(db, fake_poster, 2) == {"sent": 1}
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r["action"] == "verified"
        assert [f.status for f in await list_followups(s, case_id)] == [
            "sent",
            "sent",
            "cancelled",
            "cancelled",
            "cancelled",
        ]
    # even if a row were re-armed by hand, the sweeper refuses after verification
    async with db.session_scope() as s:
        for f in (await s.execute(select(Followup))).scalars():
            f.status, f.due_at = "scheduled", utcnow() - timedelta(minutes=1)
    assert await sweep(lambda: fake_poster) == {"skipped": 5}
    assert len(fake_poster.texts) == 2
