"""Many cases at once: one slow case must not block the others.

process_case now runs as short transactions - the row lock (and the pool connection's transaction) is released
while files are read by the AI and while the Illunise browser searches. On SQLite (the test DB) a long write
transaction blocks EVERY other writer, so "case B completes while case A is stuck in its search" proves the
lock is really released; on Postgres the same commit points free the row lock and the connection."""

import asyncio
import time

from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import CaseStatus
from app.db.repository import get_case
from tests.conftest import make_candidates, make_input
from tests.test_flow import GOOD, MOBILE


async def four(db, chat, msg0, mobile):
    async with db.session_scope() as s:
        cid = (await attach_message(s, make_input(msg0, "photo", chat_id=chat, user_id=chat))).case.case_id
        await attach_message(s, make_input(msg0 + 1, "text", mobile, chat_id=chat, user_id=chat))
        await attach_message(s, make_input(msg0 + 2, "document", chat_id=chat, user_id=chat))
        await attach_message(s, make_input(msg0 + 3, "video", chat_id=chat, user_id=chat))
    return cid


class GatedSearch:
    """The Illunise search: the FIRST call is held until released, every later call answers at once."""

    def __init__(self):
        self.release, self.reached, self.calls = asyncio.Event(), asyncio.Event(), 0

    async def __call__(self, query, amount=None, when=None):
        self.calls += 1
        n = self.calls
        if n == 1:
            self.reached.set()
            await self.release.wait()
        # every search finds a DIFFERENT order: one order can only ever belong to one case
        return make_candidates(
            [
                {
                    **c,
                    "betex_order_id": f"{c['betex_order_id']}{n:02d}",
                    "illunise_order_id": f"{c['illunise_order_id']}{n:02d}",
                }
                for c in GOOD
            ]
        )


async def test_a_slow_search_on_one_case_does_not_block_another(db, fake_bot, fake_ai, no_download, monkeypatch):
    gate = GatedSearch()
    manager.set_order_search(gate)
    try:
        a = await four(db, 111, 1, MOBILE)
        b = await four(db, 222, 10, MOBILE)  # same customer number: the fixture orders are hers

        async def run_a():
            async with db.session_scope() as s:
                return await manager.process_case(s, a, force=True)

        task_a = asyncio.create_task(run_a())
        await asyncio.wait_for(gate.reached.wait(), 5)  # A is now inside the (slow) search
        t0 = time.monotonic()
        async with db.session_scope() as s:  # B runs start to finish while A is stuck
            assert await manager.process_case(s, b, force=True) == "ready"
        assert time.monotonic() - t0 < 3, "B waited on A's transaction"
        async with db.session_scope() as s:
            assert (await get_case(s, a)).status == CaseStatus.SEARCHING_ORDER.value  # A's progress is visible
        gate.release.set()
        assert await asyncio.wait_for(task_a, 5) == "ready"
    finally:
        manager.set_order_search(None)


async def test_a_message_arriving_mid_search_stops_the_stale_run(db, fake_bot, fake_ai, no_download):
    gate = GatedSearch()
    manager.set_order_search(gate)
    try:
        a = await four(db, 111, 1, MOBILE)

        async def run_a():
            async with db.session_scope() as s:
                return await manager.process_case(s, a, force=True)

        task_a = asyncio.create_task(run_a())
        await asyncio.wait_for(gate.reached.wait(), 5)
        async with db.session_scope() as s:  # a new message for the case bumps its version (what flush_chat does)
            c = await get_case(s, a)
            c.processing_version += 1
        gate.release.set()
        assert await asyncio.wait_for(task_a, 5) == "stale"  # the newer job owns the case now
        async with db.session_scope() as s:
            c = await get_case(s, a)
            assert c.betex_pay_order_id is None and c.status == CaseStatus.SEARCHING_ORDER.value
    finally:
        manager.set_order_search(None)


async def test_many_cases_process_concurrently(db, fake_bot, fake_ai, no_download):
    gate = GatedSearch()
    gate.release.set()  # nobody is held: eight cases, eight searches, eight different orders
    manager.set_order_search(gate)
    ids = [await four(db, 100 + i, 10 * i + 1, MOBILE) for i in range(8)]

    async def run(cid):
        async with db.session_scope() as s:
            return await manager.process_case(s, cid, force=True)

    try:
        outcomes = await asyncio.gather(*(run(c) for c in ids))
    finally:
        manager.set_order_search(None)
    assert outcomes == ["ready"] * 8
