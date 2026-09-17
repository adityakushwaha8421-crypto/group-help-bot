"""/add <mobile | registration | ORDER-ID>: complete a case that is already in the Betix group.

Everything sent after /add is attached to THAT case and posted as a REPLY to its original payment-screenshot
message - never a new case, never a standalone message."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.cases import manager
from app.db.models import Case, CaseStatus
from app.db.repository import get_case, list_evidence
from app.telegram import add_evidence, input_bot
from app.workers import tasks
from tests.conftest import make_input
from tests.test_flow import GOOD, MOBILE

ROOT = 501  # the screenshot post FakePoster gives the case


class Msg:
    """Just enough of an aiogram Message for the handlers."""

    def __init__(self, inp, sink):
        self.chat = SimpleNamespace(id=inp.chat_id, type="private")
        self.from_user = SimpleNamespace(id=inp.user_id, username="me", full_name="me", is_bot=False)
        self.message_id = inp.message_id
        self._sink = sink

    async def answer(self, text, parse_mode=None):
        self._sink.append(text)
        return SimpleNamespace(message_id=990)


@pytest.fixture
def add_setup(fake_poster, monkeypatch):
    jobs = []

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        jobs.append((name, args))

    monkeypatch.setattr(input_bot, "enqueue", fake_enqueue)
    manager.set_poster_factory(lambda: fake_poster)
    add_evidence._sessions.clear()
    yield jobs
    add_evidence._sessions.clear()
    manager.set_poster_factory(None)


async def posted_without_statement(db, order_search, poster):
    """A case force-sent to Betix with only the screenshot (statement + video still missing)."""
    from app.cases.correlation import attach_message

    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", MOBILE))
        case_id = r.case.case_id
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True, force_send=True) == "ready"
        assert await manager.post_case_to_betix(s, case_id, poster) == "posted"
    return case_id


async def run_add(sink, chat_id, user_id, args):
    await input_bot.cmd_add(
        Msg(make_input(100, "text", chat_id=chat_id, user_id=user_id), sink), SimpleNamespace(args=args)
    )


async def send_file(sink, inp):
    sess = add_evidence.active(inp.chat_id)
    await input_bot.add_to_case(Msg(inp, sink), inp, sess)


async def test_add_by_mobile_then_statement_and_video_reply_to_the_same_screenshot(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup
):
    case_id = await posted_without_statement(db, order_search, fake_poster)
    assert fake_poster.media == [("payment_screenshot", "ILLUN-178621243657290")]
    sink = []
    await run_add(sink, 111, 111, MOBILE)
    assert "🗂 Found case" in sink[0] and "Still missing the bank statement and payment video" in sink[0]

    await send_file(sink, make_input(30, "document"))
    assert add_setup[-1] == ("added_evidence_job", (case_id,))
    assert await tasks.added_evidence_job({}, case_id) == 1
    assert fake_poster.media[-1][0] == "bank_statement" and fake_poster.media_replies[-1] == ("bank_statement", ROOT)

    await send_file(sink, make_input(31, "video"))
    assert await tasks.added_evidence_job({}, case_id) == 1
    assert fake_poster.media[-1][0] == "payment_video" and fake_poster.media_replies[-1] == ("payment_video", ROOT)

    async with db.session_scope() as s:
        assert len((await s.execute(select(Case))).scalars().all()) == 1  # no new case
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value  # still monitored
        assert {e.type for e in await list_evidence(s, case_id)} == {
            "payment_screenshot",
            "bank_statement",
            "payment_video",
        }
    assert all(m[1] is None or m[1] == ROOT for m in fake_poster.media_replies[1:])  # nothing standalone


async def test_add_accepts_an_extra_screenshot_too(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup
):
    case_id = await posted_without_statement(db, order_search, fake_poster)
    sink = []
    await run_add(sink, 111, 111, MOBILE)
    await send_file(sink, make_input(32, "photo"))
    assert await tasks.added_evidence_job({}, case_id) == 1
    assert fake_poster.media_replies[-1] == ("payment_screenshot", ROOT)


async def test_add_by_order_id_and_by_case_id(db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup):
    case_id = await posted_without_statement(db, order_search, fake_poster)
    async with db.session_scope() as s:
        for q in ("ILLUN-178621243657290", case_id, MOBILE):
            found = await add_evidence.find_case(s, q)
            assert found is not None and found.case_id == case_id


async def test_case_not_found(db, fake_bot, add_setup):
    sink = []
    await run_add(sink, 111, 111, "9999999999")
    assert sink == ["❌ <b>Case not found.</b>"]
    assert add_evidence.active(111) is None


async def test_a_file_sent_without_add_still_starts_a_normal_case(db, fake_bot, add_setup):
    assert add_evidence.active(111) is None
    r = await input_bot.ingest(make_input(40, "photo"))
    assert r["created"]


async def test_cancel_stops_adding(db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup):
    await posted_without_statement(db, order_search, fake_poster)
    sink = []
    await run_add(sink, 111, 111, MOBILE)
    assert add_evidence.active(111) is not None
    await input_bot.cmd_cancel(Msg(make_input(101, "text"), sink))
    assert add_evidence.active(111) is None and "Stopped adding evidence" in sink[-1]


async def test_the_same_file_twice_is_not_added_twice(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup
):
    case_id = await posted_without_statement(db, order_search, fake_poster)
    sink = []
    await run_add(sink, 111, 111, MOBILE)
    await send_file(sink, make_input(30, "document"))
    await tasks.added_evidence_job({}, case_id)
    before = len(fake_poster.media)
    await send_file(sink, make_input(30, "document"))
    assert "Not added" in sink[-1]
    assert await tasks.added_evidence_job({}, case_id) == 0 and len(fake_poster.media) == before


async def test_add_is_in_the_command_menu():
    assert "add" in [c for c, _ in input_bot.BOT_COMMANDS]
    assert "/add" in input_bot.help_text()


# ---------------------------------------------------------------- /add must not outlive its purpose
# Live 2026-09-15 18:30: an /add opened at 17:33 was still active an hour later and swallowed the NEXT customer's
# screenshot, statement and video - posting them under the wrong case in the Betix group.


async def test_add_closes_itself_once_the_case_has_everything(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup
):
    sink = []
    await posted_without_statement(db, order_search, fake_poster)
    await run_add(sink, 111, 111, MOBILE)
    await send_file(sink, make_input(20, "document"))
    assert add_evidence.active(111) is not None  # video still missing: stays open
    await send_file(sink, make_input(21, "video"))
    assert add_evidence.active(111) is None  # everything is in: closed by itself
    assert "/add</code> closed" in sink[-1]


async def test_a_forward_from_another_customer_is_not_added(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup, monkeypatch
):
    sink, held = [], []
    case_id = await posted_without_statement(db, order_search, fake_poster)
    async with db.session_scope() as s:
        (await get_case(s, case_id)).original_user_id = 555  # the case's customer
    await run_add(sink, 111, 111, MOBILE)

    def fake_hold(message, inp):  # the real hold() is synchronous
        held.append(inp.message_id)
        return True

    monkeypatch.setattr(input_bot, "hold", fake_hold)
    other = make_input(30, "document", forward=SimpleNamespace(user_id=777, username="someone_else"))
    monkeypatch.setattr(input_bot, "_incoming_from_message", lambda message: other)  # the handler's own parsing
    await input_bot.on_evidence(Msg(other, sink))
    assert held == [30]  # took the normal path
    assert add_evidence.active(111) is None and "different customer" in sink[-1]
    async with db.session_scope() as s:
        assert [e.type for e in await list_evidence(s, case_id)] == ["payment_screenshot"]  # nothing was added


async def test_a_forward_from_the_same_customer_is_added(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup, monkeypatch
):
    sink = []
    case_id = await posted_without_statement(db, order_search, fake_poster)
    async with db.session_scope() as s:
        (await get_case(s, case_id)).original_user_id = 555
    await run_add(sink, 111, 111, MOBILE)
    mine = make_input(31, "document", forward=SimpleNamespace(user_id=555, username="the_customer"))
    monkeypatch.setattr(input_bot, "_incoming_from_message", lambda message: mine)
    await input_bot.on_evidence(Msg(mine, sink))
    async with db.session_scope() as s:
        assert "bank_statement" in [e.type for e in await list_evidence(s, case_id)]


async def test_add_expires(db, fake_bot, fake_ai, order_search, no_download, fake_poster, add_setup):
    from datetime import timedelta

    sink = []
    await posted_without_statement(db, order_search, fake_poster)
    await run_add(sink, 111, 111, MOBILE)
    sess = add_evidence.active(111)
    sess.started_at = sess.started_at - timedelta(minutes=11)
    assert add_evidence.active(111) is None  # ten minutes is plenty; after that files are normal submissions
