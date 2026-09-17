"""CASE INPUT + 5-SECOND COLLECTION.

Every message is HELD; COLLECTION_SECONDS after the FIRST held message of a chat, everything held becomes ONE
case, ONE card is sent, and the case is checked once: all four required items in -> processed once; anything
missing -> the card says what and nothing runs. Later messages go through the same hold and, once the set is
complete, the case is processed. Registration number is optional and never blocks."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import Case, CaseStatus
from app.db.repository import get_case
from app.telegram import input_bot
from tests.conftest import make_input
from tests.test_flow import MOBILE


class Chat:
    def __init__(self, sink, inp):
        self.chat = SimpleNamespace(id=inp.chat_id, type="private")
        self.message_id = inp.message_id
        self._inp, self._sink, self._n = inp, sink, 900

    async def answer(self, text, parse_mode=None):
        self._n += 1
        self._sink.append(text)
        return SimpleNamespace(message_id=self._n)


@pytest.fixture
def enq(monkeypatch):
    calls = []

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        calls.append((name, args))

    monkeypatch.setattr(input_bot, "enqueue", fake_enqueue)
    input_bot._held.clear()
    input_bot._flush_tasks.clear()
    yield calls
    for t in input_bot._flush_tasks.values():
        t.cancel()
    input_bot._held.clear()
    input_bot._flush_tasks.clear()


async def test_first_message_opens_the_hold_and_nothing_is_sent_until_the_flush(db, fake_bot, enq):
    sink = []
    inputs = [make_input(1, "photo"), make_input(2, "text", MOBILE), make_input(3, "document"), make_input(4, "video")]
    opened = [input_bot.hold(Chat(sink, i), i) for i in inputs]
    assert opened == [True, False, False, False]  # one window, never restarted
    assert sink == [] and enq == []  # silence while holding
    async with db.session_scope() as s:
        assert (await s.execute(select(Case))).scalars().all() == []  # and NO case yet
    input_bot._flush_tasks[111].cancel()
    touched = await input_bot.flush_chat(111)
    assert len(touched) == 1 and len(sink) == 1  # ONE case, ONE card
    assert sink[0].startswith("🆕 <b>New Case</b>") and "🔎 All evidence received." in sink[0]
    assert [c[0] for c in enq] == ["process_case_job"]  # processed ONCE
    async with db.session_scope() as s:
        cases = (await s.execute(select(Case))).scalars().all()
        assert len(cases) == 1 and cases[0].mobile == MOBILE and cases[0].progress_message_id == 901


async def test_flush_with_gaps_shows_missing_and_arms_the_force_send(db, fake_bot, enq):
    sink = []
    for i in (make_input(1, "photo"), make_input(2, "text", MOBILE)):
        input_bot.hold(Chat(sink, i), i)
    input_bot._flush_tasks[111].cancel()
    first = await input_bot.flush_chat(111)
    # screenshot + mobile in: not processed now, but a FORCE SEND is armed for 30 s later
    assert enq == [("process_case_job", (first[0], 1, True, True))] and len(sink) == 1
    assert "⏳ Bank Statement — waiting" in sink[0] and "⏳ Payment Video — waiting" in sink[0]
    assert "⏱ Waiting 30s for the bank statement and payment video." in sink[0]
    # the rest arrives in a later batch: joins the same case, the ONE card is edited, processing starts
    for i in (make_input(3, "document"), make_input(4, "video")):
        input_bot.hold(Chat(sink, i), i)
    input_bot._flush_tasks[111].cancel()
    touched = await input_bot.flush_chat(111)
    assert len(sink) == 1 and len(fake_bot.edits) == 1 and fake_bot.edits[0][1] == 901
    assert "🔎 All evidence received." in fake_bot.edits[0][2]
    # complete now: processed at once (version 2), which makes the version-1 force timer stale
    assert enq[-1] == ("process_case_job", (touched[0], 2, True))


async def test_the_hold_really_waits_collection_seconds(db, fake_bot, enq, monkeypatch):
    monkeypatch.setenv("COLLECTION_SECONDS", "1")
    from app.config import reset_settings_cache

    reset_settings_cache()
    sink = []
    i1 = make_input(1, "photo", MOBILE)
    input_bot.hold(Chat(sink, i1), i1)
    await asyncio.sleep(0.3)
    assert sink == []
    for i in (make_input(3, "document"), make_input(4, "video")):
        input_bot.hold(Chat(sink, i), i)
    await asyncio.sleep(1.2)
    assert len(sink) == 1 and "🔎 All evidence received." in sink[0]
    assert [c[0] for c in enq] == ["process_case_job"]


async def test_registration_number_is_optional(db, fake_bot, enq):
    sink = []
    for i in (
        make_input(1, "photo"),
        make_input(2, "text", MOBILE),
        make_input(3, "document"),
        make_input(4, "video"),
        make_input(5, "text", "REG123456"),
    ):
        input_bot.hold(Chat(sink, i), i)
    input_bot._flush_tasks[111].cancel()
    touched = await input_bot.flush_chat(111)
    async with db.session_scope() as s:
        c = await get_case(s, touched[0])
        assert c.registration_number == "REG123456" and c.status == CaseStatus.WAITING_FOR_INPUT.value
    assert [c[0] for c in enq] == ["process_case_job"] and "registration" not in sink[0].lower()


def test_betix_gets_statement_password_and_video_by_default(monkeypatch):
    monkeypatch.delenv("BETIX_EXTRA_EVIDENCE_POLICY", raising=False)
    from app.config import get_settings, reset_settings_cache

    reset_settings_cache()
    assert get_settings().betix_extra_evidence_policy == "always"
