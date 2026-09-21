"""WITHDRAWAL STATEMENT DATE CHECK: the statement must reach the withdrawal date. One that ends before it is not
accepted - nothing goes to Betix, the operator is asked for a newer statement, and the SAME case carries on when
it arrives."""

from datetime import date

import pytest

from app.admin.payouts import Payout, parse_payout_page
from app.cases import manager
from app.cases.correlation import attach_message, missing_items
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence
from app.evidence.statement_dates import dates_in, last_date_in
from app.telegram.progress import progress_card
from tests.conftest import make_input

WD = "WD-84425-67115"
TODAY = date(2026, 9, 22)


def test_dates_are_read_in_every_usual_format():
    text = "01/09/2026  5-9-26  18 Sep 2026  19-Sep-2026  Sep 20, 2026  2026-09-21  21.09.2026  7 September, 2026"
    got = dates_in(text)
    for d in (
        date(2026, 9, 1),
        date(2026, 9, 5),
        date(2026, 9, 18),
        date(2026, 9, 19),
        date(2026, 9, 20),
        date(2026, 9, 21),
        date(2026, 9, 7),
    ):
        assert d in got, d
    assert date(2026, 1, 9) not in got  # day first: 01/09/2026 is 1 September


def test_the_last_date_a_statement_reaches():
    text = (
        "Statement period: 01/09/2026 to 18/09/2026\nDate of birth 04/02/1991  A/c opened 12-03-2012\n"
        "Card valid till 09/2031  Next due 05/11/2026\n17/09/2026 UPI/DR 500.00\n18/09/2026 UPI/CR 900.00"
    )
    assert last_date_in(text, TODAY) == date(2026, 9, 18)  # not the birthday, not a future due date
    assert last_date_in("no dates here", TODAY) is None
    assert last_date_in("Ref 123456789012 amount 1,00,000.00", TODAY) is None  # numbers are not dates


def test_the_withdrawal_date_comes_from_the_payout_page():
    page = f"Payout {WD}\nSTATUS\nSuccess\nACCOUNT\n50100123451231\nTIMELINE\nCREATED\n20 Sep 2026 12:52\nLAST UPDATED\n20 Sep 2026 12:56"
    p = parse_payout_page(WD, page)
    assert p.created == "20 Sep 2026 12:52" and p.created_date == date(2026, 9, 20)
    assert Payout(WD).created_date is None


async def _case(db) -> str:
    async with db.session_scope() as s:
        cid = (await attach_message(s, make_input(1, "text", WD))).case.case_id
        await attach_message(s, make_input(2, "document"))
    return cid


@pytest.mark.parametrize(
    "ends, outcome", [("2026-09-20", "ready"), ("2026-09-25", "ready"), ("2026-09-19", "statement_outdated")]
)
async def test_the_statement_must_reach_the_withdrawal_date(
    db, fake_bot, no_download, fake_poster, payout, ends, outcome
):
    payout(statement_ends=ends)  # the withdrawal was created on 20 Sep 2026
    cid = await _case(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, cid, force=True) == outcome


async def test_an_old_statement_is_refused_and_a_newer_one_is_asked_for(db, fake_bot, no_download, fake_poster, payout):
    payout(statement_ends="2026-09-18")
    cid = await _case(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, cid, force=True) == "statement_outdated"
        c = await get_case(s, cid)
        assert c.status == CaseStatus.WAITING_FOR_INPUT.value
        assert missing_items(c, await list_evidence(s, cid)) == ["newer bank statement"]
        card = progress_card(c)
        assert await manager.post_case_to_betix(s, cid, fake_poster) != "posted"
    assert fake_poster.texts == []  # nothing reached Betix
    ask = [t for _, t in fake_bot.sent if "NEWER STATEMENT NEEDED" in t]
    assert len(ask) == 1 and "Withdrawal date: 20 Sep 2026" in ask[0] and "The statement ends: 18 Sep 2026" in ask[0]
    assert "includes 20 Sep 2026 or a later date" in ask[0] and "NEWER STATEMENT NEEDED" in card
    assert not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)


async def test_the_newer_statement_continues_the_same_case(db, fake_bot, no_download, fake_poster, payout):
    seen = payout(statement_ends="2026-09-18")
    cid = await _case(db)
    async with db.session_scope() as s:
        await manager.process_case(s, cid, force=True)
    seen["statement_ends"] = "2026-09-21"  # what the NEXT statement will show
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(3, "document"))
        assert r.case.case_id == cid and not r.created  # same case, not a new one
        assert missing_items(r.case, await list_evidence(s, cid)) == []
    async with db.session_scope() as s:
        assert await manager.process_case(s, cid, force=True) == "ready"
        assert await manager.post_case_to_betix(s, cid, fake_poster) == "posted"
    assert fake_poster.texts[0][0] == f"BX{WD}"


async def test_unreadable_dates_are_kept_for_a_person(db, fake_bot, no_download, fake_poster, payout):
    payout(statement_ends=None)
    cid = await _case(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, cid, force=True) == "escalated"
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW" in t][-1]
    assert "dates could not be read" in alert and "20 Sep 2026" in alert and fake_poster.texts == []


async def test_no_date_check_when_the_panel_shows_no_creation_date(db, fake_bot, no_download, fake_poster, payout):
    payout(created=None, statement_ends="2020-01-01")
    cid = await _case(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, cid, force=True) == "ready"
