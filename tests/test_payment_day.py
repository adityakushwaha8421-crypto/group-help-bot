"""THE PAYMENT DATE FIRST. The search is limited to the payment's own day (from_date / to_date on the panel) and
the orders are judged by their CREATED time - never by the panel's "Updated" date, which is what the operator saw
as "18/19 Sep" on orders created 13 Sep. A screenshot that shows the day but no time: every order of that day
counts, and a doubt between them goes to /pi."""

from datetime import datetime, timezone

from app.admin.orders import id_time, search_dates
from app.ai.extractor import extraction_from_ai
from app.cases import manager
from app.db.repository import get_case
from tests.test_flow import GOOD, MOBILE, run_until_ready
from tests.test_pi_check import pi_queries, setup  # noqa: F401


def test_the_created_time_is_carried_inside_the_order_id():
    when = id_time("ILLUN-178930744386451")
    assert when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-09-13 13:50"  # 19:20 IST
    assert id_time("ILLUN-1") is None and id_time(None) is None


def test_search_dates_are_the_payment_day_only():
    when = datetime(2026, 9, 13, 13, 50, tzinfo=timezone.utc)  # 13 Sep 19:20 IST
    assert search_dates(when, 30, "Asia/Kolkata") == ("2026-09-13", "2026-09-13")
    midnight = datetime(2026, 9, 12, 18, 40, tzinfo=timezone.utc)  # 13 Sep 00:10 IST: the window reaches back
    assert search_dates(midnight, 30, "Asia/Kolkata") == ("2026-09-12", "2026-09-13")


def test_a_screenshot_with_a_date_but_no_time_is_read_as_that_day():
    payload = {"payment_time": {"value": "2026-09-13", "confidence": 0.9, "evidence_text": "13 Sep 2026"}}
    f = extraction_from_ai(payload, "payment_screenshot").payment_time
    assert f.precision == "date" and f.value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-09-13 18:29"
    assert f.as_dict()["precision"] == "date"


DAY_ORDERS = [  # all created 13 Sep (IST), at different hours; the payment screenshot shows "13 Sep" and no time
    {**GOOD[0], "illunise_order_id": "1", "betex_order_id": "ILLUN-178929000000001", "amount": 500.0, "order_time": "2026-09-13 09:10:00"},
    {**GOOD[0], "illunise_order_id": "2", "betex_order_id": "ILLUN-178930744386451", "amount": 500.0, "order_time": "2026-09-13 19:20:00"},
    {**GOOD[0], "illunise_order_id": "3", "betex_order_id": "ILLUN-178931000000003", "amount": 900.0, "order_time": "2026-09-13 21:00:00"},
]  # fmt: skip
OTHER_DAY = [
    {**GOOD[0], "betex_order_id": "ILLUN-178970000000009", "amount": 500.0, "order_time": "2026-09-18 19:20:00"}
]


async def _date_only_case(db, fake_ai, order_search, cands):
    fake_ai.screenshot = {
        "amount": {"value": 499.83, "confidence": 0.97},
        "payment_time": {"value": "2026-09-13", "confidence": 0.9, "evidence_text": "13 Sep"},
        "payment_status": {"value": "Successful", "confidence": 0.9},
    }
    seen = {}

    async def _search(query, amount=None, when=None, **kw):
        seen["when"], seen["kw"] = when, kw
        from tests.conftest import make_candidates

        return make_candidates(cands) if query == MOBILE else []

    manager.set_order_search(_search)
    try:
        case_id, outcome = await run_until_ready(db, lambda *_: None)
    finally:
        manager.set_order_search(None)
    return case_id, outcome, seen


async def test_every_order_of_the_payment_day_counts_when_no_time_is_shown(
    db, fake_bot, fake_ai, no_download, fake_poster, setup
):
    case_id, outcome, seen = await _date_only_case(db, fake_ai, None, DAY_ORDERS)
    assert seen["kw"] == {"window_minutes": 1440}  # the whole day is searched
    assert outcome == "checking_upi"  # two Rs 500 orders that day: a doubt, settled by /pi - not by the clock
    assert set(pi_queries(fake_poster)) == {"/pi ILLUN-178930744386451", "/pi ILLUN-178929000000001"} or list(
        pi_queries(fake_poster)
    ) == ["/pi ILLUN-178930744386451"]


async def test_one_fitting_order_on_the_day_is_taken(db, fake_bot, fake_ai, no_download, fake_poster, setup):
    case_id, outcome, _ = await _date_only_case(db, fake_ai, None, DAY_ORDERS[1:])
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).betex_pay_order_id == "ILLUN-178930744386451"


async def test_an_order_of_another_day_never_fits(db, fake_bot, fake_ai, no_download, fake_poster, setup):
    _, outcome, _ = await _date_only_case(db, fake_ai, None, OTHER_DAY)
    assert outcome == "escalated" and pi_queries(fake_poster) == {}
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW" in t][-1]
    assert "13 Sep" in alert
