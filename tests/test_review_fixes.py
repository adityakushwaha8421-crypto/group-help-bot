"""Bugs found in the project review."""

from sqlalchemy import select

from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import Case, CaseStatus
from app.db.repository import get_case
from app.telegram import input_bot
from app.telegram.betix_monitor import handle_group_message
from app.workers.tasks import recover
from tests.conftest import make_group_msg, make_input
from tests.test_flow import GOOD, MOBILE, SYS_FAIL, SYS_OK, run_until_ready


async def test_chatter_text_does_not_open_a_case(db):
    async with db.session_scope() as s:
        for word in ("ok", "hi team", "please check", "thanks"):
            r = await attach_message(s, make_input(len(word), "text", word))
            assert r.duplicate and r.case is None
        assert (await s.execute(select(Case))).scalars().all() == []
        # but a mobile number, a UTR or an order id still opens one
        r = await attach_message(s, make_input(50, "text", MOBILE))
        assert r.created and r.case.mobile == MOBILE
    r = await input_bot.ingest(make_input(60, "text", "ok"))  # chatter never joins an open case either
    assert r["duplicate"]
    async with db.session_scope() as s:
        c = await get_case(s, r["case"].case_id)
        assert c.processing_version == 0  # ...so it does not re-arm the timers


async def test_already_success_case_ignores_betix_messages(db, fake_bot, fake_ai, order_search, no_download):
    case_id, outcome = await run_until_ready(db, order_search, [{**GOOD[0], "status": "Success"}])
    assert outcome == "already_success"
    async with db.session_scope() as s:
        ok = await handle_group_message(s, make_group_msg(700, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True))
        bad = await handle_group_message(
            s, make_group_msg(701, SYS_FAIL, sender_username="betixpay_cs_bot", is_bot=True)
        )
        assert ok["action"] in ("terminal", "unlinked") and bad["action"] in ("terminal", "unlinked")
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ALREADY_SUCCESS.value
        assert await manager.verify_case(s, c, confirmed_by="x") is False
    assert not any("Payment confirmed" in t or "NEEDS A MANUAL CHECK" in t for _, t in fake_bot.sent)


async def test_recovery_processes_a_complete_but_unprocessed_case(db, monkeypatch):
    sent = []

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        sent.append((name, args, defer_seconds))

    monkeypatch.setattr("app.workers.tasks.enqueue", fake_enqueue)
    async with db.session_scope() as s:
        c = (await attach_message(s, make_input(1, "photo"))).case
        for inp in (make_input(2, "text", MOBILE), make_input(3, "document"), make_input(4, "video")):
            await attach_message(s, inp)
        half = (await attach_message(s, make_input(9, "photo", chat_id=222, user_id=222))).case  # incomplete
    counts = await recover()
    assert counts.get("process") == 1
    jobs = [j for j in sent if j[0] == "process_case_job"]
    assert len(jobs) == 1 and jobs[0][1][0] == c.case_id
    assert not any(j[1][0] == half.case_id for j in sent)
