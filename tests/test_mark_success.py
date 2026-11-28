"""Betix: "the payment has not been received ... we have decided to make an exception and close this order."
The bot asks the operator ONCE; only their approval makes it mark the order Success in Illunise (the callback goes
with it), and the case is closed only once the panel SHOWS Success. (Fakes stand in for Illunise.)"""

import pytest
from sqlalchemy import select

from app.admin import mark_success as ms
from app.admin.mark_success import MARKED, NOT_CONFIRMED, MarkResult, mark_success_on_page, parse_order_status
from app.cases import manager
from app.db.models import AuditLog, CaseStatus
from app.db.repository import get_case
from app.telegram.betix_monitor import handle_group_message
from app.telegram.confirmation import classify_human_message, classify_system_message
from tests.conftest import make_group_msg
from tests.test_flow import GOOD, run_until_ready

ORDER = GOOD[0]["betex_order_id"]
NOTICE = (
    "Dear Merchant,\nAfter multiple reviews, we confirm that the payment has not been received, and no "
    "irregularities were found in the payment proof provided.\nConsidering our long-term partnership and customer "
    "experience, we have decided to make an exception and close this order.\nThank you for your understanding and "
    "cooperation. ☺️\n\nPI2610036jmpv4vfafr\n217142977101\n{order}"
)


# ------------------------------------------------------------------ the wording
def test_the_notice_is_its_own_outcome_not_a_failure():
    import re

    pats = (re.compile(r"[A-Z]{3,10}-\d{10,}"), re.compile(r"PI\w{10,}"))
    for classify in (classify_system_message, classify_human_message):
        c = classify(NOTICE.format(order=ORDER), *pats)
        assert c.outcome == "EXCEPTION_CLOSED" and ORDER in c.order_ids
    plain = classify_system_message("we confirm that the payment has not been received", *pats)
    assert plain.outcome == "FAILED"  # without the exception: still a failure


# ------------------------------------------------------------------ the flow
@pytest.fixture
def jobs(monkeypatch):
    queued = []

    async def _enqueue(name, *args, **kw):
        queued.append((name, *args))

    monkeypatch.setattr(manager, "enqueue", _enqueue)
    return queued


@pytest.fixture
def marker():
    calls = []
    holder = {"result": MarkResult(MARKED, "Failed", "Success", "Failed -> Success", "Success")}

    async def _mark(order_id, utr):
        calls.append((order_id, utr))
        return holder["result"]

    manager.set_marker(_mark)
    yield calls, holder
    manager.set_marker(None)


async def closed_case(db, order_search, fake_poster, order=ORDER) -> str:
    case_id, _ = await run_until_ready(db, order_search, GOOD)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        r = await handle_group_message(s, make_group_msg(700, NOTICE.format(order=order), sender_username="Wendy"))
        assert r["action"] == ("approval_asked" if order == ORDER else r["action"])
    return case_id


def texts(fake_bot, word):
    return [t for _, t in fake_bot.sent if word in t]


async def test_the_notice_asks_the_operator_and_marks_nothing(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs, marker
):
    calls, _ = marker
    cid = await closed_case(db, order_search, fake_poster)
    ask = texts(fake_bot, "APPROVAL NEEDED")
    assert len(ask) == 1 and ORDER in ask[0] and "Wendy" in ask[0]
    assert calls == [] and ("mark_success_job", cid) not in jobs
    assert await manager.mark_success_approved(cid) == "skipped"  # no approval: never marked
    async with db.session_scope() as s:
        c = await get_case(s, cid)
        assert c.status != CaseStatus.VERIFIED.value and c.followup_cancelled  # no more "Any update?"
        # the same notice again asks nothing new
        await handle_group_message(s, make_group_msg(701, NOTICE.format(order=ORDER), sender_username="Wendy"))
    assert len(texts(fake_bot, "APPROVAL NEEDED")) == 1 and calls == []


