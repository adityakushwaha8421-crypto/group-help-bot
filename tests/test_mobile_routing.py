"""The mobile number the operator types must land on the case being collected NOW.

Live 2026-09-12 (CASE-20260912-000004): "Reg mobile number - 9175404354" was swallowed by an 8-minute-old case that
already had that number and its own screenshot, so the new case kept showing "mobile number" as missing."""

import pytest

from app.ai.extractor import extract_mobile
from app.cases.correlation import attach_message, missing_items
from app.db.repository import get_case, list_evidence
from app.telegram.input_bot import case_status_lines
from tests.conftest import make_input

MOB = "9175404354"


@pytest.mark.parametrize(
    "text",
    [
        "Reg mobile number - 9175404354",
        "registered mobile: 9175404354",
        "Registered Mobile No. 9175404354",
        "mobile 9175404354",
        "Mob no - +91 9175404354",
        "+91 9175404354",
        "+91-9175404354",
        "91 75404 354".replace(" ", ""),  # bare
        "9175404354",
        "reg mobile number 91754-04354",
        "Customer mobile 09175404354",
    ],
)
def test_every_format_gives_the_ten_digit_number(text):
    assert extract_mobile(text) == MOB


async def test_the_number_updates_the_case_being_collected(db):
    """Screenshot first, number second: the SAME case is updated, nothing new is created."""
    async with db.session_scope() as s:
        r1 = await attach_message(s, make_input(1, "photo"))
        r2 = await attach_message(s, make_input(2, "text", "Reg mobile number - 9175404354"))
        assert r2.case.case_id == r1.case.case_id and not r2.created
        case = r2.case
        ev = await list_evidence(s, case.case_id)
        assert case.mobile == MOB
        assert f"✅ Mobile: <code>{MOB}</code>" in case_status_lines(case, ev)
        assert "mobile number" not in missing_items(case, ev)


async def test_a_repeated_number_for_the_case_being_collected_is_not_a_new_case(db):
    async with db.session_scope() as s:
        r1 = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", f"Reg mobile number - {MOB}"))
        again = await attach_message(s, make_input(3, "text", MOB))  # typed twice in the same batch
        assert again.case.case_id == r1.case.case_id and not again.created


async def test_the_number_starts_the_next_case_instead_of_joining_an_old_one(db, monkeypatch):
    """The live sequence: an older case already has this number AND a screenshot, then the operator sends the
    number again followed by the new evidence."""
    from app.utils.timeutil import utcnow

    async with db.session_scope() as s:
        old = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", f"Reg mobile number - {MOB}"))
        old_id = old.case.case_id
        # ... 8 minutes pass (older than BATCH_JOIN_SECONDS) ...
        old.case.last_input_at = utcnow() - __import__("datetime").timedelta(minutes=8)
        await s.flush()
        r = await attach_message(s, make_input(10, "text", f"Reg mobile number - {MOB}"))
        assert r.created and r.case.case_id != old_id and r.case.mobile == MOB
        new_id = r.case.case_id
        # the evidence that follows joins the NEW case, which already shows the number
        for inp in (make_input(11, "video"), make_input(12, "document"), make_input(13, "photo")):
            assert (await attach_message(s, inp)).case.case_id == new_id
        case = await get_case(s, new_id)
        ev = await list_evidence(s, new_id)
        assert missing_items(case, ev) == [] and case.mobile == MOB
        assert (await get_case(s, old_id)).mobile == MOB  # the old case is untouched


async def test_a_different_number_always_starts_a_new_case(db):
    async with db.session_scope() as s:
        first = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", MOB))
        other = await attach_message(s, make_input(3, "text", "Reg mobile number - 8262808027"))
        assert other.created and other.case.case_id != first.case.case_id
        assert other.case.mobile == "8262808027"


async def test_processing_never_starts_before_the_number_is_shown(db, fake_bot, fake_ai, order_search, no_download):
    """A case without the mobile is never processed or posted, whatever else it holds."""
    from app.cases import manager

    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "document"))
        await attach_message(s, make_input(3, "video"))
        case_id = r.case.case_id
        ev = await list_evidence(s, case_id)
        assert "mobile number" in missing_items(r.case, ev)
        assert "⏳ Mobile — waiting" in case_status_lines(r.case, ev)
        assert await manager.process_case(s, case_id, force=True) == "not_ready"
        assert await manager.process_case(s, case_id, force=True, force_send=True) == "not_ready"
        # the number arrives -> the same case is complete and may now be processed
        await attach_message(s, make_input(4, "text", f"Reg mobile number - {MOB}"))
        case = await get_case(s, case_id)
        assert case.mobile == MOB and missing_items(case, await list_evidence(s, case_id)) == []
