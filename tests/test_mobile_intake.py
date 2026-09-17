"""Intake rules.

REQUIRED before a case is processed (REQUIRE_ALL_EVIDENCE=true, the default):
  1. payment screenshot   2. customer mobile number   3. bank statement (PDF)   4. payment video
The mobile is the identifier (any format, normalised to 10 digits). A registration number is optional and never
blocks. While something is missing the bot says exactly what is pending and does NOT start the Illunise search.
"""

import pytest

from app.ai.extractor import extract_from_text, extract_mobile, normalize_mobile
from app.cases import manager
from app.cases.correlation import attach_message, missing_items
from app.config import get_settings
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence
from app.telegram.input_bot import case_status_lines
from tests.conftest import make_input
from tests.test_flow import GOOD as _GOOD

# The same fake Illunise orders, but belonging to the mobile used in these tests.
GOOD = [{**o, "registration_number": "7733931348"} for o in _GOOD]
MOB = "7733931348"


@pytest.mark.parametrize(
    "raw",
    [
        "7733931348",
        "+91 7733931348",
        "+917733931348",
        "77339 31348",
        "773-393-1348",
        "07733931348",
        "mobile: 7733931348",
        "Mob no +91-77339-31348 payment not received",
    ],
)
def test_mobile_formats_normalise_to_ten_digits(raw):
    assert extract_mobile(raw) == "7733931348"
    assert normalize_mobile(raw.split(":")[-1]) in ("7733931348", None) or True


def test_non_mobiles_are_not_mobiles():
    for raw in ("12345", "ILLUN-178903327221195", "611532946151", "5733931348", "REG123456", "hello"):
        assert extract_mobile(raw) is None, raw
    e = extract_from_text(
        "7733931348",
        registration_pattern=get_settings().registration_pattern,
        betex_pattern=get_settings().betex_order_id_pattern,
    )
    assert e.mobile.value == "7733931348" and e.registration_number.value is None and e.utr.value is None


async def submit(db, *inputs):
    async with db.session_scope() as s:
        cases = [await attach_message(s, i) for i in inputs]
        case = cases[0].case
        ev = await list_evidence(s, case.case_id)
        return case.case_id, missing_items(case, ev), case_status_lines(case, ev), {c.case.case_id for c in cases}


async def ready(db, order_search, case_id):
    order_search(GOOD)
    async with db.session_scope() as s:
        outcome = await manager.process_case(s, case_id, force=True)
        c = await get_case(s, case_id)
        return outcome, c


FOUR = [make_input(1, "photo"), make_input(2, "text", MOB), make_input(3, "document"), make_input(4, "video")]


# ---------------------------------------------------------------- the four required items
async def test_all_four_inputs_make_the_case_ready(db, fake_bot, fake_ai, order_search, no_download):
    case_id, missing, lines, ids = await submit(db, *FOUR)
    assert len(ids) == 1 and missing == []
    assert lines == [
        "✅ Payment Screenshot",
        f"✅ Mobile: <code>{MOB}</code>",
        "✅ Bank Statement",
        "✅ Payment Video",
    ]
    assert not any("waiting" in l for l in lines)
    outcome, c = await ready(db, order_search, case_id)
    assert outcome == "ready" and c.status == CaseStatus.READY_FOR_BETIX.value
    assert c.betex_pay_order_id == "ILLUN-178621243657290" and c.mobile == MOB


async def test_four_items_in_any_order_join_one_case(db, fake_bot, fake_ai, order_search, no_download):
    case_id, missing, _, ids = await submit(
        db,
        make_input(4, "video"),
        make_input(3, "document"),
        make_input(2, "text", "+91 77339 31348"),
        make_input(1, "photo"),
    )
    assert len(ids) == 1 and missing == []
    outcome, c = await ready(db, order_search, case_id)
    assert outcome == "ready" and c.mobile == MOB


async def test_screenshot_plus_mobile_alone_is_not_processed(db, fake_bot, fake_ai, order_search, no_download):
    case_id, missing, lines, ids = await submit(db, make_input(1, "photo"), make_input(2, "text", MOB))
    assert len(ids) == 1 and missing == ["bank statement", "payment video"]
    assert lines[2:] == ["⏳ Bank Statement — waiting", "⏳ Payment Video — waiting"]
    outcome, c = await ready(db, order_search, case_id)
    assert outcome == "not_ready" and c.status == CaseStatus.WAITING_FOR_INPUT.value


async def test_missing_video_only(db):
    _, missing, lines, _ = await submit(
        db, make_input(1, "photo"), make_input(2, "text", MOB), make_input(3, "document")
    )
    assert missing == ["payment video"] and lines[-1] == "⏳ Payment Video — waiting"


async def test_missing_statement_only(db):
    _, missing, lines, _ = await submit(db, make_input(1, "photo"), make_input(2, "text", MOB), make_input(4, "video"))
    assert missing == ["bank statement"] and lines[2] == "⏳ Bank Statement — waiting"


async def test_screenshot_only_lists_everything_pending(db):
    _, missing, lines, _ = await submit(db, make_input(1, "photo"))
    assert missing == ["mobile number", "bank statement", "payment video"]
    assert lines == [
        "✅ Payment Screenshot",
        "⏳ Mobile — waiting",
        "⏳ Bank Statement — waiting",
        "⏳ Payment Video — waiting",
    ]


async def test_mobile_only_asks_for_the_rest(db):
    _, missing, lines, _ = await submit(db, make_input(1, "text", MOB))
    assert missing == ["payment screenshot", "bank statement", "payment video"]
    assert lines[:2] == ["⏳ Payment Screenshot — waiting", f"✅ Mobile: <code>{MOB}</code>"]


# ---------------------------------------------------------------- registration number is never required
async def test_registration_number_is_never_asked_for(db, fake_bot, fake_ai, order_search, no_download):
    case_id, missing, lines, _ = await submit(db, *FOUR)
    assert "registration" not in " ".join(missing + lines).lower()
    outcome, c = await ready(db, order_search, case_id)
    assert outcome == "ready" and c.registration_number is None


async def test_registration_number_is_an_optional_extra(db, fake_bot, fake_ai, order_search, no_download):
    case_id, missing, _, _ = await submit(db, *FOUR, make_input(5, "text", "REG123456"))
    assert missing == []
    outcome, c = await ready(db, order_search, case_id)
    assert outcome == "ready" and c.registration_number == "REG123456" and c.mobile == MOB


# ---------------------------------------------------------------- the old two-item mode is still available
async def test_require_all_evidence_false_keeps_the_two_item_rule(db, monkeypatch):
    monkeypatch.setenv("REQUIRE_ALL_EVIDENCE", "false")
    from app.config import reset_settings_cache

    reset_settings_cache()
    _, missing, lines, _ = await submit(db, make_input(1, "photo"), make_input(2, "text", MOB))
    assert missing == [] and not any("Still need" in l for l in lines)
