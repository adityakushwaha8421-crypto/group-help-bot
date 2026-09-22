"""/stopr ORDER-ID: every pending reminder of that case is cancelled; the case and its evidence stay as they are."""

from sqlalchemy import select

from app.cases import manager
from app.db.models import CaseStatus, Followup
from app.db.repository import get_case, list_evidence
from app.followups.scheduler import sweep
from tests.test_flow import GOOD, run_until_ready

ORDER = GOOD[0]["betex_order_id"]


async def test_stopr_cancels_the_reminders_and_keeps_the_case(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    from app.followups.service import cancel_case_followups

    case_id, _ = await run_until_ready(db, order_search, GOOD)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        pending = (
            (await s.execute(select(Followup).where(Followup.case_id == case_id, Followup.status == "scheduled")))
            .scalars()
            .all()
        )
        assert pending  # reminders were scheduled
        before = len(await list_evidence(s, case_id))
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        n = await cancel_case_followups(s, c, "stopped by @me (/stopr)")
        assert n == len(pending)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.followup_cancelled and c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value  # still watched
        assert c.betix_root_message_id and len(await list_evidence(s, case_id)) == before  # nothing else changed
        assert (
            not (await s.execute(select(Followup).where(Followup.case_id == case_id, Followup.status == "scheduled")))
            .scalars()
            .all()
        )
    sent_before = len(fake_poster.texts)
    async with db.session_scope() as s:  # even a reminder that somehow comes due sends nothing
        from datetime import timedelta

        from app.utils.timeutil import utcnow

        for f in (await s.execute(select(Followup).where(Followup.case_id == case_id))).scalars():
            f.status, f.due_at = "scheduled", utcnow() - timedelta(minutes=1)
    await sweep(lambda: fake_poster)
    assert len(fake_poster.texts) == sent_before


def test_the_command_is_registered_and_documented():
    from app.telegram.input_bot import BOT_COMMANDS, help_text

    assert any(c == "stopr" for c, _ in BOT_COMMANDS) and "/stopr" in help_text()
