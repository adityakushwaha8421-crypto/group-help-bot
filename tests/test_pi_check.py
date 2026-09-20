"""Several Illunise orders too close to call (same amount, created within the same couple of minutes before the
payment): check them ONE BY ONE, best candidate first - `/pi <ORDER-ID-1>`; if its "Order's UPI" fits the payee UPI
OCR'd from the payment screenshot (handle + visible ending) STOP and use it, the rest are never asked; only on a
mismatch ask the next. Then post screenshot + order id + statement + video and start confirmation / follow-ups.
Nothing fits, or no answer -> manual review. A clear order never triggers /pi.

User's example: screenshot `******5011@ptyes`, Order's UPI `shoriful-5011@ptyes`."""

import pytest

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case, list_case_betix_messages, list_followups
from app.telegram.betix_monitor import handle_group_message
from app.telegram.progress import progress_card
from app.telegram.upi_check import parse_pi_answer, upi_ending_match
from app.workers import tasks
from tests.conftest import make_group_msg
from tests.test_flow import GOOD, run_until_ready

A, B = "ILLUN-178621243657290", "ILLUN-178621243657291"
# Payment 19:32:12, ₹6,499.92. Both orders: same mobile, same amount, created 19:30:40 and 19:30:10.
TWINS = [
    dict(GOOD[0]),
    {**GOOD[0], "illunise_order_id": "1003", "betex_order_id": B, "order_time": "2026-09-10 19:30:10"},
]


def pi_answer(order_id, upi, status="Pending | 🔁CallbackStatus: Init"):
    return (
        f"Order's UPI: {upi}\n💵OrderAmount: 6500\n 💰PaidAmount: 0\n📄OrderStatus: {status}\n🧾UTR: NA\n"
        f"📌PlatOrderNo: PI2609103iuuugaluc4 ( 72 )\n📌MerchantOrderNo: {order_id}\n"
        "🕒CreatedTime: 2026-09-10 19:30:40 +05:30"
    )


@pytest.fixture
def setup(fake_ai, fake_poster, monkeypatch):
    manager.set_poster_factory(lambda: fake_poster)
    jobs = []

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        jobs.append((name, args, defer_seconds))

    monkeypatch.setattr("app.workers.queue.enqueue", fake_enqueue)
    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    monkeypatch.setattr(manager, "enqueue", fake_enqueue)  # manager binds it at import time
    fake_ai.receiver_upi = "******5011@ptyes"  # the "Paid to" UPI printed on the screenshot
    yield jobs
    manager.set_poster_factory(None)


def pi_queries(poster):
    return {text: mid for mid, (text, reply_to) in enumerate(poster.texts, start=502) if text.startswith("/pi ")}


async def answer(db, message_id, text, reply_to, *, bot=True):
    async with db.session_scope() as s:
        return await handle_group_message(
            s,
            make_group_msg(
                message_id, text, sender_username="betixpay_cs_bot" if bot else "someone", is_bot=bot, reply_to=reply_to
            ),
        )


async def query_ids(db, case_id):
    async with db.session_scope() as s:
        return {
            m.text.split()[1]: m.message_id
            for m in await list_case_betix_messages(s, case_id)
            if m.direction == "out" and m.kind == "pi_query"
        }


def test_parse_pi_answer_and_the_users_example():
    assert parse_pi_answer(pi_answer(A, "shoriful-5011@ptyes")) == (A, "shoriful-5011@ptyes")
    assert upi_ending_match("******5011@ptyes", "shoriful-5011@ptyes")[0]
    assert not upi_ending_match("******5011@ptyes", "rahul-7788@ptyes")[0]


def sent_pi(poster):
    return [t for t, _ in poster.texts if t.startswith("/pi ")]


