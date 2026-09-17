"""FORCE SEND: screenshot + mobile are required; the bank statement and payment video are waited for FORCE_SEND_SECONDS
(30 s) after the latest input - every new file restarts the wait - and then the case is processed and sent to Betix
with whatever has arrived. Late files still go out as replies to the screenshot."""

from types import SimpleNamespace

import pytest

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case
from app.telegram import input_bot
from app.workers import tasks
from tests.conftest import make_input
from tests.test_flow import GOOD, MOBILE


class Chat:
    def __init__(self, sink, inp):
        self.chat = SimpleNamespace(id=inp.chat_id, type="private")
        self.message_id = inp.message_id
        self._sink, self._n = sink, 900

    async def answer(self, text, parse_mode=None):
        self._n += 1
        self._sink.append(text)
        return SimpleNamespace(message_id=self._n)


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
    return (await input_bot.flush_chat(111))[0]


async def test_screenshot_and_mobile_are_force_sent_after_30_seconds(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs
):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    assert jobs == [("process_case_job", (case_id, 1, True, True), 30)]
    order_search(GOOD)
    # the 30 s are up: the job runs -> processed and posted with what there is
    assert await tasks.process_case_job({}, case_id, 1, True, True) == "ready"
    assert jobs[-1][0] == "post_case_job"
    assert await tasks.post_case_job({}, case_id) == "posted"
    assert fake_poster.media == [("payment_screenshot", "ILLUN-178621243657290")]  # nothing else to send
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value


async def test_a_new_file_restarts_the_30_seconds(db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    await send(sink, make_input(3, "document"))  # statement arrives during the wait
    assert [j[1] for j in jobs] == [(case_id, 1, True, True), (case_id, 2, True, True)] and jobs[-1][2] == 30
    order_search(GOOD)
    assert await tasks.process_case_job({}, case_id, 1, True, True) == "stale"  # the old timer does nothing
    assert await tasks.process_case_job({}, case_id, 2, True, True) == "ready"
    await tasks.post_case_job({}, case_id)
    assert [m[0] for m in fake_poster.media] == ["payment_screenshot", "bank_statement"]  # the statement went too


async def test_everything_in_goes_at_once_without_waiting(db, fake_bot, fake_ai, order_search, no_download, jobs):
    sink = []
    case_id = await send(
        sink, make_input(1, "photo"), make_input(2, "text", MOBILE), make_input(3, "document"), make_input(4, "video")
    )
    assert jobs == [("process_case_job", (case_id, 1, True), 0)]


async def test_no_force_send_without_the_screenshot_or_the_mobile(db, fake_bot, fake_ai, order_search, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(3, "document"))  # no mobile
    assert jobs == []
    assert "⏳ Payment Video — waiting" in sink[0]
    async with db.session_scope() as s:  # even a forced run refuses without the mobile
        assert await manager.process_case(s, case_id, force=True, force_send=True) == "not_ready"


async def test_late_video_after_the_force_send_goes_as_a_reply(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, jobs
):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    order_search(GOOD)
    await tasks.process_case_job({}, case_id, 1, True, True)
    await tasks.post_case_job({}, case_id)
    await send(sink, make_input(4, "video"))
    assert jobs[-1][0] == "late_evidence_job"
    assert await tasks.late_evidence_job({}, case_id) == 1
    assert fake_poster.media[-1][0] == "payment_video" and fake_poster.media_replies[-1] == ("payment_video", 501)


async def test_force_send_can_be_switched_off(db, fake_bot, jobs, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("FORCE_SEND_SECONDS", "0")
    reset_settings_cache()
    sink = []
    await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    assert jobs == [] and "⏳ Bank Statement — waiting" in sink[0]


async def test_restart_recovery_arms_the_force_send(db, fake_bot, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    jobs.clear()
    await tasks.recover()
    assert [(n, a[0], a[2:]) for n, a, _ in jobs] == [("process_case_job", case_id, (True, True))]
