"""The bot never stays silent. Whatever the operator sends gets an answer at the bottom of the chat: the case card,
a line saying which existing case took the messages, or - when something could not be processed - what failed and
why. (Live 2026-11-25: a withdrawal id and its statement were sent twice and the operator saw no reply at all.)"""

from types import SimpleNamespace

import pytest

from app.cases import manager
from app.db.repository import get_case
from app.telegram import input_bot
from app.workers import tasks
from tests.conftest import make_input
from tests.test_flow import MOBILE


class Chat:
    def __init__(self, sink, inp):
        self.chat = SimpleNamespace(id=inp.chat_id, type="private")
        self.message_id = inp.message_id
        self._sink = sink

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self._sink.append(text)
        return SimpleNamespace(message_id=900 + self.message_id)


@pytest.fixture
def jobs(monkeypatch, fake_poster):
    calls = []

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        calls.append((name, args, defer_seconds))

    monkeypatch.setattr(input_bot, "enqueue", fake_enqueue)
    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    manager.set_poster_factory(lambda: fake_poster)
    input_bot._held.clear()
    input_bot._flush_tasks.clear()
    yield calls
    for t in input_bot._flush_tasks.values():
        t.cancel()
    input_bot._held.clear()
    input_bot._flush_tasks.clear()
    manager.set_poster_factory(None)


async def send(sink, *inputs):
    for i in inputs:
        input_bot.hold(Chat(sink, i), i)
    input_bot._flush_tasks[111].cancel()
    return await input_bot.flush_chat(111)


async def test_a_message_that_cannot_be_taken_in_is_reported_and_the_rest_goes_on(db, fake_bot, jobs, monkeypatch):
    real = input_bot.ingest

    async def flaky(inp):
        if inp.kind == "document":
            raise ValueError("value too long for type character varying(60)")
        return await real(inp)

    monkeypatch.setattr(input_bot, "ingest", flaky)
    sink = []
    touched = await send(sink, make_input(1, "text", "WD-50663-89662"), make_input(2, "document"))
    assert len(touched) == 1  # the withdrawal id still opened its case
    failed = [t for t in sink if "COULD NOT PROCESS" in t]
    assert len(failed) == 1 and "PhonePe_Statement.pdf" in failed[0]
    assert "ValueError" in failed[0] and "value too long" in failed[0] and "send it again" in failed[0]
    assert any("New Withdrawal" in t for t in sink)


async def test_a_crash_of_the_whole_batch_is_reported_too(db, fake_bot, jobs, monkeypatch):
    async def boom(chat_id):
        raise RuntimeError("database is not reachable")

    monkeypatch.setattr(input_bot, "flush_chat", boom)
    await input_bot._flush_later(111, 0)
    assert any("COULD NOT PROCESS" in t and "database is not reachable" in t for _, t in fake_bot.sent)


async def test_sending_the_same_messages_again_is_answered(db, fake_bot, jobs):
    sink = []
    await send(sink, make_input(1, "text", "WD-50663-89662"), make_input(2, "document"))
    sink.clear()
    again = await send(sink, make_input(1, "text", "WD-50663-89662"), make_input(2, "document"))  # redelivered
    assert again == [] and sink == ["↩️ Already received — this file is on a case already, nothing new was added."]


async def test_plain_chatter_is_still_not_answered(db, fake_bot, jobs):
    sink = []
    await send(sink, make_input(1, "photo"))
    sink.clear()
    await send(sink, make_input(2, "text", "thanks"))
    assert sink == []


async def test_messages_that_change_nothing_on_an_open_case_are_answered(db, fake_bot, jobs, monkeypatch):
    """They join the case whose card sits further up; the card's text stays the same - one line says so."""
    sink = []
    (case_id,) = await send(sink, make_input(1, "photo"))
    assert len(sink) == 1

    async def not_modified(text, *, chat_id, message_id, **kw):
        raise RuntimeError("Bad Request: message is not modified")

    monkeypatch.setattr(fake_bot, "edit_message_text", not_modified)
    sink_before = len(sink)
    await send(sink, make_input(5, "photo"))  # a second screen of the same payment: the card reads the same
    new = sink[sink_before:]
    assert len(new) == 1 and "Nothing new for case" in new[0] and "/status" in new[0] and case_id in new[0]


async def test_a_card_that_does_change_is_edited_without_an_extra_line(db, fake_bot, jobs):
    sink = []
    await send(sink, make_input(1, "photo"))
    await send(sink, make_input(2, "text", MOBILE))
    assert len(sink) == 1 and len(fake_bot.edits) == 1  # ONE message per case, edited - as always


async def test_messages_for_a_case_already_posted_are_answered(db, fake_bot, jobs):
    sink = []
    (case_id,) = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        c.status, c.betix_root_message_id, c.betex_pay_order_id = "WAITING_FOR_CONFIRMATION", 501, "ILLUN-1789"
    sink.clear()
    await send(sink, make_input(3, "document"))  # the statement arrives after the case went out
    assert len(sink) == 1 and "ILLUN-1789" in sink[0] and "already posted" in sink[0]
    assert "waiting for confirmation" in sink[0] and "Sending it as a reply to the original post" in sink[0]
    assert any(j[0] == "late_evidence_job" for j in jobs)
