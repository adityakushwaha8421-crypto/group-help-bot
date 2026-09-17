"""Telegram send spacing: same-chat calls are spaced, different chats are independent, a flood wait is slept
through and retried, and unrelated errors pass straight through."""

import asyncio
import time

import pytest
from aiogram.exceptions import TelegramRetryAfter

from app.telegram.throttle import SendThrottle

GROUP, ME, YOU = -1009999, 111, 222


def retry_after(seconds):
    return TelegramRetryAfter(method=None, message="Flood control exceeded", retry_after=seconds)


async def test_same_chat_calls_are_spaced():
    th = SendThrottle(group_interval=0.05, private_interval=0.05, per_second=100)
    stamps = []

    async def call():
        stamps.append(time.monotonic())
        return len(stamps)

    results = await asyncio.gather(*(th.run(GROUP, call) for _ in range(4)))
    assert sorted(results) == [1, 2, 3, 4]
    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
    assert all(g >= 0.045 for g in gaps), gaps


async def test_different_chats_do_not_wait_on_each_other():
    th = SendThrottle(group_interval=0.3, private_interval=0.3, per_second=100)

    async def call():
        return time.monotonic()

    t0 = time.monotonic()
    await asyncio.gather(th.run(GROUP, call), th.run(ME, call), th.run(YOU, call))
    assert time.monotonic() - t0 < 0.2  # three chats, no spacing between them


async def test_a_flood_wait_is_slept_through_and_retried():
    th = SendThrottle(group_interval=0.0, private_interval=0.0, per_second=100)
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        if calls["n"] == 1:
            raise retry_after(0.05)
        return "sent"

    t0 = time.monotonic()
    assert await th.run(GROUP, call) == "sent"
    assert calls["n"] == 2 and time.monotonic() - t0 >= 0.05
    assert th.stats() == {"chats": 1, "flood_waits": 1}


async def test_gives_up_after_repeated_flood_waits():
    th = SendThrottle(group_interval=0.0, private_interval=0.0, per_second=100, max_retries=2)

    async def call():
        raise retry_after(0.01)

    with pytest.raises(TelegramRetryAfter):
        await th.run(GROUP, call)


async def test_other_errors_pass_straight_through():
    th = SendThrottle(group_interval=0.0, private_interval=0.0, per_second=100)

    async def call():
        raise RuntimeError("Bad Request: chat not found")

    with pytest.raises(RuntimeError):
        await th.run(ME, call)


async def test_group_and_private_intervals_differ():
    th = SendThrottle(group_interval=3.0, private_interval=0.4, per_second=100)
    assert th._interval(GROUP) == 3.0 and th._interval(ME) == 0.4 and th._interval("@betixgroup") == 3.0
