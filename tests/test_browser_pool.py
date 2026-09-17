"""The Illunise browser pool: a fixed number of tabs shared by every search, reused between searches, replaced
when they break, and never more than `size` searches running at the same time."""

import asyncio

import pytest

from app.admin.pool import BrowserPool


class FakeTab:
    made = 0

    def __init__(self):
        FakeTab.made += 1
        self.id = FakeTab.made
        self.alive = True
        self.closed = False

    def is_alive(self):
        return self.alive

    async def __aexit__(self, *exc):
        self.closed = True


@pytest.fixture(autouse=True)
def reset():
    FakeTab.made = 0


async def make_tab():
    return FakeTab()


async def test_never_more_than_size_searches_at_once():
    pool = BrowserPool(size=3, session_factory=make_tab)
    running, peak = 0, 0

    async def search(i):
        nonlocal running, peak
        async with pool.page():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

    await asyncio.gather(*(search(i) for i in range(10)))
    assert peak == 3 and FakeTab.made == 3  # ten searches, three tabs, never a fourth
    assert pool.stats() == {"size": 3, "busy": 0, "idle": 3, "waiting": 0}


async def test_tabs_are_reused_not_relaunched():
    pool = BrowserPool(size=2, session_factory=make_tab)
    for _ in range(6):
        async with pool.page() as tab:
            assert isinstance(tab, FakeTab)
    assert FakeTab.made == 1  # sequential searches share one tab


async def test_a_failed_search_throws_its_tab_away():
    pool = BrowserPool(size=1, session_factory=make_tab)
    with pytest.raises(RuntimeError):
        async with pool.page() as tab:
            first = tab
            raise RuntimeError("Illunise timed out")
    assert first.closed and pool.stats()["idle"] == 0
    async with pool.page() as tab:  # the next search gets a fresh tab, not the broken one
        assert tab is not first and FakeTab.made == 2


async def test_a_dead_idle_tab_is_replaced():
    pool = BrowserPool(size=1, session_factory=make_tab)
    async with pool.page() as tab:
        pass
    tab.alive = False  # the browser context died while idle
    async with pool.page() as tab2:
        assert tab2 is not tab and tab.closed


async def test_close_releases_everything():
    pool = BrowserPool(size=2, session_factory=make_tab)
    async with pool.page() as a:
        pass
    await pool.close()
    assert a.closed and pool.stats()["idle"] == 0