async def test_approval_marks_the_exact_order_and_closes_the_case(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs, marker
):
    calls, _ = marker
    cid = await closed_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        assert await manager.approve_mark_success(s, cid, "@boss") == "queued"
        assert await manager.approve_mark_success(s, cid, "@boss") == "already"  # a second tap does nothing
    assert jobs.count(("mark_success_job", cid)) == 1
    assert await manager.mark_success_approved(cid) == "verified"
    async with db.session_scope() as s:
        c = await get_case(s, cid)
        assert calls == [(ORDER, c.utr)]  # this order, with the UTR read off the screenshot
        assert c.status == CaseStatus.VERIFIED.value
        row = (await s.execute(select(AuditLog).where(AuditLog.action == "ORDER_MARKED_SUCCESS"))).scalars().one()
        assert row.actor == "@boss" and row.result == "marked" and row.details["order_id"] == ORDER
    assert texts(fake_bot, "marked Success")
    assert await manager.mark_success_approved(cid) == "skipped" and len(calls) == 1  # never twice


async def test_a_mark_the_panel_does_not_show_is_never_reported_as_done(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs, marker
):
    _, holder = marker
    holder["result"] = MarkResult(NOT_CONFIRMED, "Failed", "Failed", "still Failed 30s after the click")
    cid = await closed_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        await manager.approve_mark_success(s, cid, "@boss")
    assert await manager.mark_success_approved(cid) == "escalated"
    async with db.session_scope() as s:
        c = await get_case(s, cid)
        assert c.status == CaseStatus.ESCALATED.value and "NOT marked" in (c.failure_reason or "")


async def test_without_the_notice_an_approval_is_refused(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs, marker
):
    calls, _ = marker
    case_id, _ = await run_until_ready(db, order_search, GOOD)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        assert await manager.approve_mark_success(s, case_id, "@boss") == "not_applicable"
    assert await manager.mark_success_approved(case_id) == "skipped" and calls == []


async def test_a_notice_from_before_is_saved_as_success_without_button_or_panel(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs, marker
):
    from app.db.repository import add_betix_message
    from app.utils.timeutil import utcnow

    calls, _ = marker
    case_id, _ = await run_until_ready(db, order_search, GOOD)
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        # how the older code left it: the notice stored with the case, the case in Manual Review
        await add_betix_message(
            s,
            case_id=case_id,
            chat_id=-1009999,
            message_id=650,
            direction="in",
            kind="reply",
            text=NOTICE.format(order=ORDER),
            sender_username="Wendy",
            sent_at=utcnow(),
        )
        await manager.escalate_case(s, await get_case(s, case_id), "Betix reports the payment as FAILED.")
    async with db.session_scope() as s:
        assert await manager.close_past_exception_notices(s) == {"exception_notices_closed": 1}
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.VERIFIED.value and c.confirmed_by == "@Wendy"
        assert await manager.close_past_exception_notices(s) == {}  # once
    assert calls == [] and not jobs.count(("mark_success_job", case_id))  # the panel is never touched
    assert not texts(fake_bot, "APPROVAL NEEDED")  # and nobody is asked


async def test_a_notice_in_the_approval_flow_is_never_closed_by_the_sweep(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs, marker
):
    cid = await closed_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        assert await manager.close_past_exception_notices(s) == {}
        assert (await get_case(s, cid)).status != CaseStatus.VERIFIED.value  # still waiting for the approval


# ------------------------------------------------------------------ the click itself
class Dialog:
    def __init__(self, message, kind):
        self.message, self.type, self.reply = message, kind, "unanswered"

    async def accept(self, text=None):
        self.reply = True if text is None else text

    async def dismiss(self):
        self.reply = None


class Locator:
    def __init__(self, n):
        self.n = n

    async def count(self):
        return self.n


