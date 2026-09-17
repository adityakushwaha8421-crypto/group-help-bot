"""`/upi` is never used. A Betix bot "❌ UPI Does not belong to us" goes to manual review like any other rejection;
nothing is ever replied with /upi. The payee-UPI helpers below serve only the /pi order check (screenshot OCR UPI
vs the Order's UPI printed in the Betix bot's answer to /pi)."""

import pytest

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence
from app.telegram.betix_monitor import handle_group_message
from app.telegram.betix_poster import BetixPostError
from app.telegram.upi_check import ocr_upi, upi_ending_match
from tests.conftest import make_group_msg
from tests.test_flow import run_until_ready

ORDER = "ILLUN-178621243657290"
ROOT = 501

NOT_BELONG = (
    "🔍Recognition results\n 🧾UTR: 611532946151\n 🏦UPI: 4913@pthdfc\n\n❌ UPI Does not belong to us: [4913@pthdfc] --"
    "\n\n\nResubmit utr result: Failed\n-----------\nMerchantId: B3126 (INR) \nOrder's UPI: 8679734913@pthdfc\n"
    f"📌MerchantOrderNo: {ORDER}"
)
RESUBMIT_FAILED = (
    "🔍Recognition results\n 🧾UTR: 613916453011\n 🏦UPI: \n\n\nResubmit utr result: Failed\n-----------\n"
    f"Order's UPI: BHARATPE09913969476@yesbankltd\n📌MerchantOrderNo: {ORDER}"
)


@pytest.fixture
def poster_hook(fake_poster):
    manager.set_poster_factory(lambda: fake_poster)
    yield fake_poster
    manager.set_poster_factory(None)


async def posted_case(db, order_search, poster, screenshot_upi):
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, poster) == "posted"
        shot = next(e for e in await list_evidence(s, case_id) if e.type == "payment_screenshot")
        shot.analysis = {
            "payload": {
                "receiver_upi": {"value": screenshot_upi, "confidence": 0.97, "evidence_text": f"To: {screenshot_upi}"}
            }
        }
    return case_id


async def betix_says(db, message_id, text, reply_to=ROOT):
    async with db.session_scope() as s:
        return await handle_group_message(
            s, make_group_msg(message_id, text, sender_username="betixpay_cs_bot", is_bot=True, reply_to=reply_to)
        )


def no_upi_sent(poster):
    return not any(t.strip().lower().startswith("/upi") for t, _ in poster.texts)


async def test_upi_does_not_belong_goes_to_manual_review_without_upi(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook
):
    """Even when the screenshot UPI's ending fits the Order's UPI: no /upi, manual review."""
    case_id = await posted_case(db, order_search, poster_hook, "4913@pthdfc")
    r = await betix_says(db, 700, NOT_BELONG)
    assert r["action"] == "escalated_failed" and no_upi_sent(poster_hook)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ESCALATED.value and "UPI does not belong to us" in c.failure_reason


async def test_resubmit_failed_keeps_monitoring_without_upi(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook
):
    case_id = await posted_case(db, order_search, poster_hook, "bharatpe09913969476@yesbankltd")
    r = await betix_says(db, 700, RESUBMIT_FAILED)
    assert r["action"] == "recorded" and no_upi_sent(poster_hook)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.WAITING_FOR_CONFIRMATION.value


async def test_the_poster_refuses_to_send_upi(fake_poster):
    for text in ("/upi 8679734913@pthdfc", " /UPI x@ybl"):
        with pytest.raises(BetixPostError):
            await fake_poster._real.__class__._send_text(fake_poster._real, text)


def test_no_upi_code_is_left():
    import pathlib

    src = "".join(p.read_text() for p in pathlib.Path("app").rglob("*.py"))
    assert "send_order_upi" not in src and "parse_upi_failure" not in src and "betix_upi_reply_template" not in src


# ---------------------------------------------------------------- payee-UPI helpers used by the /pi check
@pytest.mark.parametrize(
    "shot,order,expected",
    [
        ("4913@pthdfc", "8679734913@pthdfc", True),  # the user's example
        ("xxxxxx4913@pthdfc", "8679734913@pthdfc", True),  # masked, as our screenshot read it
        ("XXXXXX1014-4@ibl", "6307861014-4@ibl", True),
        ("182@ptyes", "anshika-182@ptyes", True),
        ("9939@ptaxis", "Vikas-9939@ptaxis", True),  # case-insensitive
        ("8679734913@pthdfc", "8679734913@pthdfc", True),
        ("4913@ptaxis", "8679734913@pthdfc", False),  # handle differs
        ("1234@pthdfc", "8679734913@pthdfc", False),  # ending differs
        ("13@pthdfc", "8679734913@pthdfc", False),  # too few characters to call it
        ("P@YTM", "9954538657@ptsbi", False),
        (None, "8679734913@pthdfc", False),
    ],
)
def test_ending_and_handle_rule(shot, order, expected):
    assert upi_ending_match(shot, order)[0] is expected


def test_ocr_reading_validation():
    assert ocr_upi({"value": "XXXXXX4913@pthdfc", "evidence_text": "Sent to: Paytm • XXXXXX4913@pthdfc"}) == (
        "XXXXXX4913@pthdfc",
        "payment screenshot OCR",
    )
    assert ocr_upi({"value": "• 4913@pthdfc", "evidence_text": "To: • 4913@pthdfc"})[0] == "4913@pthdfc"
    assert ocr_upi({"value": None, "evidence_text": None})[0] is None
    assert ocr_upi({"value": "Mr GAGAN VERMA", "evidence_text": "Paid to Mr GAGAN VERMA"})[0] is None
    assert ocr_upi({"value": "4913@pthdfc", "evidence_text": None})[0] is None
    assert ocr_upi(None)[0] is None
