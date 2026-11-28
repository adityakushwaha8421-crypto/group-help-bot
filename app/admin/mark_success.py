"""Mark ONE Illunise order as Success - only ever after the operator approved it in the bot.

Live page, 2026-11-28: /admin/orders/<ORDER ID> carries two buttons,
  <button onclick="markSuccess('<ID>',this)">✅ Mark as Success</button>
  <button onclick="resendCallback('<ID>',this)">🔁 Resend Callback</button>
markSuccess() asks prompt("Enter UTR number (leave blank to skip):"), then confirm("Mark this order as SUCCESS
[with UTR ...] and fire the callback?"), POSTs /admin/orders/<ID>/mark-success and reloads; the button then reads
"✓ Marked" (or "✗ <error>"). The page prints STATUS and, in the "Callback URL" row, the callback's own status."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from app.admin.browser import AdminBrowser
from app.admin.login import ensure_logged_in
from app.admin.pool import get_pool
from app.utils.logging import get_logger

log = get_logger("admin.mark_success")

MARKED, ALREADY_SUCCESS, NOT_FOUND, NO_BUTTON, MARK_FAILED, NOT_CONFIRMED = (
    "marked",
    "already_success",
    "not_found",
    "no_button",
    "mark_failed",
    "not_confirmed",
)
WAIT_SECONDS, POLL_SECONDS = 30, 2


@dataclass
class MarkResult:
    outcome: str
    status_before: str | None = None
    status_after: str | None = None
    detail: str = ""
    callback: str | None = None  # the callback's status as the page prints it afterwards
    callback_resent: bool = False

    @property
    def ok(self) -> bool:
        """The panel SHOWS Success - the only thing that may be reported as done."""
        return self.outcome in (MARKED, ALREADY_SUCCESS)


def parse_order_status(order_id: str, text: str) -> str | None:
    """The order page's text -> its STATUS. None when the page is not this very order's page."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    if not any(order_id.upper() in x.upper() for x in lines):
        return None
    for label, value in zip(lines, lines[1:]):
        if label.upper() == "STATUS":
            return re.sub(r"^[^A-Za-z]+", "", value).strip() or None
    return None


def _is_success(status: str | None) -> bool:
    return (status or "").strip().lower() in ("success", "successful", "paid")


async def read_order_page(browser: AdminBrowser, order_id: str) -> tuple[str | None, str | None]:
    """(status, callback status) of exactly this order; (None, None): the panel has no such order page."""
    page = browser.page
    resp = await page.goto(browser.url(f"/admin/orders/{order_id}"), wait_until="domcontentloaded")
    if resp is not None and resp.status == 404:
        return None, None
    status = parse_order_status(order_id, await page.inner_text("body"))
    callback = await page.evaluate(
        """() => { for (const tr of document.querySelectorAll('table tr')) { const c=[...tr.children].map(x=>x.innerText.trim());
             if (c.length >= 3 && /^callback url$/i.test(c[0])) return c[c.length-1]; } return null; }"""
    )
    return status, (callback or None)


async def _click(page, selector: str, answer) -> list[str]:
    """Click one button and answer its dialogs with `answer(dialog) -> str | bool | None` (None: dismiss)."""
    seen: list[str] = []

    async def on_dialog(dialog):
        seen.append(dialog.message)
        reply = answer(dialog)
        if reply is None or reply is False:
            await dialog.dismiss()
        elif reply is True:
            await dialog.accept()
        else:
            await dialog.accept(reply)

    page.on("dialog", on_dialog)
    try:
        await page.click(selector)
        await page.wait_for_timeout(2500)
    finally:
        page.remove_listener("dialog", on_dialog)
    return seen


async def mark_success_on_page(browser: AdminBrowser, order_id: str, utr: str | None = None) -> MarkResult:
    """Mark ONE order, by its exact id, as Success and report what the panel shows afterwards.

    Never clicks when: the page is not this very order, it is already Success, or the page has no Mark as Success
    button for exactly this id. The UTR prompt gets `utr` (blank when unknown - never an invented one); the
    confirm() is accepted only when it is the Mark-as-SUCCESS question."""
    await ensure_logged_in(browser)
    utr = (utr or "").strip()
    before, callback = await read_order_page(browser, order_id)
    if before is None:
        return MarkResult(NOT_FOUND, detail=f"{order_id} is not in the Illunise orders")
    if _is_success(before):
        return MarkResult(ALREADY_SUCCESS, before, before, "already Success - nothing clicked", callback)
    page = browser.page
    selector = f"""button[onclick*="markSuccess('{order_id}'"]"""
    if await page.locator(selector).count() != 1:
        return MarkResult(NO_BUTTON, before, before, "the page has no Mark as Success button for this order", callback)

    def answer(dialog):
        if dialog.type == "prompt" and dialog.message.startswith("Enter UTR"):
            return utr or ""
        if dialog.type == "confirm" and dialog.message.startswith("Mark this order as SUCCESS"):
            return True if (not utr or utr in dialog.message) else None
        return None  # anything else: never confirm what we did not ask for

    seen = await _click(page, selector, answer)
    if len(seen) < 2 or not seen[1].startswith("Mark this order as SUCCESS"):
        return MarkResult(MARK_FAILED, before, before, f"unexpected dialog: {(seen or ['none'])[-1][:120]}", callback)
    log.info("mark as success clicked", order_id=order_id)

    waited, after = 0, before
    while waited < WAIT_SECONDS:
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        after, callback = await read_order_page(browser, order_id)
        if _is_success(after):
            break
    if not _is_success(after):
        return MarkResult(
            NOT_CONFIRMED, before, after, f"still {after or 'unknown'} {waited}s after the click", callback
        )
    result = MarkResult(MARKED, before, after, f"{before} -> {after}", callback)
    # Mark as Success fires the callback itself. Only when the page still shows it as not delivered: resend once.
    if callback and re.search(r"pending|fail|error", callback, re.I):
        resend = f"""button[onclick*="resendCallback('{order_id}'"]"""
        if await page.locator(resend).count() == 1:
            await _click(
                page,
                resend,
                lambda d: True if d.type == "confirm" and d.message.startswith("Resend callback") else None,
            )
            result.callback_resent = True
            _, result.callback = await read_order_page(browser, order_id)
            log.info("callback resent", order_id=order_id, callback=result.callback)
    return result


async def mark_success_order(order_id: str, utr: str | None = None) -> MarkResult:
    async with get_pool().page() as browser:
        return await mark_success_on_page(browser, order_id, utr)
