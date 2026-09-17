"""WITHDRAWAL cases: the operator sends only a withdrawal id ("WD-84425-67115") and the bank statement. The Betix
group receives "BXWD-84425-67115" as a plain message and the statement as a reply to it - nothing else. No
screenshot, mobile or video is asked for, no AI read, no Illunise search, and one id is posted once."""

from app.ai.extractor import extract_from_text, extract_mobile, extract_withdrawal_id
from app.cases import manager
from app.cases.correlation import attach_message, missing_items
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence
from app.telegram.betix_monitor import handle_group_message
from app.telegram.input_bot import case_card, case_status_lines, force_send_pending
from tests.conftest import make_group_msg, make_input
from tests.test_flow import SYS_OK

WD = "WD-84425-67115"


def test_the_id_is_recognised_in_any_wording():
    for text in (WD, "wd-84425-67115", f"Withdrawal id {WD}", f"BX{WD} please check", f"ID: {WD}."):
        assert extract_withdrawal_id(text) == WD, text
    assert extract_withdrawal_id("ILLUN-178921693146901") is None
    e = extract_from_text(f"Withdrawal {WD}")
    assert e.withdrawal_id.value == WD and e.registration_number.value is None
    assert extract_mobile(WD) is None  # "84425-67115" is not a mobile number


async def test_id_then_statement_make_one_withdrawal_case(db, fake_bot):
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "text", WD))
        assert r.created and r.case.kind == "withdrawal" and r.case.withdrawal_id == WD
        ev = await list_evidence(s, r.case.case_id)
        assert missing_items(r.case, ev) == ["bank statement"]
        assert case_status_lines(r.case, ev) == [f"✅ Withdrawal ID: <code>{WD}</code>", "⏳ Bank Statement — waiting"]
        assert not force_send_pending(r.case, missing_items(r.case, ev))  # never force-sent without the PDF
        r2 = await attach_message(s, make_input(2, "document"))
        assert r2.case.case_id == r.case.case_id and not r2.created
        ev = await list_evidence(s, r.case.case_id)
        assert missing_items(r.case, ev) == []
        assert "💸 <b>New Withdrawal</b>" in case_card(r.case, ev, [], created=True, forwarded=False)


async def test_statement_then_id_also_make_one_case(db, fake_bot):
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "document"))
        r2 = await attach_message(s, make_input(2, "text", WD))
        assert r2.case.case_id == r.case.case_id and r2.case.kind == "withdrawal"
        assert missing_items(r2.case, await list_evidence(s, r.case.case_id)) == []


async def test_posting_sends_bx_id_then_the_statement_as_a_reply(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    async with db.session_scope() as s:
        cid = (await attach_message(s, make_input(1, "text", WD))).case.case_id
        await attach_message(s, make_input(2, "document"))
    async with db.session_scope() as s:
        assert await manager.process_case(s, cid, force=True) == "ready"
        c = await get_case(s, cid)
        assert c.betex_pay_order_id == f"BX{WD}" and c.status == CaseStatus.READY_FOR_BETIX.value
        assert await manager.post_case_to_betix(s, cid, fake_poster) == "posted"
        c = await get_case(s, cid)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value and c.betix_root_message_id == 501
    assert fake_ai.calls == 0  # nothing read by the model
    assert fake_poster.texts[0] == (f"BX{WD}", None)  # the id, standalone: it is the anchor
    assert fake_poster.media_replies == [("bank_statement", 501)]  # the statement replies to it, nothing else


async def test_confirmation_and_followups_work_as_for_a_payment(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    from app.config import reset_settings_cache

    reset_settings_cache()
    async with db.session_scope() as s:
        cid = (await attach_message(s, make_input(1, "text", WD))).case.case_id
        await attach_message(s, make_input(2, "document"))
    async with db.session_scope() as s:
        await manager.process_case(s, cid, force=True)
        await manager.post_case_to_betix(s, cid, fake_poster)
        r = await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r["action"] == "verified"
        c = await get_case(s, cid)
        assert c.status == CaseStatus.VERIFIED.value and c.followup_cancelled
    assert any("PAYMENT CONFIRMED" in t and f"BX{WD}" in t for _, t in fake_bot.sent)


async def test_the_same_withdrawal_id_is_posted_once(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    async with db.session_scope() as s:
        a = (await attach_message(s, make_input(1, "text", WD))).case.case_id
        await attach_message(s, make_input(2, "document"))
    async with db.session_scope() as s:
        await manager.process_case(s, a, force=True)
        await manager.post_case_to_betix(s, a, fake_poster)
    async with db.session_scope() as s:  # sent again an hour later, from a fresh case
        b = (await attach_message(s, make_input(11, "text", WD, chat_id=222, user_id=222))).case.case_id
        await attach_message(s, make_input(12, "document", chat_id=222, user_id=222))
    async with db.session_scope() as s:
        assert await manager.process_case(s, b, force=True) == "already_sent"
    assert fake_poster.texts.count((f"BX{WD}", None)) == 1


async def test_a_protected_statement_asks_for_the_password_first(db, fake_bot, tmp_path):
    from tests.test_file_detection import make_pdf

    locked = make_pdf(tmp_path / "s.pdf", "4321")
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "text", WD))
        st = await attach_message(s, make_input(2, "document", filename="s.pdf"))
        st.evidence.local_path, st.evidence.downloaded = str(locked), True
        assert missing_items(r.case, await list_evidence(s, r.case.case_id)) == ["statement password"]
        await attach_message(s, make_input(3, "text", "password 4321"))
        assert r.case.statement_password == "4321"
        assert missing_items(r.case, await list_evidence(s, r.case.case_id)) == []
