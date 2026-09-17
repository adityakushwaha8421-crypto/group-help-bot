"""Betix staff: "Not received We will notify you immediately of any updates." means the payment is still being
traced - PENDING. The case stays monitored, follow-ups continue, and the operator gets NO manual-review alert.
A plain "Not received" / "Reversed" / "fake image" is still a failure and still alerts.

Live 2026-09-11 (ILLUN-178911060967003, @tevy133333): the "will notify you" reply escalated the case by mistake."""

import pytest

from app.config import get_settings
from app.db.models import CaseStatus
from app.db.repository import get_case, list_followups
from app.telegram.betix_monitor import handle_group_message
from app.telegram.confirmation import classify_human_message
from tests.conftest import make_group_msg
from tests.test_authority import group_only, posted_case  # noqa: F401  (fixture)

PENDING_REPLIES = [
    "Not received We will notify you immediately of any updates.",
    "Not received yet team.We will notify you immediately of any updates.",
    "team, there is a delay with this order. Please ask the user to wait another 48 hours. "
    "If the payment is still not received by then, it will be refunded.",
    "not received yet",
    "We will update you shortly",
]
FAILED_REPLIES = [
    "Not received",
    "813968572638 Not received",
    "We checked and have not received it.",
    "Reversed sir",
    "fake image PI2608062rt1mjnshd0",
    "not ours",
]


def cls(text):
    s = get_settings()
    return classify_human_message(text, s.betex_order_id_pattern, s.plat_order_pattern).outcome


@pytest.mark.parametrize("text", PENDING_REPLIES)
def test_not_received_yet_is_pending(env, text):
    assert cls(text) == "PENDING"


@pytest.mark.parametrize("text", FAILED_REPLIES)
def test_a_plain_refusal_is_still_a_failure(env, text):
    assert cls(text) == "FAILED"


def test_not_received_with_a_request_for_proof_asks_for_evidence(env):
    assert cls("We have not received it yet. Please provide a video and bank statement.") == "NEED_MORE_EVIDENCE"


async def test_will_notify_you_keeps_monitoring_without_an_alert(
    db,
    fake_bot,
    fake_ai,
    order_search,
    no_download,
    fake_poster,
    group_only,  # noqa: F811
):
    case_id = await posted_case(db, order_search, fake_poster)
    sent_before = len(fake_bot.sent)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s,
            make_group_msg(700, PENDING_REPLIES[0], sender_id=5001, sender_username="tevy133333", reply_to=501),
        )
        assert r["outcome"] == "PENDING" and r["action"] == "recorded"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value and not c.followup_cancelled
        assert all(f.status == "scheduled" for f in await list_followups(s, case_id))
    assert len(fake_bot.sent) == sent_before  # no notification at all


async def test_plain_not_received_still_goes_to_manual_review(
    db,
    fake_bot,
    fake_ai,
    order_search,
    no_download,
    fake_poster,
    group_only,  # noqa: F811
):
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(700, "Not received", sender_id=5001, sender_username="tevy133333", reply_to=501)
        )
        assert r["action"] == "escalated_failed"
        assert (await get_case(s, case_id)).status == CaseStatus.ESCALATED.value
    assert any("MANUAL REVIEW NEEDED" in t for _, t in fake_bot.sent)
