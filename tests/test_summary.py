"""/summary: today's cases by stage + the open ones, in one card."""

from datetime import timedelta

from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import CaseStatus
from app.db.repository import get_case
from app.telegram import input_bot
from app.telegram.summary import build_summary, counts, period_start
from app.utils.timeutil import utcnow
from tests.conftest import make_input
from tests.test_flow import MOBILE, run_until_ready


def test_period_start_is_local_midnight():
    start = period_start(1, "Asia/Kolkata")
    local = start.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    assert (local.hour, local.minute) == (0, 0) and start <= utcnow()
    assert period_start(7, "Asia/Kolkata") == period_start(1, "Asia/Kolkata") - timedelta(days=6)


async def test_summary_counts_and_open_list(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, _ = await run_until_ready(db, order_search)  # READY_FOR_BETIX
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)  # -> WAITING_FOR_CONFIRMATION
        c2 = (
            await attach_message(s, make_input(9, "photo", chat_id=222, user_id=222, username="ops"))
        ).case  # collecting
        c3 = (await attach_message(s, make_input(10, "photo", chat_id=333, user_id=333))).case
        await manager.fail_case(s, c3, "test")  # FAILED
    async with db.session_scope() as s:
        text = await build_summary(s, 1)
    assert text.startswith("📊 <b>TODAY</b>")
    assert "Total cases: <b>3</b>" in text and "Waiting on Betix: <b>1</b>" in text
    assert "Collecting evidence: <b>1</b>" in text
    assert "Closed without a match: <b>1</b>" in text and "Confirmed: <b>0</b>" in text
    assert (
        "Open cases (2)" in text and "ILLUN-178621243657290" in text and c2.case_id in text and c3.case_id not in text
    )
    assert "👀" in text and MOBILE in text


async def test_summary_week_label_and_no_open_cases(db):
    async with db.session_scope() as s:
        text = await build_summary(s, 7)
    assert "<b>LAST 7 DAYS</b>" in text and "Total cases: <b>0</b>" in text and "No open cases right now" in text


async def test_summary_default_is_all_time(db):
    from datetime import timedelta

    async with db.session_scope() as s:
        old = (await attach_message(s, make_input(1, "photo"))).case
        old.created_at = utcnow() - timedelta(days=40)  # far outside any daily window
        (await attach_message(s, make_input(2, "photo", chat_id=222, user_id=222))).case
    async with db.session_scope() as s:
        text = await build_summary(s)
        today = await build_summary(s, 1)
    assert "<b>ALL TIME</b>" in text and "Total cases: <b>2</b>" in text and "All cases ever" in text
    assert "📊 <b>TODAY</b>" in today and "Total cases: <b>1</b>" in today


def test_counts_buckets():
    class C:  # noqa: D401
        def __init__(self, st):
            self.status = st

    n = counts(
        [
            C(CaseStatus.VERIFIED.value),
            C(CaseStatus.FOLLOWUP_2_SENT.value),
            C(CaseStatus.SEARCHING_ORDER.value),
            C(CaseStatus.ORDER_MATCH_AMBIGUOUS.value),
            C(CaseStatus.ESCALATED.value),
        ]
    )
    assert n == {
        "total": 5,
        "verified": 1,
        "waiting": 1,
        "working": 1,
        "collecting": 0,
        "ambiguous": 1,
        "escalated": 1,
        "already_success": 0,
        "already_sent": 0,
        "failed": 0,
    }


def test_removed_commands_are_gone():
    src = open(input_bot.__file__, encoding="utf-8").read()
    for cmd in ("retry", "select", "verify", "fail", "done"):
        assert f'Command("{cmd}")' not in src and f'("{cmd}",' not in src and f"/{cmd} " not in src
    assert ("summary", "All-time overview by stage (or /summary 7 for a week)") in input_bot.BOT_COMMANDS


async def test_evidence_auto_creates_cases_no_new_command(db):
    src = open(input_bot.__file__, encoding="utf-8").read()
    assert 'Command("new")' not in src and '("new",' not in src
    async with db.session_scope() as s:
        a = (await attach_message(s, make_input(1, "photo"))).case  # first message creates the case
        same = (await attach_message(s, make_input(2, "text", MOBILE))).case  # joins it
        b = (await attach_message(s, make_input(3, "photo"))).case  # a NEW screenshot = a new case
        dup = await attach_message(s, make_input(4, "photo", file_unique_id="u3"))  # the same file again: no new case
    assert a.case_id == same.case_id and b.case_id != a.case_id
    assert dup.duplicate and dup.case.case_id == b.case_id


async def test_order_id_is_the_case_id_once_known(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    from app.db.repository import find_case_by_order_id
    from app.telegram.progress import progress_card
    from app.telegram.ui import case_label

    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert case_label(c) == "ILLUN-178621243657290" != case_id
        assert "<code>ILLUN-178621243657290</code>" in progress_card(c)
        assert (await find_case_by_order_id(s, "illun-178621243657290")).case_id == case_id
        text = await build_summary(s, 1)
        assert "ILLUN-178621243657290" in text and case_id not in text
        await manager.post_case_to_betix(s, case_id, fake_poster)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        await manager.verify_case(s, c, confirmed_by="Betix")
    solved = [t for _, t in fake_bot.sent if "PAYMENT CONFIRMED" in t][0]
    assert "<code>ILLUN-178621243657290</code>" in solved
