"""PAYMENT CONFIRMED must reach the operator for EVERY verified case (2026-09-29).

A valid confirmation (Betix system bot OR a human Betix member) verifies the case, cancels the remaining
follow-ups and sends the final notification exactly once. Nothing in between - a failed Telegram send, a
confirmation that arrives while the post is still in flight, a crashed job - may lose it."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.cases import manager
from app.db.models import CaseStatus, Notification
from app.db.repository import get_case, list_followups
from app.followups.scheduler import sweep
from app.telegram.betix_monitor import handle_group_message
from app.telegram.confirmation import classify_human_message
from app.workers import tasks
from tests.conftest import make_group_msg
from tests.test_confirmation import BX, PL
from tests.test_flow import SYS_OK, run_until_ready

ROOT = 501


@pytest.fixture
def either(monkeypatch):
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    monkeypatch.setenv("FOLLOWUP_SCHEDULE_SECONDS", "600,1200")
    from app.config import reset_settings_cache

    reset_settings_cache()


async def posted(db, order_search, fake_poster):
    case_id, outcome = await run_until_ready(db, order_search)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    return case_id


def confirmed_texts(fake_bot):
    return [t for _, t in fake_bot.sent if "PAYMENT CONFIRMED" in t]


async def test_a_failed_send_is_retried_by_the_sweep_and_sent_once(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, either
):
    case_id = await posted(db, order_search, fake_poster)
    real = fake_bot.send_message
    down = {"on": True}

    async def flaky(chat_id, text, **kw):
        if down["on"]:
            raise RuntimeError("Network is unreachable")
        return await real(chat_id, text, **kw)

    fake_bot.send_message = flaky
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=ROOT)
        )
        assert r["action"] == "verified"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.VERIFIED.value and c.followup_cancelled  # verified even though the send failed
        row = (await s.execute(select(Notification).where(Notification.kind == "payment_confirmed"))).scalar_one()
        assert not row.sent and "unreachable" in row.error
    assert confirmed_texts(fake_bot) == []

    await sweep(lambda: fake_poster)  # still down: logged, still unsent
    assert confirmed_texts(fake_bot) == []
    down["on"] = False
    r = await sweep(lambda: fake_poster)
    assert r.get("notifications_resent") == 1
    assert len(confirmed_texts(fake_bot)) == 1 and "ILLUN-178621243657290" in confirmed_texts(fake_bot)[0]
    await sweep(lambda: fake_poster)
    assert len(confirmed_texts(fake_bot)) == 1  # exactly once
    async with db.session_scope() as s:
        row = (await s.execute(select(Notification).where(Notification.kind == "payment_confirmed"))).scalar_one()
        assert row.sent and row.error is None


async def test_a_verified_case_without_its_notification_gets_it_from_the_sweep(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, either
):
    case_id = await posted(db, order_search, fake_poster)
    async with db.session_scope() as s:
        await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=ROOT)
        )
    assert len(confirmed_texts(fake_bot)) == 1
    async with db.session_scope() as s:  # the notification row vanished (an old bug, a manual DB fix, ...)
        for row in (await s.execute(select(Notification))).scalars():
            await s.delete(row)
    r = await sweep(lambda: fake_poster)
    assert r.get("notified_late") == 1
    assert len(confirmed_texts(fake_bot)) == 2
    await sweep(lambda: fake_poster)
    assert len(confirmed_texts(fake_bot)) == 2  # and never again
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.VERIFIED.value


async def test_a_confirmation_that_comes_before_the_post_is_applied_once_the_case_is_with_betix(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, either
):
    """The Betix bot names the order id while our post is still in flight (READY_FOR_BETIX): the confirmation is
    recorded; the sweep verifies the case and sends PAYMENT CONFIRMED as soon as the post is done."""
    case_id, outcome = await run_until_ready(db, order_search)
    assert outcome == "ready"
    async with db.session_scope() as s:
        r = await handle_group_message(s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True))
        assert r["case_id"] == case_id and r["action"] == "confirmation_recorded"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.READY_FOR_BETIX.value and c.system_confirmed_at is not None
    assert confirmed_texts(fake_bot) == []
    await sweep(lambda: fake_poster)  # not with Betix yet: nothing to apply
    assert confirmed_texts(fake_bot) == []
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    r = await sweep(lambda: fake_poster)
    assert r.get("verified_late") == 1
    assert len(confirmed_texts(fake_bot)) == 1 and "Betix System" in confirmed_texts(fake_bot)[0]
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.VERIFIED.value and c.followup_cancelled
        assert not any(f.status == "scheduled" for f in await list_followups(s, case_id))
    await sweep(lambda: fake_poster)
    assert len(confirmed_texts(fake_bot)) == 1


async def test_a_human_member_confirming_with_the_order_id_in_the_text_verifies_too(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, either, monkeypatch
):
    monkeypatch.setenv("AI_CLASSIFY_UNKNOWN_BETIX_REPLIES", "false")  # the regex alone must get it
    from app.config import reset_settings_cache

    reset_settings_cache()
    case_id = await posted(db, order_search, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(602, "ILLUN-178621243657290 done ✅", sender_username="wendy", sender_id=42)
        )
        assert r["action"] == "verified", r
    texts = confirmed_texts(fake_bot)
    assert len(texts) == 1 and "@wendy" in texts[0]
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.VERIFIED.value


def test_human_success_with_ids_around_the_word():
    for t in [
        "ILLUN-178621243657290 done ✅",
        "@fantasyAdda_support PI260823h7r4pvcjbp confirmed",
        "Success - ILLUN-178621243657290",
        "UTR 611532946151 received",
    ]:
        assert classify_human_message(t, BX, PL).outcome == "SUCCESS", t
    assert classify_human_message("ILLUN-178621243657290 not received", BX, PL).outcome != "SUCCESS"


async def test_a_crashed_betix_message_job_is_retried(db, fake_bot, monkeypatch):
    calls, queued = [], []

    async def boom(session, msg):
        calls.append(msg.message_id)
        raise RuntimeError("database is locked")

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        queued.append((name, args, job_id, defer_seconds))

    monkeypatch.setattr(tasks, "handle_group_message", boom)
    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    payload = make_group_msg(700, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True).as_dict()
    assert (await tasks.betix_message_job({}, payload))["action"] == "retry"
    assert queued[-1][0] == "betix_message_job" and queued[-1][1] == (payload, 1) and queued[-1][3] == 15
    assert (await tasks.betix_message_job({}, payload, 1))["action"] == "retry"
    with pytest.raises(RuntimeError):  # the last attempt fails loudly
        await tasks.betix_message_job({}, payload, 2)
    assert calls == [700, 700, 700]


async def test_the_sweep_leaves_old_and_reversed_cases_alone(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, either
):
    case_id = await posted(db, order_search, fake_poster)
    async with db.session_scope() as s:
        await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=ROOT)
        )
        for row in (await s.execute(select(Notification))).scalars():
            await s.delete(row)
        c = await get_case(s, case_id)
        c.verified_at = c.verified_at - timedelta(days=10)  # long closed: not the sweep's business
    assert (await sweep(lambda: fake_poster)).get("notified_late") is None
    assert len(confirmed_texts(fake_bot)) == 1
