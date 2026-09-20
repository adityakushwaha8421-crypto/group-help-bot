"""The Refund click itself: only the exact payout, only from Success, never twice, and only the panel showing
Refunded counts as done. (A fake page stands in for Illunise - no real payout is ever touched by the tests.)"""

import pytest

from app.admin import payouts
from app.admin.payouts import (
    ALREADY_REFUNDED,
    NOT_CONFIRMED,
    NOT_FOUND,
    NOT_SUCCESS,
    REFUND_FAILED,
    REFUNDED,
    Payout,
    refund_on_page,
)

WD = "WD-53074-68176"


class Dialog:
    def __init__(self, message, kind="confirm"):
        self.message, self.type, self.accepted = message, kind, None

    async def accept(self):
        self.accepted = True

    async def dismiss(self):
        self.accepted = False


class Page:
    def __init__(self, button="Refund", dialog_id=WD):
        self.button, self.dialog_id = button, dialog_id
        self.clicks, self.dialogs, self._handlers = 0, [], []

    def on(self, _event, fn):
        self._handlers.append(fn)

    def remove_listener(self, _event, fn):
        self._handlers.remove(fn)

    async def inner_text(self, _sel, timeout=0):
        return self.button

    async def click(self, _sel):
        self.clicks += 1
        d = Dialog(f"Refund payout {self.dialog_id}?\n\nA refund row will be queued...")
        self.dialogs.append(d)
        for fn in list(self._handlers):
            await fn(d)

    async def wait_for_timeout(self, _ms):
        return None


class Browser:
    def __init__(self, page):
        self.page = page


@pytest.fixture
def panel(monkeypatch, env):
    """statuses: what the panel shows on each successive read of the payout."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "refund_poll_seconds", 0, raising=False)
    state = {"statuses": ["Success", "Success", "Refunded"], "reads": 0}

    async def _read(browser, wd):
        assert wd == WD
        i = min(state["reads"], len(state["statuses"]) - 1)
        state["reads"] += 1
        st = state["statuses"][i]
        return None if st is None else Payout(wd, account="1234567890", amount=4854.5, status=st)

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(payouts, "read_payout", _read)
    monkeypatch.setattr(payouts.asyncio, "sleep", _no_sleep)
    return state


async def test_success_is_refunded_and_confirmed_by_the_panel(panel):
    page = Page()
    r = await refund_on_page(Browser(page), WD)
    assert r.outcome == REFUNDED and r.ok and (r.status_before, r.status_after) == ("Success", "Refunded")
    assert page.clicks == 1 and page.dialogs[0].accepted is True


async def test_already_refunded_is_never_clicked(panel):
    panel["statuses"] = ["Refunded"]
    page = Page(button="Refunded")
    r = await refund_on_page(Browser(page), WD)
    assert r.outcome == ALREADY_REFUNDED and r.ok and page.clicks == 0


@pytest.mark.parametrize("status", ["Processing", "Failed", "Pending", ""])
async def test_anything_but_success_is_never_clicked(panel, status):
    panel["statuses"] = [status]
    page = Page()
    r = await refund_on_page(Browser(page), WD)
    assert r.outcome == NOT_SUCCESS and not r.ok and page.clicks == 0


async def test_an_unknown_id_is_never_clicked(panel):
    panel["statuses"] = [None]
    page = Page()
    r = await refund_on_page(Browser(page), WD)
    assert r.outcome == NOT_FOUND and page.clicks == 0


async def test_a_confirm_box_naming_another_payout_is_refused(panel):
    page = Page(dialog_id="WD-11111-22222")
    r = await refund_on_page(Browser(page), WD)
    assert r.outcome == REFUND_FAILED and not r.ok and page.dialogs[0].accepted is False


async def test_a_queued_refund_is_waited_for_not_clicked_again(panel):
    page = Page(button="Refund queued")
    r = await refund_on_page(Browser(page), WD)
    assert r.outcome == REFUNDED and page.clicks == 0


async def test_still_success_after_the_wait_is_not_solved(panel, monkeypatch):
    from app.config import get_settings

    panel["statuses"] = ["Success"]
    monkeypatch.setattr(get_settings(), "refund_poll_seconds", 60, raising=False)
    monkeypatch.setattr(get_settings(), "refund_wait_seconds", 180, raising=False)
    page = Page()
    r = await refund_on_page(Browser(page), WD)
    assert r.outcome == NOT_CONFIRMED and not r.ok and page.clicks == 1
