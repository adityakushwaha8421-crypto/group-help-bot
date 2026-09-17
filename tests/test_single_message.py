"""One message per case in the operator chat: the flush creates it, later batches edit it."""

from types import SimpleNamespace

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
        self._sink.append(("send", text))
        return SimpleNamespace(message_id=self._n)


async def drive(sink, batches, monkeypatch):
    enq = []

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        enq.append((name, args))

    monkeypatch.setattr(input_bot, "enqueue", fake_enqueue)
    input_bot._held.clear()
    for batch in batches:
        for inp in batch:
            input_bot.hold(Chat(sink, inp), inp)
        input_bot._flush_tasks[111].cancel()
        await input_bot.flush_chat(111)
    return enq


async def test_two_batches_produce_one_message_that_is_edited(db, fake_bot, monkeypatch):
    sink = []
    enq = await drive(
        sink,
        [
            [make_input(1, "photo"), make_input(2, "text", MOBILE)],
            [make_input(3, "document")],
            [make_input(4, "video")],
        ],
        monkeypatch,
    )
    assert [k for k, _ in sink] == ["send"]  # exactly one message sent
    assert sink[0][1].startswith("🆕 <b>New Case</b>") and "⏱ Waiting 30s for the" in sink[0][1]
    assert len(fake_bot.edits) == 2 and all(mid == 901 for _, mid, _ in fake_bot.edits)  # the rest are edits of it
    assert "⏳ Payment Video — waiting" in fake_bot.edits[0][2]
    assert "✅ Payment Video" in fake_bot.edits[1][2] and "🔎 All evidence received." in fake_bot.edits[1][2]
    # force timer armed (v1), re-armed by the statement (v2), then the video completes the case: processed (v3)
    assert [a for _, a in enq] == [(a[0], 1, True, True) for _, a in enq[:1]] + [
        (enq[0][1][0], 2, True, True),
        (enq[0][1][0], 3, True),
    ]


async def test_edit_failure_falls_back_to_a_new_message(db, fake_bot, monkeypatch):
    async def broken(text, *, chat_id, message_id, **kw):
        raise RuntimeError("message to edit not found")

    fake_bot.edit_message_text = broken
    sink = []
    await drive(sink, [[make_input(1, "photo")], [make_input(2, "text", MOBILE)]], monkeypatch)
    assert [k for k, _ in sink] == ["send", "send"]
