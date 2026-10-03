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
    wrong = [
        {**GOOD[0], "amount": 9000.0, "registration_number": "9000000001"},  # another customer's, another amount
        {**GOOD[0], "betex_order_id": B, "order_time": "2026-09-10 12:00:00"},  # hours before the payment
    ]
    manager.set_order_search(None)
    _, outcome = await run_until_ready(db, order_search, wrong)
    assert outcome == "escalated" and pi_queries(fake_poster) == {}


async def test_nearby_orders_are_named_never_no_order(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    """/pi is not possible (the screenshot prints no receiver UPI): the alert lists the nearby orders."""
    fake_ai.receiver_upi = None
    _, outcome = await run_until_ready(db, order_search, DOUBT_TWO)
    assert outcome == "escalated" and pi_queries(fake_poster) == {}
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW" in t][-1]
    assert "2 order(s)" in alert and "created just before the payment" in alert and "No order" not in alert
    assert alert.index(B) < alert.index(A)  # closest first
    assert "no payee UPI printed on the screenshot" in alert


async def test_an_order_paid_late_is_still_a_reasonable_candidate(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    """Created 45 min before the payment (outside the 30-min window, inside twice the window): too weak to be
    selected on its own, but it is ASKED about instead of going straight to Manual Review."""
    late = [{**GOOD[0], "order_time": "2026-09-10 18:47:00"}]
    case_id, outcome = await run_until_ready(db, order_search, late)
    assert outcome == "checking_upi" and list(pi_queries(fake_poster)) == [f"/pi {A}"]
    qid = (await query_ids(db, case_id))[A]
    assert (await answer(db, 900, pi_answer(A, "shoriful-5011@ptyes"), qid))["action"] == "pi_ready"


async def test_an_order_created_after_the_payment_is_never_asked(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    after = [{**GOOD[0], "order_time": "2026-09-10 19:50:00"}]
    _, outcome = await run_until_ready(db, order_search, after)
    assert outcome == "escalated" and pi_queries(fake_poster) == {}


# ---------------------------------------------------------------- the customer's own order, another amount
# Live 2026-10-03 (CASE-20261003-000039): order ₹300 created 18:47 under the customer's mobile, ₹330 paid at 18:48.
# It went to Manual Review as "no order" - it is a DOUBT: asked with /pi, and its UPI decides.
OWN_OTHER_AMOUNT = [{**GOOD[0], "amount": 6000.0, "status": "Expired"}]  # paid: ₹6,499.92


async def test_own_order_with_another_amount_is_checked_with_pi_and_matched_by_upi(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    case_id, outcome = await run_until_ready(db, order_search, OWN_OTHER_AMOUNT)
    assert outcome == "checking_upi"
    assert list(pi_queries(fake_poster)) == [f"/pi {A}"]
    qid = (await query_ids(db, case_id))[A]
    r = await answer(db, 900, pi_answer(A, "shoriful-5011@ptyes"), qid)
    assert r["action"] == "pi_ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.READY_FOR_BETIX.value and c.betex_pay_order_id == A
    assert not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)
    told = [t for _, t in fake_bot.sent if "amount differs" in t]
    assert len(told) == 1 and "₹6,000.00" in told[0] and "₹6,499.92" in told[0]


async def test_own_order_with_another_amount_and_another_upi_is_manual_review(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    case_id, _ = await run_until_ready(db, order_search, OWN_OTHER_AMOUNT)
    qid = (await query_ids(db, case_id))[A]
    await answer(db, 900, pi_answer(A, "someoneelse-7777@okaxis"), qid)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ORDER_MATCH_AMBIGUOUS.value and not c.betex_pay_order_id
    assert any("MANUAL REVIEW" in t for _, t in fake_bot.sent)
    assert not any("amount differs" in t for _, t in fake_bot.sent)


async def test_orders_that_fit_the_amount_are_asked_before_one_that_does_not(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    cands = [
        {**GOOD[0], "amount": 6000.0, "status": "Expired", "order_time": "2026-09-10 19:31:30"},  # nearest, other ₹
        {**GOOD[0], "illunise_order_id": "1003", "betex_order_id": B, "status": "Success", "utr": OTHER_UTR,
         "order_time": "2026-09-10 19:20:00"},
    ]  # fmt: skip
    case_id, outcome = await run_until_ready(db, order_search, cands)
    assert outcome == "checking_upi"
    assert list(pi_queries(fake_poster)) == [f"/pi {B}"]  # the amount fits: first, although it is further away
    await answer(db, 900, pi_answer(B, "someoneelse-7777@okaxis"), (await query_ids(db, case_id))[B])
    assert list(pi_queries(fake_poster)) == [f"/pi {B}", f"/pi {A}"]  # then the customer's other-amount order


async def test_another_customers_order_with_another_amount_is_never_asked(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    _, outcome = await run_until_ready(
        db, order_search, [{**GOOD[0], "amount": 6000.0, "registration_number": "9000000001"}]
    )
    assert outcome == "escalated" and pi_queries(fake_poster) == {}


async def test_own_order_of_another_amount_long_before_the_payment_is_never_asked(
    db, fake_bot, order_search, no_download, fake_poster, setup
):
    _, outcome = await run_until_ready(
        db, order_search, [{**GOOD[0], "amount": 6000.0, "order_time": "2026-09-10 17:00:00"}]
    )
    assert outcome == "escalated" and pi_queries(fake_poster) == {}


# ---------------------------------------------------------------- the Betix group is told (2026-10-03)
async def post_own_other_amount(db, order_search, fake_poster, ordered: float) -> str:
    case_id, _ = await run_until_ready(db, order_search, [{**GOOD[0], "amount": ordered, "status": "Expired"}])
    qid = (await query_ids(db, case_id))[A]
    await answer(db, 900, pi_answer(A, "shoriful-5011@ptyes"), qid)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    return case_id


def notes(fake_poster):
    return [(t, r) for t, r in fake_poster.texts if "User paid" in t]


async def test_the_betix_group_is_told_the_user_paid_extra(db, fake_bot, order_search, no_download, fake_poster, setup):
    case_id = await post_own_other_amount(db, order_search, fake_poster, 6000.0)  # paid ₹6,499.92
    async with db.session_scope() as s:
        root = (await get_case(s, case_id)).betix_root_message_id
    ((text, reply_to),) = notes(fake_poster)
    assert reply_to == root  # a reply to the screenshot post
    assert text.startswith("User paid extra amount.")
    assert "Order amount: ₹6,000.00" in text and "Paid amount: ₹6,499.92" in text and "Extra: ₹499.92" in text
    assert A in text
    async with db.session_scope() as s:  # posting again never repeats it
        from app.db.repository import list_evidence

        await fake_poster.post_case(s, await get_case(s, case_id), await list_evidence(s, case_id))
    assert len(notes(fake_poster)) == 1


async def test_the_betix_group_is_told_the_user_paid_less(db, fake_bot, order_search, no_download, fake_poster, setup):
    await post_own_other_amount(db, order_search, fake_poster, 7000.0)
    ((text, _),) = notes(fake_poster)
    assert text.startswith("User paid less amount.") and "Less: ₹500.08" in text


async def test_no_note_when_the_amounts_agree(db, fake_bot, order_search, no_download, fake_poster, setup):
    case_id, outcome = await run_until_ready(db, order_search, GOOD)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert notes(fake_poster) == []


def test_padded_paise_are_not_a_difference():
    from types import SimpleNamespace

    from app.telegram.betix_poster import amount_note

    case = SimpleNamespace(amount=13999.35, order_amount=14000.0, betex_pay_order_id=A)
    assert amount_note(case, 1.0) is None
    case.amount = 330.0
    case.order_amount = 300.0
    assert amount_note(case, 1.0).startswith("User paid extra amount.")
