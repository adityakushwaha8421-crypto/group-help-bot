"""One Chromium for the whole process, a few logged-in Illunise tabs handed out one search at a time.

Before: every order search launched its own browser and logged in (seconds of work and hundreds of MB each), and
nothing limited how many ran at once. Now `find_candidates` borrows a tab from this pool: at most
ADMIN_BROWSER_PAGES searches run in parallel, the rest wait their turn; a tab that breaks is thrown away and
replaced; a crashed browser is relaunched on the next use."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from app.admin.browser import AdminBrowser
from app.config import get_settings
from app.utils.logging import get_logger

log = get_logger("admin.pool")


class BrowserPool:
    def __init__(self, size: int | None = None, session_factory: Callable[[], Awaitable[Any]] | None = None):
        self.size = size or get_settings().admin_browser_pages
        self._sem = asyncio.Semaphore(self.size)
        self._idle: list[Any] = []
        self._busy = 0
        self._pw = None
        self._browser = None
        self._launch_lock = asyncio.Lock()
        self._factory = session_factory  # tests inject fake sessions; production makes AdminBrowser tabs

    # ---- the browser itself
    async def _browser_ready(self):
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._browser is not None:
                log.warning("admin browser lost; relaunching")
                self._idle.clear()
            from playwright.async_api import async_playwright

            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=get_settings().admin_headless)
            log.info("admin browser launched", pages=self.size)
            return self._browser

    async def _new_session(self):
        if self._factory is not None:
            return await self._factory()
        browser = await self._browser_ready()
        return await AdminBrowser(shared_browser=browser).__aenter__()

    @staticmethod
    async def _discard(sess) -> None:
        try:
            await sess.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    # ---- borrowing a tab
    @asynccontextmanager
    async def page(self):
        """Borrow a logged-in tab; blocks while all `size` tabs are busy. Broken tabs are not returned."""
        async with self._sem:
            sess = None
            while self._idle and sess is None:
                cand = self._idle.pop()
                if getattr(cand, "is_alive", lambda: True)():
                    sess = cand
                else:
                    await self._discard(cand)
            if sess is None:
                sess = await self._new_session()
            self._busy += 1
            ok = False
            try:
                yield sess
                ok = True
            finally:
                self._busy -= 1
                if ok and getattr(sess, "is_alive", lambda: True)():
                    self._idle.append(sess)
                else:
                    await self._discard(sess)  # whatever failed, the next search starts from a fresh tab

    def stats(self) -> dict[str, int]:
        return {"size": self.size, "busy": self._busy, "idle": len(self._idle), "waiting": max(0, -self._sem._value)}

    async def close(self) -> None:
        for sess in self._idle:
            await self._discard(sess)
        self._idle.clear()
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = self._pw = None


_pool: BrowserPool | None = None


def get_pool() -> BrowserPool:
    global _pool
    if _pool is None:
        _pool = BrowserPool()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
