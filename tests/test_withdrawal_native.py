"""A WITHDRAWAL is routed like a deposit: the payout page in Illunise names the gateway that paid it out. A payout
of the Native gateway ("BetixPay Native") goes to the Native group; any other goes to the Betix group."""

import pytest
from sqlalchemy import select

from app.admin.payouts import parse_payout_page
from app.cases import manager
from app.cases.correlation import attach_message
from app.config import reset_settings_cache
from app.db.models import GATEWAY_BETIX, GATEWAY_NATIVE, AuditLog, CaseStatus, Followup
from app.db.repository import get_case
from app.telegram.progress import progress_card
from tests.conftest import make_input

NATIVE_CHAT, BETIX_CHAT = -1004444, -1009999
WD = "WD-50663-89662"
PAGE = """Payout WD-50663-89662
Refund
AMOUNT
₹1,105.80
STATUS
Success
BENEFICIARY
Shyam Jagtap
BANK ACCOUNT
State Bank of India
ACCOUNT
12345678903520
IFSC
SBIN0003918
UTR
632729244910
WITHDRAWAL
Withdraw ID
WD-50663-89662
Gateway
{gateway}
Vendor Order
{vendor}
TIMELINE
CREATED
23 Nov 2026 17:45
"""


@pytest.fixture(autouse=True)
def native_group(env, monkeypatch):
    monkeypatch.setenv("NATIVE_GROUP_CHAT_ID", str(NATIVE_CHAT))
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_the_payout_page_names_the_gateway_and_the_vendor_order():
    p = parse_payout_page(WD, PAGE.format(gateway="BetixPay", vendor="BXWD-50663-89662"))
    assert p.gateway == "BetixPay" and p.vendor_order == "BXWD-50663-89662"
    assert p.account == "12345678903520" and p.status == "Success" and p.amount == 1105.80
    n = parse_payout_page(WD, PAGE.format(gateway="BetixPay Native", vendor="BNWD-50663-89662"))
    assert n.gateway == "BetixPay Native" and n.vendor_order == "BNWD-50663-89662"


async def withdrawal(db) -> str:
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "text", WD))
        await attach_message(s, make_input(2, "document"))
        r.case.progress_chat_id, r.case.progress_message_id = 111, 4242
        return r.case.case_id


async def test_a_native_payout_goes_to_the_native_group(db, fake_bot, fake_ai, no_download, payout, fake_poster):
    payout(gateway="BetixPay Native", vendor_order="BNWD-50663-89662", created="23 Nov 2026 17:45",
           statement_ends="2026-11-24")  # fmt: skip
    case_id = await withdrawal(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        c = await get_case(s, case_id)
        assert c.gateway_choice == GATEWAY_NATIVE and c.gateway == "BetixPay Native"
        assert c.betex_pay_order_id == "BNWD-50663-89662"  # the gateway's own id for it, off the payout page
        assert "Sending it to the Native group" in progress_card(c)
        row = (await s.execute(select(AuditLog).where(AuditLog.action == "GATEWAY_DETECTED"))).scalars().one()
        assert row.result == "native" and row.details["gateway"] == "BetixPay Native"
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.betix_chat_id == NATIVE_CHAT and c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value
        assert "SENT TO NATIVE GROUP" in progress_card(c)
        assert len((await s.execute(select(Followup))).scalars().all()) > 0  # a withdrawal is still chased
    assert fake_poster.texts[0] == ("BNWD-50663-89662", None)  # the id, as the anchor
    assert fake_poster.media_replies == [("bank_statement", 501)]  # the statement, as its reply
    assert set(fake_poster.chats) == {NATIVE_CHAT}


async def test_a_betix_payout_goes_to_the_betix_group_as_before(
    db, fake_bot, fake_ai, no_download, payout, fake_poster
):
    payout(gateway="BetixPay", vendor_order="BXWD-50663-89662", created="23 Nov 2026 17:45",
           statement_ends="2026-11-24")  # fmt: skip
    case_id = await withdrawal(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.gateway_choice == GATEWAY_BETIX and c.betix_chat_id == BETIX_CHAT
        assert "SENT TO BETIX" in progress_card(c)
    assert fake_poster.texts[0] == ("BXWD-50663-89662", None) and set(fake_poster.chats) == {BETIX_CHAT}


async def test_a_page_without_a_vendor_order_still_posts_the_bx_id(
    db, fake_bot, fake_ai, no_download, payout, fake_poster
):
    payout(gateway=None, vendor_order=None, created="23 Nov 2026 17:45", statement_ends="2026-11-24")
    case_id = await withdrawal(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        assert (await get_case(s, case_id)).gateway_choice == GATEWAY_BETIX
    assert fake_poster.texts[0] == ("BXWD-50663-89662", None) and set(fake_poster.chats) == {BETIX_CHAT}


async def test_without_a_native_group_a_native_payout_is_never_sent_to_betix(
    db, fake_bot, fake_ai, no_download, payout, fake_poster, monkeypatch
):
    monkeypatch.setenv("NATIVE_GROUP_CHAT_ID", "")
    reset_settings_cache()
    payout(gateway="BetixPay Native", created="23 Nov 2026 17:45", statement_ends="2026-11-24")
    case_id = await withdrawal(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "escalated"
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "already"
        assert "NATIVE_GROUP_CHAT_ID" in ((await get_case(s, case_id)).failure_reason or "")
    assert not fake_poster.chats


async def test_the_card_names_the_gateway_the_panel_showed(db, fake_bot, fake_ai, no_download, payout, fake_poster):
    from app.telegram.progress import payout_gateway_rows

    payout(gateway="BetixPay Native", created="23 Nov 2026 17:45", statement_ends="2026-11-24")
    case_id = await withdrawal(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        assert payout_gateway_rows(await get_case(s, case_id)) == ["\U0001f500 Gateway: Native (BetixPay Native)"]
