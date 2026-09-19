"""When nothing under the customer's number was created around the payment, the panel is searched by UTR and by
the exact padded amount; an order found that way is the payment even under another registered number.
(Live case 2026-09-11: ₹5,999.93, UTR 977042086205 - nothing anywhere, correctly escalated with a clear reason.)"""

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case, list_candidates
from tests.conftest import make_candidates
from tests.test_flow import MOBILE, run_until_ready

OLD_FAILED = [
    {
        "illunise_order_id": "9001",
        "betex_order_id": "ILLUN-178660472036384",
        "registration_number": MOBILE,
        "amount": 6000.0,
        "order_time": "2026-08-13 12:35:00",
        "status": "Failed",
        "gateway": "BetixPay",
    },
]
# the customer's real order, made under ANOTHER registered number, carrying the payment's UTR
OTHER_NUMBER_UTR = [
    {
        "illunise_order_id": "9002",
        "betex_order_id": "ILLUN-178904900000001",
        "registration_number": "9000000001",
        "amount": 6499.92,
        "order_time": "2026-09-10 19:31:00",
        "status": "Pending",
        "gateway": "BetixPay",
        "utr": "611532946151",
    },
]
OTHER_NUMBER_PADDED = [
    {
        "illunise_order_id": "9003",
        "betex_order_id": "ILLUN-178904900000002",
        "registration_number": "9000000002",
        "amount": 6500.0,
        "padded_amount": 6499.92,
        "order_time": "2026-09-10 19:30:30",
        "status": "Pending",
        "gateway": "BetixPay",
    },
]


def searcher(table: dict[str, list[dict]]):
    queries = []

    async def _search(query, amount=None, when=None):
        queries.append(query)
        return make_candidates(table.get(query, []))

    manager.set_order_search(_search)
    return queries


async def test_order_under_another_number_is_found_by_utr(db, fake_bot, fake_ai, no_download):
    queries = searcher({MOBILE: OLD_FAILED, "611532946151": OTHER_NUMBER_UTR})
    try:
        case_id, outcome = await run_until_ready(db, lambda *_: None)
    finally:
        manager.set_order_search(None)
    assert outcome == "ready" and queries == [MOBILE, "611532946151"]
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.betex_pay_order_id == "ILLUN-178904900000001" and c.status == CaseStatus.READY_FOR_BETIX.value
        best = max(await list_candidates(s, case_id), key=lambda r: r.score)
        assert best.signals["registration"]["score"] == 1.0 and "same UTR" in best.signals["registration"]["how"]


async def test_order_under_another_number_is_found_by_padded_amount(db, fake_bot, fake_ai, no_download):
    queries = searcher({MOBILE: OLD_FAILED, "611532946151": [], "6499.92": OTHER_NUMBER_PADDED})
    try:
        case_id, outcome = await run_until_ready(db, lambda *_: None)
    finally:
        manager.set_order_search(None)
    assert outcome == "ready" and queries == [
        MOBILE,
        "611532946151",
        "6499.92",
    ]  # found by the padded amount: the search stops there
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).betex_pay_order_id == "ILLUN-178904900000002"


async def test_nothing_anywhere_escalates_with_a_clear_reason(db, fake_bot, fake_ai, no_download):
    queries = searcher({MOBILE: OLD_FAILED})
    try:
        case_id, outcome = await run_until_ready(db, lambda *_: None)
    finally:
        manager.set_order_search(None)
    assert outcome == "escalated" and queries == [
        MOBILE,
        "611532946151",
        "6499.92",
        "6500",
    ]  # mobile, UTR, padded amount, whole-rupee order amount
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW NEEDED" in t][0]
    assert "No order was created around the payment" in alert and "ILLUN-178660472036384" in alert
    assert "created 13 Aug" in alert and "Failed" in alert
    assert "Also searched by UTR and padded amount and order amount" in alert and "another registered number" in alert


async def test_no_fallback_when_the_mobile_search_already_matches(db, fake_bot, fake_ai, no_download):
    from tests.test_flow import GOOD

    queries = searcher({MOBILE: GOOD})
    try:
        _, outcome = await run_until_ready(db, lambda *_: None)
    finally:
        manager.set_order_search(None)
    assert outcome == "ready" and queries == [MOBILE]
