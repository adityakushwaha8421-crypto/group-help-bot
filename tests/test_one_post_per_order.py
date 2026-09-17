"""One Illunise order id goes to the Betix group ONCE. A second case for the same order (the operator re-sends the
same payment, or two submissions are processed side by side) ends as ALREADY_SENT and posts nothing.

Live 2026-09-11: the same ₹5,699.85 payment was submitted twice 11 min apart; both cases matched
ILLUN-178904483482951 and both were posted, each with its own follow-up chain."""

from sqlalchemy import select

from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import CaseStatus, Followup
from app.db.repository import get_case
from app.telegram import summary
from app.telegram.progress import progress_card
from tests.conftest import make_input
from tests.test_flow import GOOD, MOBILE, run_until_ready

ORDER = "ILLUN-178621243657290"


async def submit_again(db, base: int) -> str:
    """The same payment submitted as a NEW case (new Telegram messages)."""
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(base + 1, "photo"))
        await attach_message(s, make_input(base + 2, "text", MOBILE))
        await attach_message(s, make_input(base + 3, "document", "Password:- lata2812"))
        await attach_message(s, make_input(base + 4, "video"))
        return r.case.case_id


async def process(db, case_id):
    async with db.session_scope() as s:
        return await manager.process_case(s, case_id, force=True)


async def post(db, case_id, poster):
    async with db.session_scope() as s:
        return await manager.post_case_to_betix(s, case_id, poster)


def screenshot_posts(poster):
    return [m for m in poster.media if m[0] == "payment_screenshot"]


async def test_resubmitted_payment_is_not_posted_twice(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    first, outcome = await run_until_ready(db, order_search, GOOD)
    assert outcome == "ready" and await post(db, first, fake_poster) == "posted"
    assert screenshot_posts(fake_poster) == [("payment_screenshot", ORDER)]

    second = await submit_again(db, 20)
    assert second != first
    order_search(GOOD)
    assert await process(db, second) == "already_sent"
    assert await post(db, second, fake_poster) == "already"  # refuses even if a job asks
    assert screenshot_posts(fake_poster) == [("payment_screenshot", ORDER)]  # still exactly one post

    async with db.session_scope() as s:
        c = await get_case(s, second)
        assert c.status == CaseStatus.ALREADY_SENT.value and c.betex_pay_order_id == ORDER
        assert f"Order {ORDER} was already sent to the Betix group" in c.failure_reason
        assert first not in c.failure_reason  # the operator only ever sees the order id
        assert c.betix_root_message_id is None
        fus = (await s.execute(select(Followup).where(Followup.case_id == second))).scalars().all()
        assert fus == []  # no second follow-up chain
        card = progress_card(c)
        assert "🔁 ALREADY WITH BETIX" in card and "not sent again" in card
        assert await manager.fail_case(s, c, "cancelled by operator") is False  # a finished case stays finished


async def test_two_submissions_racing_to_the_group_post_once(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """Both cases matched and became READY before either posted: the last gate (checked under the per-order lock)
    lets the first through and stops the second."""
    first, _ = await run_until_ready(db, order_search, GOOD)
    second = await submit_again(db, 20)
    order_search(GOOD)
    async with db.session_scope() as s:  # force the race: the second case also reached READY_FOR_BETIX
        c = await get_case(s, second)
        c.status, c.betex_pay_order_id = CaseStatus.READY_FOR_BETIX.value, ORDER
    assert await post(db, first, fake_poster) == "posted"
    assert await post(db, second, fake_poster) == "already_sent"
    assert len(screenshot_posts(fake_poster)) == 1
    async with db.session_scope() as s:
        assert (await get_case(s, second)).status == CaseStatus.ALREADY_SENT.value


async def test_an_earlier_case_that_never_posted_does_not_block(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """Only a real (or in-flight) post blocks. A case that matched but was escalated without posting does not."""
    first, _ = await run_until_ready(db, order_search, GOOD)
    async with db.session_scope() as s:
        c = await get_case(s, first)
        c.status = CaseStatus.ESCALATED.value  # e.g. the Telegram post failed
    second = await submit_again(db, 20)
    order_search(GOOD)
    assert await process(db, second) == "ready"
    assert await post(db, second, fake_poster) == "posted"
    assert len(screenshot_posts(fake_poster)) == 1


async def test_same_case_reposting_is_idempotent(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, _ = await run_until_ready(db, order_search, GOOD)
    assert await post(db, case_id, fake_poster) == "posted"
    assert await post(db, case_id, fake_poster) == "already"
    async with db.session_scope() as s:  # restart recovery re-runs the post job on a POSTED case
        c = await get_case(s, case_id)
        c.status = CaseStatus.POSTED_TO_BETIX.value
    await post(db, case_id, fake_poster)
    assert len(screenshot_posts(fake_poster)) == 1


def test_summary_counts_duplicates():
    from app.db.models import Case

    n = summary.counts([Case(status=CaseStatus.ALREADY_SENT.value), Case(status=CaseStatus.VERIFIED.value)])
    assert n["already_sent"] == 1 and n["verified"] == 1
