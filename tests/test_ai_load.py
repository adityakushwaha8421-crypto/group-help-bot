"""AI reads under load: a file is read once, not on every retry/restart; a failed read is retried; and no more
than AI_MAX_CONCURRENCY model calls are in flight at once."""

import asyncio

from app.ai import analyzer
from app.ai.analyzer import AIUnavailable
from app.cases import manager
from app.db.repository import get_case
from tests.test_flow import GOOD, submit_four_messages


async def test_a_file_is_read_once_across_reprocessing(db, fake_bot, fake_ai, order_search, no_download):
    order_search([])  # nothing found: the case escalates, then we run it again as a retry / restart would
    case_id = await submit_four_messages(db)
    async with db.session_scope() as s:
        await manager.process_case(s, case_id, force=True)
    first = fake_ai.calls
    assert first >= 2  # screenshot + statement (+ video frames)
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        c.status = "WAITING_FOR_INPUT"  # re-run as a restart recovery does
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
    assert fake_ai.calls == first  # nothing was sent to the model again


async def test_a_failed_read_is_retried_not_reused(db, fake_bot, fake_ai, order_search, no_download, monkeypatch):
    monkeypatch.setattr(manager, "enqueue", _noop)
    real = fake_ai.analyze_files

    async def down(files, *, doc_type_hint, context_hint=""):
        raise AIUnavailable("no credits left on the OpenAI account")

    fake_ai.analyze_files = down
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ai_unavailable"
    fake_ai.analyze_files = real
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
    assert fake_ai.calls >= 2  # the files were read once the service was back


async def _noop(*a, **k):
    return None


async def test_no_more_than_the_cap_in_flight(monkeypatch):
    monkeypatch.setenv("AI_MAX_CONCURRENCY", "3")
    from app.config import reset_settings_cache

    reset_settings_cache()
    analyzer._sem = None
    peak = {"now": 0, "max": 0}

    class Resp:
        output_text = '{"ok": true}'

    class Responses:
        async def create(self, **kw):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.02)
            peak["now"] -= 1
            return Resp()

    class Client:
        responses = Responses()

    monkeypatch.setattr(analyzer, "_client", lambda: Client())
    await asyncio.gather(*(analyzer._structured("s", [], {"name": "x", "schema": {}}) for _ in range(12)))
    assert peak["max"] == 3 and analyzer.ai_stats()["in_flight"] == 0
    analyzer._sem = None