class Page:
    """An order page: `statuses`/`callbacks` are what it shows on each successive load."""

    def __init__(self, statuses, callbacks=("Pending",), buttons=True, confirm="Mark this order as SUCCESS"):
        self.statuses, self.callbacks, self.buttons, self.confirm = list(statuses), list(callbacks), buttons, confirm
        self.loads, self.clicked, self.dialogs, self._handlers = 0, [], [], []

    def _at(self, seq):
        return seq[min(max(self.loads - 1, 0), len(seq) - 1)]

    async def goto(self, url, wait_until=None):
        self.loads += 1
        return None

    async def inner_text(self, _sel):
        return f"Order\n{ORDER}\nSTATUS\n{self._at(self.statuses)}\nAMOUNT\n₹2,000.00"

    async def evaluate(self, _js):
        return self._at(self.callbacks)

    def locator(self, selector):
        return Locator(1 if self.buttons and ORDER in selector else 0)

    def on(self, _e, fn):
        self._handlers.append(fn)

    def remove_listener(self, _e, fn):
        self._handlers.remove(fn)

    async def click(self, selector):
        self.clicked.append("resend" if "resendCallback" in selector else "mark")
        if "resendCallback" in selector:
            seq = [Dialog("Resend callback to the registered URL for this order?", "confirm")]
        else:
            seq = [Dialog("Enter UTR number (leave blank to skip):", "prompt")]
        for d in seq:
            self.dialogs.append(d)
            for fn in list(self._handlers):
                await fn(d)
        if "markSuccess" in selector and self.dialogs[-1].reply not in (None, "unanswered"):
            utr = self.dialogs[-1].reply
            d = Dialog(f"{self.confirm}{' with UTR ' + utr if utr else ''} and fire the callback?", "confirm")
            self.dialogs.append(d)
            for fn in list(self._handlers):
                await fn(d)

    async def wait_for_timeout(self, _ms):
        return None


class Browser:
    def __init__(self, page):
        self.page = page

    def url(self, path):
        return "https://panel.test" + path


@pytest.fixture
def quick(monkeypatch):
    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(ms, "ensure_logged_in", _none)
    monkeypatch.setattr(ms.asyncio, "sleep", _none)


def test_the_status_is_read_only_off_this_orders_page():
    assert parse_order_status(ORDER, f"Order\n{ORDER}\nSTATUS\n✕ Failed\nAMOUNT\n1") == "Failed"
    assert parse_order_status(ORDER, "Order\nILLUN-1\nSTATUS\nFailed") is None


async def test_failed_is_marked_with_the_utr_and_confirmed_by_the_panel(quick):
    page = Page(["FAILED", "Success"], ["Pending", "Success"])
    r = await mark_success_on_page(Browser(page), ORDER, "217142977101")
    assert r.outcome == MARKED and r.ok and (r.status_before, r.status_after) == ("FAILED", "Success")
    assert page.clicked == ["mark"] and [d.reply for d in page.dialogs] == ["217142977101", True]
    assert not r.callback_resent  # Mark as Success fired it


async def test_a_callback_still_pending_is_resent_once(quick):
    page = Page(["FAILED", "Success", "Success"], ["Pending", "Pending", "Success"])
    r = await mark_success_on_page(Browser(page), ORDER, None)
    assert r.ok and r.callback_resent and r.callback == "Success"
    assert page.clicked == ["mark", "resend"] and page.dialogs[0].reply == ""  # no UTR known: left blank


async def test_already_success_is_never_clicked(quick):
    page = Page(["Success"])
    r = await mark_success_on_page(Browser(page), ORDER, None)
    assert r.outcome == ms.ALREADY_SUCCESS and r.ok and page.clicked == []


async def test_no_button_or_another_question_marks_nothing(quick):
    page = Page(["FAILED"], buttons=False)
    assert (await mark_success_on_page(Browser(page), ORDER, None)).outcome == ms.NO_BUTTON and page.clicked == []
    page = Page(["FAILED"], confirm="Delete this order")
    r = await mark_success_on_page(Browser(page), ORDER, None)
    assert r.outcome == ms.MARK_FAILED and not r.ok and page.dialogs[-1].reply is None  # the question was refused


async def test_a_click_the_panel_does_not_show_is_not_done(quick):
    page = Page(["FAILED"])
    r = await mark_success_on_page(Browser(page), ORDER, None)
    assert r.outcome == NOT_CONFIRMED and not r.ok
