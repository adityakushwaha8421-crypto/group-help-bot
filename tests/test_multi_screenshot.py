"""One payment, several screenshots (CRED: the receipt, then its details page with the UTR and the receiver UPI).
Sent together they are ONE case and every field is taken from whichever screen shows it; the receiver UPI for the
/pi check comes from any of them. Two screenshots that turn out to be two PAYMENTS are split into two cases."""

from datetime import datetime, timezone

from app.ai.extractor import parse_datetime_noyear
from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence
from app.telegram.upi_check import upi_ending_match
from tests.conftest import make_input
from tests.test_flow import GOOD, MOBILE

# the CRED example of 2026-09-22: Rs 23,999.02 at 09:02am, 22nd sep'26; details on the second screen
RECEIPT = {
    "amount": {"value": 23999.02, "confidence": 0.97},
    "payment_time": {"value": None, "confidence": 0.9, "evidence_text": "09:02am, 22nd sep'26"},
    "payment_status": {"value": "SUCCESSFUL", "confidence": 0.9},
    "receiver_name": {"value": "Arti Abhimanyu Lokhande", "confidence": 0.9},
}
DETAILS = {
    "utr": {"value": "626515955215", "confidence": 0.96, "evidence_text": "UPI transaction ID 626515955215"},
    "transaction_reference": {"value": "f525a67b-42ee-4d3b-9ccb-2ef97610c3a8", "confidence": 0.9},
    "receiver_upi": {"value": "boim-074160027519@boi", "confidence": 0.95, "evidence_text": "paid to UPI ID boim-074160027519@boi"},
}  # fmt: skip
ORDER = [{**GOOD[0], "amount": 24000.0, "order_time": "2026-09-22 09:01:00", "utr": None}]


def test_the_time_is_read_in_the_receipts_own_format():
    now = datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc)
    for printed in ("09:02am, 22nd sep'26", "22nd sep'26, 09:02am", "22nd Sep '26 at 09:02 am", "11:09 PM on 13 Sept"):
        assert parse_datetime_noyear(printed, now) is not None, printed
    got = parse_datetime_noyear("09:02am, 22nd sep'26", now)
    assert (got.year, got.month, got.day) == (2026, 9, 22) and got.strftime("%H:%M") == "03:32"  # 09:02 IST


def test_a_receiver_upi_hidden_in_the_middle_still_matches():
    assert upi_ending_match("boim-0741XXXX7519@boi", "boim-074160027519@boi")[0]
    assert upi_ending_match("mo**42@ptyes", "monu9842@ptyes")[0]
    assert not upi_ending_match("mo**42@ptyes", "sonu9842@ptyes")[0]
    assert not upi_ending_match("boim-0741XXXX7519@boi", "boim-074160027518@boi")[0]
    assert upi_ending_match("******5011@ptyes", "shoriful-5011@ptyes")[0]  # the old leading-mask form
    assert not upi_ending_match("XX@ptyes", "monu9842@ptyes")[0]  # nothing visible


async def _two_screens(db, second_delay_seconds: int = 0):
    async with db.session_scope() as s:
        r1 = await attach_message(s, make_input(1, "photo"))
        if second_delay_seconds:
            from datetime import timedelta

            r1.case.last_input_at = r1.case.last_input_at - timedelta(seconds=second_delay_seconds)
            await s.flush()
        r2 = await attach_message(s, make_input(2, "photo"))
        await attach_message(s, make_input(3, "text", MOBILE))
        await attach_message(s, make_input(4, "document"))
        await attach_message(s, make_input(5, "video"))
        return r1.case.case_id, r2.case.case_id


