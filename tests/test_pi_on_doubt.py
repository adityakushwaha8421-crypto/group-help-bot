"""ANY DOUBT -> ask Betix. No order clears the bar on its own, but one or more fit the amount and were created
before the payment inside the window: their UPI is checked with /pi one by one, closest to the payment first, and
the FIRST that fits the screenshot's receiver UPI is the order - the rest are never asked. A clear match still
needs no /pi; an order that fits neither amount nor time is never asked about."""

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case
from tests.test_flow import GOOD, run_until_ready
from tests.test_pi_check import answer, pi_answer, pi_queries, query_ids, setup  # noqa: F401

A, B = "ILLUN-178621243657290", "ILLUN-178621243657291"
OTHER_UTR = "999999999999"  # the panel holds ANOTHER UTR for the order: the score is capped, the order is in doubt
DOUBT_ONE = [{**GOOD[0], "status": "Success", "utr": OTHER_UTR}]
DOUBT_TWO = [
    {**GOOD[0], "status": "Success", "utr": OTHER_UTR, "order_time": "2026-09-10 19:25:00"},
    {**GOOD[0], "illunise_order_id": "1003", "betex_order_id": B, "status": "Success", "utr": OTHER_UTR,
     "order_time": "2026-09-10 19:31:00"},
]  # fmt: skip


async def test_a_single_doubtful_order_is_checked_with_pi(db, fake_bot, order_search, no_download, fake_poster, setup):
    case_id, outcome = await run_until_ready(db, order_search, DOUBT_ONE)
    assert outcome == "checking_upi"
    assert list(pi_queries(fake_poster)) == [f"/pi {A}"]
    qid = (await query_ids(db, case_id))[A]
    r = await answer(db, 900, pi_answer(A, "shoriful-5011@ptyes"), qid)
    assert r["action"] == "pi_ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.READY_FOR_BETIX.value and c.betex_pay_order_id == A
    assert not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)


async def test_the_closest_order_is_asked_first_and_a_match_stops_the_check(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    case_id, outcome = await run_until_ready(db, order_search, DOUBT_TWO)
    assert outcome == "checking_upi"
    assert list(pi_queries(fake_poster)) == [f"/pi {B}"]  # created 1 min before the payment: asked first
    qid = (await query_ids(db, case_id))[B]
    await answer(db, 900, pi_answer(B, "shoriful-5011@ptyes"), qid)
    assert list(pi_queries(fake_poster)) == [f"/pi {B}"]  # matched: the other order is never asked
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).betex_pay_order_id == B


async def test_no_upi_fits_is_manual_review(db, fake_bot, order_search, no_download, fake_poster, setup):
    case_id, _ = await run_until_ready(db, order_search, DOUBT_ONE)
    qid = (await query_ids(db, case_id))[A]
    await answer(db, 900, pi_answer(A, "someoneelse-7777@okaxis"), qid)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.ORDER_MATCH_AMBIGUOUS.value
    assert any("MANUAL REVIEW" in t for _, t in fake_bot.sent)


async def test_a_clear_match_needs_no_pi(db, fake_bot, order_search, no_download, fake_poster, setup):
    _, outcome = await run_until_ready(db, order_search, GOOD)
    assert outcome == "ready" and pi_queries(fake_poster) == {}


async def test_an_order_that_fits_neither_amount_nor_time_is_never_asked(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    wrong = [{**GOOD[0], "amount": 9000.0}, {**GOOD[0], "betex_order_id": B, "order_time": "2026-09-10 12:00:00"}]
    manager.set_order_search(None)
    _, outcome = await run_until_ready(db, order_search, wrong)
    assert outcome == "escalated" and pi_queries(fake_poster) == {}
