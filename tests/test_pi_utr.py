"""The UTR is the primary reference in the /pi check, and a bank app's receipt without a payee UPI is no obstacle.

Several orders look alike and the Betix bot is asked about each (`/pi <ORDER-ID>`). Its answer carries the order's
UPI AND the UTR Betix holds on it. Betix holding THIS payment's UTR on an order settles it - whatever the UPI
reads, and even when the screenshot (the Kotak app's receipt, for one) prints no UPI at all. The payee UPI is read
by OCR: when no order's UPI fits letter for letter, one that is only a character or three off is that UPI."""

from app.db.models import CaseStatus
from app.db.repository import get_case
from app.telegram.upi_check import parse_pi_utr
from tests.test_flow import run_until_ready
from tests.test_pi_check import TWINS, answer, pi_answer, query_ids, sent_pi, setup  # noqa: F401

UTR = "611532946151"  # the UTR on the fake screenshot


def with_utr(text: str, utr: str = UTR) -> str:
    return text.replace("UTR: NA", f"UTR: {utr}")


async def asked_now(db, case_id, poster) -> tuple[str, int]:
    """(the order the bot is waiting on, the message id of its /pi)."""
    oid = sent_pi(poster)[-1].split()[1]
    return oid, (await query_ids(db, case_id))[oid]


async def chosen(db, case_id):
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        return c.status, c.betex_pay_order_id


def test_the_utr_line_of_a_pi_answer():
    assert parse_pi_utr(pi_answer("ILLUN-1", "a-1@ybl")) is None  # "UTR: NA"
    assert parse_pi_utr(with_utr(pi_answer("ILLUN-1", "a-1@ybl"))) == UTR
    assert parse_pi_utr("🧾UTR: -") is None and parse_pi_utr(None) is None


async def test_no_upi_on_the_screenshot_the_check_still_runs_and_the_utr_decides(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    """A Kotak-app receipt: no payee UPI printed. The orders are asked about all the same; Betix holds the
    payment's UTR on the second one - that is the order. No Manual Review."""
    fake_ai.receiver_upi = None
    case_id, outcome = await run_until_ready(db, order_search, TWINS)
    assert outcome == "checking_upi" and len(sent_pi(fake_poster)) == 1
    first, q = await asked_now(db, case_id, fake_poster)
    r = await answer(db, 900, pi_answer(first, "rahul-7788@ptyes"), q)  # UTR: NA - not this one
    assert r["action"] == "pi_next"
    second, q = await asked_now(db, case_id, fake_poster)
    r = await answer(db, 901, with_utr(pi_answer(second, "shoriful-5011@ptyes")), q)
    assert r["action"] == "pi_ready" and second != first
    assert await chosen(db, case_id) == (CaseStatus.READY_FOR_BETIX.value, second)
    assert not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)


async def test_no_upi_and_betix_holds_the_utr_on_none_is_manual_review(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    fake_ai.receiver_upi = None
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    first, q = await asked_now(db, case_id, fake_poster)
    await answer(db, 900, pi_answer(first, "rahul-7788@ptyes"), q)
    second, q = await asked_now(db, case_id, fake_poster)
    r = await answer(db, 901, with_utr(pi_answer(second, "shoriful-5011@ptyes"), "999999999999"), q)  # another UTR
    assert r["action"] == "pi_ambiguous"
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW" in t][-1]
    assert "the screenshot shows no payee UPI and Betix holds this payment's UTR on none of them" in alert
    assert first in alert and second in alert and "UTR at Betix 999999999999" in alert and f"UTR: {UTR}" in alert


async def test_the_utr_settles_it_even_when_the_upi_does_not_fit(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    """The screenshot's UPI (******5011@ptyes) does not fit the order's - but Betix holds this payment's UTR on it:
    the UTR is the primary reference."""
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    first, q = await asked_now(db, case_id, fake_poster)
    r = await answer(db, 900, with_utr(pi_answer(first, "rahul-7788@ptyes")), q)
    assert r["action"] == "pi_ready"
    assert await chosen(db, case_id) == (CaseStatus.READY_FOR_BETIX.value, first)
    assert len(sent_pi(fake_poster)) == 1  # settled: the other order is never asked


async def test_a_upi_a_character_off_is_accepted_when_no_order_fits_exactly(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    """The screenshot's UPI read by OCR as shoriful-5O11@ptyes (a letter O for the zero); the first order's UPI is
    shoriful-5011@ptyes, the second another one altogether. Nothing fits letter for letter - the first is it."""
    fake_ai.receiver_upi = "shoriful-5O11@ptyes"
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    first, q = await asked_now(db, case_id, fake_poster)
    r = await answer(db, 900, pi_answer(first, "shoriful-5011@ptyes"), q)
    assert r["action"] == "pi_next"  # not exact: the other order is asked first, it might fit exactly
    second, q = await asked_now(db, case_id, fake_poster)
    r = await answer(db, 901, pi_answer(second, "rahul-7788@ptyes"), q)
    assert r["action"] == "pi_ready"
    assert await chosen(db, case_id) == (CaseStatus.READY_FOR_BETIX.value, first)
    assert not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)


async def test_an_exact_upi_beats_one_that_is_a_character_off(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup
):
    fake_ai.receiver_upi = "shoriful-5011@ptyes"
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    first, q = await asked_now(db, case_id, fake_poster)
    await answer(db, 900, pi_answer(first, "shoriful-5012@ptyes"), q)  # one character off
    second, q = await asked_now(db, case_id, fake_poster)
    r = await answer(db, 901, pi_answer(second, "shoriful-5011@ptyes"), q)  # exactly the screenshot's
    assert r["action"] == "pi_ready"
    assert await chosen(db, case_id) == (CaseStatus.READY_FOR_BETIX.value, second)


async def test_four_characters_off_is_another_upi(db, fake_bot, fake_ai, order_search, no_download, fake_poster, setup):
    fake_ai.receiver_upi = "shoriful-5011@ptyes"
    case_id, _ = await run_until_ready(db, order_search, TWINS)
    first, q = await asked_now(db, case_id, fake_poster)
    await answer(db, 900, pi_answer(first, "shoriful-9988@ptyes"), q)  # 4 characters off
    second, q = await asked_now(db, case_id, fake_poster)
    r = await answer(db, 901, pi_answer(second, "rahul-7788@ptyes"), q)
    assert r["action"] == "pi_ambiguous"
    assert any("none fits the screenshot UPI" in t for _, t in fake_bot.sent)