async def test_two_screens_of_one_payment_are_one_case_with_the_facts_of_both(
    db, fake_bot, fake_ai, order_search, no_download
):
    fake_ai.screenshots = [dict(RECEIPT), dict(DETAILS)]
    a, b = await _two_screens(db)
    assert a == b
    order_search(ORDER)
    async with db.session_scope() as s:
        assert await manager.process_case(s, a, force=True) == "ready"
        c = await get_case(s, a)
        assert c.amount == 23999.02 and c.utr == "626515955215"  # amount from screen 1, UTR from screen 2
        assert c.payment_time and c.payment_time.strftime("%Y-%m-%d") == "2026-09-22"
        assert c.betex_pay_order_id == GOOD[0]["betex_order_id"]
        assert len([e for e in await list_evidence(s, a) if e.type == "payment_screenshot"]) == 2


async def test_the_receiver_upi_comes_from_whichever_screen_shows_it(db, fake_bot, fake_ai, order_search, no_download):
    fake_ai.screenshots = [dict(RECEIPT), dict(DETAILS)]
    a, _ = await _two_screens(db)
    order_search(ORDER)
    async with db.session_scope() as s:
        await manager.process_case(s, a, force=True)
        c = await get_case(s, a)
        upi, how = await manager.read_screenshot_upi(s, c)
    assert upi == "boim-074160027519@boi" and fake_ai.ocr_calls == 0  # found in the full read: no extra OCR call


async def test_two_different_payments_sent_together_are_split(db, fake_bot, fake_ai, order_search, no_download):
    other = {
        **RECEIPT,
        "amount": {"value": 500.0, "confidence": 0.97},
        "utr": {"value": "999999999999", "confidence": 0.9},
    }
    fake_ai.screenshots = [dict(RECEIPT), other]
    a, b = await _two_screens(db)
    assert a == b
    order_search(ORDER)
    async with db.session_scope() as s:
        assert await manager.process_case(s, a, force=True) == "ready"
        c = await get_case(s, a)
        assert c.amount == 23999.02
        shots = [e for e in await list_evidence(s, a) if e.type == "payment_screenshot"]
        assert len(shots) == 1  # the other screenshot left this case ...
    from sqlalchemy import select

    from app.db.models import Case, Evidence

    async with db.session_scope() as s:
        moved = (
            (await s.execute(select(Evidence).where(Evidence.case_id != a, Evidence.type == "payment_screenshot")))
            .scalars()
            .one()
        )
        new = await get_case(s, moved.case_id)
        assert new.case_id != a and new.status == CaseStatus.WAITING_FOR_INPUT.value and new.mobile == MOBILE
        assert len((await s.execute(select(Case))).scalars().all()) == 2
    assert any("Two payments in one submission" in t and new.case_id in t for _, t in fake_bot.sent)


async def test_a_screenshot_sent_later_is_still_a_new_case(db, fake_bot, fake_ai, order_search, no_download):
    a, b = await _two_screens(db, second_delay_seconds=120)
    assert a != b


async def test_both_screens_go_to_betix_under_the_same_order_id(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    fake_ai.screenshots = [dict(RECEIPT), dict(DETAILS)]
    a, _ = await _two_screens(db)
    order_search(ORDER)
    async with db.session_scope() as s:
        assert await manager.process_case(s, a, force=True) == "ready"
        assert await manager.post_case_to_betix(s, a, fake_poster) == "posted"
        c = await get_case(s, a)
        shots = [e for e in await list_evidence(s, a) if e.type == "payment_screenshot"]
        assert all(e.posted_to_betix_message_id for e in shots) and len(shots) == 2
        root = c.betix_root_message_id
    order = GOOD[0]["betex_order_id"]
    assert [m for m in fake_poster.media if m[0] == "payment_screenshot"] == [("payment_screenshot", order)] * 2
    assert fake_poster.media_replies[:2] == [
        ("payment_screenshot", None),
        ("payment_screenshot", root),
    ]  # 2nd under 1st
    # posting again sends nothing more
    async with db.session_scope() as s:
        c = await get_case(s, a)
        await fake_poster.post_case(s, c, await list_evidence(s, a))
    assert len([m for m in fake_poster.media if m[0] == "payment_screenshot"]) == 2
