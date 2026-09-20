"""Betix answers a withdrawal with "Reversed": the payout came back. The bot READS the payout and asks the operator;
only the operator's tap on "Refund now" makes it refund, and "solved" is reported only once the panel shows
Refunded. Wrong status, unknown id, failed or unconfirmed refund -> Manual Review, never "solved"."""

import pytest
from sqlalchemy import select

from app.admin.payouts import NOT_CONFIRMED, NOT_SUCCESS, REFUNDED, Payout, RefundResult
from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import AuditLog, CaseStatus
from app.db.repository import get_case
from app.telegram.betix_monitor import handle_group_message
from app.telegram.progress import progress_card
from tests.conftest import make_group_msg, make_input

WD = "WD-84425-67115"
pytestmark = pytest.mark.usefixtures("payout")


@pytest.fixture
def jobs(monkeypatch):
    """Queued jobs are recorded, and run by the test when it wants them to."""
    queued = []

    async def _enqueue(name, *args, **kw):
        queued.append((name, *args))

    monkeypatch.setattr(manager, "enqueue", _enqueue)
    return queued


@pytest.fixture
def refunder():
    calls = []
    holder = {
        "result": RefundResult(REFUNDED, "Success", "Refunded", "ok", Payout(WD, amount=4854.5, status="Refunded"))
    }

    async def _refund(wd):
        calls.append(wd)
        return holder["result"]

    manager.set_refunder(_refund)
    yield calls, holder
    manager.set_refunder(None)


async def reversed_case(db, fake_poster) -> str:
    async with db.session_scope() as s:
        cid = (await attach_message(s, make_input(1, "text", WD))).case.case_id
        await attach_message(s, make_input(2, "document"))
    async with db.session_scope() as s:
        await manager.process_case(s, cid, force=True)
        await manager.post_case_to_betix(s, cid, fake_poster)
        r = await handle_group_message(
            s, make_group_msg(602, f"Reversed BX{WD}", sender_username="Wendy", reply_to=501)
        )
        assert r["action"] == "reversal_check_queued"
    return cid


def texts(fake_bot, word):
    return [t for _, t in fake_bot.sent if word in t]


async def test_reversed_asks_the_operator_and_refunds_nothing(db, fake_bot, no_download, fake_poster, jobs, refunder):
    calls, _ = refunder
    cid = await reversed_case(db, fake_poster)
    assert ("reversal_check_job", cid) in jobs
    assert await manager.reversal_check(cid) == "asked"
    ask = texts(fake_bot, "REFUND NEEDED")
    assert len(ask) == 1 and WD in ask[0] and "Success" in ask[0] and "Wendy" in ask[0]
    assert calls == [] and not texts(fake_bot, "WITHDRAWAL REVERSED")  # nothing refunded, nothing "solved"
    assert ("refund_withdrawal_job", cid) not in jobs
    async with db.session_scope() as s:
        c = await get_case(s, cid)
        assert c.status != CaseStatus.VERIFIED.value and c.followup_cancelled  # Betix answered: no more follow-ups


async def test_the_tap_refunds_and_only_refunded_is_solved(db, fake_bot, no_download, fake_poster, jobs, refunder):
    calls, _ = refunder
    cid = await reversed_case(db, fake_poster)
    await manager.reversal_check(cid)
    async with db.session_scope() as s:
        assert await manager.approve_refund(s, cid, "@me") == "queued"
    assert ("refund_withdrawal_job", cid) in jobs
    assert await manager.refund_approved_withdrawal(cid) == "reversed"
    assert calls == [WD]  # the exact id of the case, once
    done = texts(fake_bot, "WITHDRAWAL REVERSED")
    assert len(done) == 1 and f"BX{WD}" in done[0] and "4,854.50" in done[0] and "Wendy" in done[0]
    assert "Solved" in done[0] and "pay the customer manually" in done[0] and "User ID: <code>" in done[0]
    async with db.session_scope() as s:
        c = await get_case(s, cid)
        assert c.status == CaseStatus.VERIFIED.value and "WITHDRAWAL REVERSED" in progress_card(c)
        row = (await s.execute(select(AuditLog).where(AuditLog.action == "WITHDRAWAL_REFUND"))).scalars().one()
        assert row.result == REFUNDED and row.actor == "@me"