async def test_first_candidate_matches_stop_immediately(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, outcome = await run_until_ready(db, order_search, TWINS)
    assert outcome == "checking_upi"
    assert sent_pi(fake_poster) == [f"/pi {A}"]  # only the best candidate is asked first
    assert fake_poster.media == []  # nothing posted to Betix yet
    assert ("pi_check_timeout_job", (case_id, A), 120) in setup
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.CHECKING_ORDER_UPI.value and c.pi_check_orders == [A, B]
        assert "checking each one's UPI with Betix" in progress_card(c)
    q = await query_ids(db, case_id)
    r = await answer(db, 700, pi_answer(A, "shoriful-5011@ptyes"), q[A])
    assert r["action"] == "pi_ready"
    assert sent_pi(fake_poster) == [f"/pi {A}"]  # STOP: B is never asked
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.READY_FOR_BETIX.value and c.betex_pay_order_id == A
    # betix_message_job hands the case to the poster: screenshot with the order id, then statement + video
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value
        assert len(await list_followups(s, case_id)) >= 2
    assert fake_poster.media[0] == ("payment_screenshot", A)
    assert {m[0] for m in fake_poster.media[1:]} == {"bank_statement", "payment_video"}


async def test_mismatch_moves_to_the_next_candidate(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    r1 = await answer(db, 700, pi_answer(A, "rahul-7788@ptyes"), q[A])
    assert r1["action"] == "pi_next" and sent_pi(fake_poster) == [f"/pi {A}", f"/pi {B}"]
    assert ("pi_check_timeout_job", (case_id, B), 120) in setup
    q = await query_ids(db, case_id)
    r2 = await answer(db, 701, pi_answer(B, "shoriful-5011@ptyes"), q[B])
    assert r2["action"] == "pi_ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).betex_pay_order_id == B


async def test_a_paid_pi_answer_is_not_a_confirmation(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    r = await answer(db, 700, pi_answer(A, "rahul-7788@ptyes", "Paid | 🔁CallbackStatus: Success"), q[A])
    assert r["action"] == "pi_next"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.CHECKING_ORDER_UPI.value and c.verified_at is None


async def test_no_order_fits_goes_to_manual_review(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    await answer(db, 700, pi_answer(A, "rahul-7788@ptyes"), q[A])
    q = await query_ids(db, case_id)
    r = await answer(db, 701, pi_answer(B, "amit-1234@ptyes"), q[B])
    assert r["action"] == "pi_ambiguous"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ORDER_MATCH_AMBIGUOUS.value and c.betex_pay_order_id is None
    alert = [t for _, t in fake_bot.sent if "all 2 close order(s) checked, none fits the screenshot UPI" in t][-1]
    assert "rahul-7788@ptyes" in alert and "amit-1234@ptyes" in alert and "5011@ptyes" in alert
    assert fake_poster.media == []


async def test_no_answer_for_one_order_moves_on_to_the_next(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    """Betix does not answer /pi for A: not Manual Review yet - B is asked, and B's UPI fits."""
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    assert await tasks.pi_check_timeout_job({}, case_id, A) == "next"
    assert sent_pi(fake_poster) == [f"/pi {A}", f"/pi {B}"]
    assert not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)
    q = await query_ids(db, case_id)
    r = await answer(db, 701, pi_answer(B, "shoriful-5011@ptyes"), q[B])
    assert r["action"] == "pi_ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).betex_pay_order_id == B


async def test_manual_review_only_after_every_candidate_was_asked(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    assert await tasks.pi_check_timeout_job({}, case_id, A) == "next"
    assert await tasks.pi_check_timeout_job({}, case_id, B) == "ambiguous"
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW" in t][-1]
    assert "all 2 close order(s) checked" in alert and "2 got no answer from Betix" in alert
    assert f"{A}: (no answer from Betix)" in alert and f"{B}: (no answer from Betix)" in alert


async def test_an_old_timeout_after_moving_on_is_ignored(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    await answer(db, 700, pi_answer(A, "rahul-7788@ptyes"), q[A])  # A answered (no match) -> B asked
    assert await tasks.pi_check_timeout_job({}, case_id, A) == "stale"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.CHECKING_ORDER_UPI.value
    # the restart-recovery form (no order id) times out the CURRENT query: B, the last one -> manual review
    assert await tasks.pi_check_timeout_job({}, case_id) == "ambiguous"
    assert any("1 got no answer from Betix" in t for _, t in fake_bot.sent)


def test_the_order_closest_to_the_payment_is_asked_first():
    """Live 2026-09-20, Rs 199 at 11:34: other customers' PAID Rs 200 orders from 11:28 outscored the customer's own
    expired 11:34 order by a hair and would have been asked first. Closest order time first; the score only
    breaks ties; an order holding the payment's UTR leads whatever its time."""
    from app.admin.matcher import Candidate, MatchResult, Scored

    def sc(oid, score, lead, utr=None):
        sig = {"amount": {"score": 1.0}, "time": {"score": 0.98, "lead_minutes": lead}, "utr": {"score": utr}}
        return Scored(Candidate(oid, oid), score, sig)

    far, near, best = sc("FAR", 0.95, 4.0), sc("NEAR", 0.95, 1.0), sc("BEST", 0.97, 6.0)
    r = MatchResult("AMBIGUOUS", best, near, [far, best, near], "x")
    assert [c.candidate.betex_order_id for c in manager.close_candidates(r)] == ["NEAR", "FAR", "BEST"]
    tie = sc("TIE", 0.97, 1.0)
    r = MatchResult("AMBIGUOUS", best, near, [far, best, near, tie], "x")
    assert [c.candidate.betex_order_id for c in manager.close_candidates(r)][:2] == ["TIE", "NEAR"]
    held = sc("HOLDS-UTR", 0.96, 9.0, utr=1.0)
    r = MatchResult("AMBIGUOUS", best, near, [far, best, near, held], "x")
    assert manager.close_candidates(r)[0].candidate.betex_order_id == "HOLDS-UTR"


async def test_unreadable_screenshot_upi_skips_pi_and_alerts(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    fake_ai.receiver_upi = None
    case_id, outcome = await run_until_ready(db, order_search, TWINS)
    assert outcome == "ambiguous" and fake_poster.texts == []  # no /pi noise in the group
    assert any("UPI check not possible: no payee UPI printed on the screenshot" in t for _, t in fake_bot.sent)


async def test_a_clear_order_never_asks_pi(db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup):
    _, outcome = await run_until_ready(db, order_search, GOOD)
    assert outcome == "ready" and fake_poster.texts == []


async def test_a_human_reply_to_our_pi_changes_nothing(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    r = await answer(db, 700, "done", q[A], bot=False)
    assert r["action"] == "pi_reply"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.CHECKING_ORDER_UPI.value and c.reviewer_confirmed_at is None


# ---------------------------------------------------------------- /pi cleanup after the order is confirmed
async def test_matched_pi_and_its_bot_reply_are_deleted_before_posting(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    assert (await answer(db, 700, pi_answer(A, "shoriful-5011@ptyes"), q[A]))["action"] == "pi_ready"
    assert fake_poster.deleted == [q[A], 700]  # the /pi request, then the Betix bot's direct reply
    assert fake_poster.media == []  # deleted first; the evidence is posted afterwards
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media[0] == ("payment_screenshot", A)


async def test_each_pi_exchange_is_deleted_as_soon_as_its_check_is_done(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    """After EACH check: our `/pi <ORDER-ID>` and the Betix bot's direct reply to it - and nothing else."""
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    await answer(db, 700, pi_answer(A, "rahul-7788@ptyes"), q[A])  # no match -> cleaned up, B asked
    assert fake_poster.deleted == [q[A], 700]
    q = await query_ids(db, case_id)
    await answer(db, 701, pi_answer(B, "shoriful-5011@ptyes"), q[B])  # match -> cleaned up too
    assert fake_poster.deleted == [q[A], 700, q[B], 701]
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).betex_pay_order_id == B  # what was asked and answered is kept by us


async def test_only_our_pi_and_the_system_bots_direct_reply_are_deleted(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    await answer(db, 650, "please wait sir", q[A], bot=False)  # a PERSON replies to our /pi: never touched
    async with db.session_scope() as s:  # another bot replies to it as well: never touched either
        await handle_group_message(
            s, make_group_msg(651, "hello", sender_username="some_other_bot", is_bot=True, reply_to=q[A])
        )
        await handle_group_message(
            s, make_group_msg(652, "unrelated notice", sender_username="betixpay_cs_bot", is_bot=True)
        )
    await answer(db, 700, pi_answer(A, "shoriful-5011@ptyes"), q[A])
    assert fake_poster.deleted == [q[A], 700]
    assert not {650, 651, 652} & set(fake_poster.deleted)


async def test_an_unanswered_pi_is_removed_when_the_check_moves_on(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    assert await tasks.pi_check_timeout_job({}, case_id, A) == "next"
    assert fake_poster.deleted == [q[A]]  # our own message; there was no reply to delete


async def test_matched_mode_keeps_the_old_behaviour(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup, monkeypatch
):
    from app.config import reset_settings_cache

    monkeypatch.setenv("PI_CLEANUP", "matched")
    reset_settings_cache()
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    await answer(db, 700, pi_answer(A, "rahul-7788@ptyes"), q[A])  # no match -> B asked
    q = await query_ids(db, case_id)
    await answer(db, 701, pi_answer(B, "shoriful-5011@ptyes"), q[B])
    assert fake_poster.deleted == [q[B], 701]  # A's /pi and its reply stay


async def test_cleanup_all_and_off(db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("PI_CLEANUP", "all")
    reset_settings_cache()
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    await answer(db, 700, pi_answer(A, "rahul-7788@ptyes"), q[A])
    q = await query_ids(db, case_id)
    await answer(db, 701, pi_answer(B, "shoriful-5011@ptyes"), q[B])
    assert sorted(fake_poster.deleted) == sorted([q[A], q[B], 700, 701])


async def test_cleanup_off_keeps_everything(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup, monkeypatch
):
    from app.config import reset_settings_cache

    monkeypatch.setenv("PI_CLEANUP", "off")
    reset_settings_cache()
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    assert (await answer(db, 700, pi_answer(A, "shoriful-5011@ptyes"), q[A]))["action"] == "pi_ready"
    assert fake_poster.deleted == []


async def test_undeletable_bot_reply_never_blocks_the_flow(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    """Our bot is not a group admin: its own /pi goes, the Betix bot's reply cannot - the case still proceeds."""
    fake_poster.undeletable.add(700)
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    q = await query_ids(db, case_id)
    assert (await answer(db, 700, pi_answer(A, "shoriful-5011@ptyes"), q[A]))["action"] == "pi_ready"
    assert fake_poster.deleted == [q[A]]
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
