"""UTR is NOT mandatory. A screenshot that shows none is matched on mobile + amount + time like any other; a UTR,
when there is one, stays a strong extra signal. None is ever invented or taken from elsewhere."""

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case
from tests.test_flow import GOOD, submit_four_messages
from tests.test_utr_required import NO_UTR


async def test_a_screenshot_without_a_utr_is_matched_normally(db, fake_bot, fake_ai, order_search, no_download):
    fake_ai.screenshot = dict(NO_UTR)
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.READY_FOR_BETIX.value and c.betex_pay_order_id == GOOD[0]["betex_order_id"]
        assert c.utr is None  # never invented
    assert not any("UTR Not Found" in t for _, t in fake_bot.sent)