async def test_a_second_tap_or_a_second_run_never_refunds_again(db, fake_bot, no_download, fake_poster, jobs, refunder):
    calls, _ = refunder
    cid = await reversed_case(db, fake_poster)
    await manager.reversal_check(cid)
    async with db.session_scope() as s:
        assert await manager.approve_refund(s, cid, "@me") == "queued"
    async with db.session_scope() as s:
        assert await manager.approve_refund(s, cid, "@me") == "already"
    assert await manager.refund_approved_withdrawal(cid) == "reversed"
    assert await manager.refund_approved_withdrawal(cid) == "skipped"
    async with db.session_scope() as s:
        assert await manager.approve_refund(s, cid, "@me") == "not_applicable"
    assert calls == [WD] and len(texts(fake_bot, "WITHDRAWAL REVERSED")) == 1


async def test_no_refund_without_the_operators_approval(db, fake_bot, no_download, fake_poster, jobs, refunder):
    calls, _ = refunder
    cid = await reversed_case(db, fake_poster)
    assert await manager.refund_approved_withdrawal(cid) == "skipped"  # a stray job: nobody tapped
    assert calls == []


async def test_no_refund_without_a_reversed_from_betix(db, fake_bot, no_download, fake_poster, jobs, refunder):
    calls, _ = refunder
    async with db.session_scope() as s:
        cid = (await attach_message(s, make_input(1, "text", WD))).case.case_id
        await attach_message(s, make_input(2, "document"))
    async with db.session_scope() as s:
        await manager.process_case(s, cid, force=True)
        await manager.post_case_to_betix(s, cid, fake_poster)
        assert await manager.approve_refund(s, cid, "@me") == "not_applicable"
    assert await manager.reversal_check(cid) == "skipped" and calls == []


async def test_already_refunded_is_solved_without_a_click(
    db, fake_bot, no_download, fake_poster, jobs, refunder, payout
):
    calls, _ = refunder
    cid = await reversed_case(db, fake_poster)
    payout(status="Refunded")
    assert await manager.reversal_check(cid) == "reversed"
    assert calls == [] and len(texts(fake_bot, "WITHDRAWAL REVERSED")) == 1 and not texts(fake_bot, "REFUND NEEDED")


@pytest.mark.parametrize("status", ["Processing", "Failed", "Pending"])
async def test_any_other_status_is_manual_review(
    db, fake_bot, no_download, fake_poster, jobs, refunder, payout, status
):
    calls, _ = refunder
    cid = await reversed_case(db, fake_poster)
    payout(status=status)
    assert await manager.reversal_check(cid) == "escalated"
    alert = texts(fake_bot, "MANUAL REVIEW NEEDED")[-1]
    assert f"status is {status}, not Success" in alert and calls == []
    assert not texts(fake_bot, "WITHDRAWAL REVERSED") and not texts(fake_bot, "REFUND NEEDED")


async def test_an_unknown_payout_is_manual_review(db, fake_bot, no_download, fake_poster, jobs, refunder, payout):
    cid = await reversed_case(db, fake_poster)
    payout(found=False)
    assert await manager.reversal_check(cid) == "escalated"
    assert "not found in Illunise payouts" in texts(fake_bot, "MANUAL REVIEW NEEDED")[-1]


@pytest.mark.parametrize(
    "result, words",
    [
        (
            RefundResult(NOT_CONFIRMED, "Success", "Success", "still Success 180s after Refund"),
            "does not show Refunded yet",
        ),
        (RefundResult(NOT_SUCCESS, "Processing", "Processing", ""), "Refund was NOT clicked"),
    ],
)
async def test_not_solved_unless_the_panel_shows_refunded(
    db, fake_bot, no_download, fake_poster, jobs, refunder, result, words
):
    _, holder = refunder
    holder["result"] = result
    cid = await reversed_case(db, fake_poster)
    await manager.reversal_check(cid)
    async with db.session_scope() as s:
        await manager.approve_refund(s, cid, "@me")
    assert await manager.refund_approved_withdrawal(cid) == "escalated"
    alert = texts(fake_bot, "MANUAL REVIEW NEEDED")[-1]
    assert "NOT refunded" in alert and words in alert and "Do not pay the customer" in alert
    assert not texts(fake_bot, "WITHDRAWAL REVERSED")
