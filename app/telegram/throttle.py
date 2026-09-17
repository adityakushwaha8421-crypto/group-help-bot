"""Spacing for Bot API calls so a burst of cases never trips Telegram's limits.

Telegram allows roughly 1 message/second into a private chat, 20/minute into a group and ~30/second overall.
Beyond that it answers 429 with a `retry_after`; before this module that surfaced as a failed post - and a failed
post escalated the case. Every send, edit and delete now goes through `run()`: calls to the same chat are spaced
out, a flood wait is slept through and the call retried, and different chats never wait on each other."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable, TypeVar

from app.config import get_settings
from app.utils.logging import get_logger

log = get_logger("tg.throttle")
T = TypeVar("T")


class SendThrottle:
    def __init__(
        self,
        *,
        group_interval: float | None = None,
        private_interval: float | None = None,
        per_second: int | None = None,
        max_retries: int = 3,
    ):
        s = get_settings()
        self.group_interval = s.tg_group_send_interval_seconds if group_interval is None else group_interval
        self.private_interval = s.tg_private_send_interval_seconds if private_interval is None else private_interval
        self.per_second = s.tg_sends_per_second if per_second is None else per_second
        self.max_retries = max_retries
        self._locks: dict[int | str, asyncio.Lock] = {}
        self._next: dict[int | str, float] = {}
        self._recent: deque[float] = deque()
        self.flood_waits = 0

    def _interval(self, chat_id: int | str) -> float:
        try:
            return self.group_interval if int(chat_id) < 0 else self.private_interval
        except (TypeError, ValueError):
            return self.group_interval  # "@username" chats are groups/channels

    async def _global_slot(self) -> None:
        while True:
            now = time.monotonic()
            while self._recent and now - self._recent[0] > 1.0:
                self._recent.popleft()
            if len(self._recent) < self.per_second:
                self._recent.append(now)
                return
            await asyncio.sleep(self._recent[0] + 1.0 - now)

    async def run(self, chat_id: int | str, call: Callable[[], Awaitable[T]]) -> T:
        """Perform one Bot API call to `chat_id`, spaced and flood-wait safe. Other errors pass straight through."""
        from aiogram.exceptions import TelegramRetryAfter

        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            last_exc: Exception | None = None
            for attempt in range(self.max_retries + 1):
                wait = self._next.get(chat_id, 0.0) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                await self._global_slot()
                try:
                    result = await call()
                except TelegramRetryAfter as exc:
                    self.flood_waits += 1
                    last_exc = exc
                    pause = float(exc.retry_after) + 0.5
                    log.warning("telegram flood wait", chat=chat_id, seconds=pause, attempt=attempt + 1)
                    self._next[chat_id] = time.monotonic() + pause
                    continue
                self._next[chat_id] = time.monotonic() + self._interval(chat_id)
                return result
            assert last_exc is not None
            raise last_exc

    def stats(self) -> dict[str, int]:
        return {"chats": len(self._locks), "flood_waits": self.flood_waits}


_throttle: SendThrottle | None = None


def get_throttle() -> SendThrottle:
    global _throttle
    if _throttle is None:
        _throttle = SendThrottle()
    return _throttle


def reset_throttle() -> None:
    global _throttle
    _throttle = None
