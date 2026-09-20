"""Illunise payouts (withdrawals): find one withdrawal and read which bank account it was sent to.

Live page, 2026-09-20: the list is /admin/payouts (GET `q`, `status`, `gateway`), every row links to
/admin/payouts/<WITHDRAW ID>. That View page prints label / value pairs one under the other:
BENEFICIARY, BANK ACCOUNT (bank name, branch), ACCOUNT, IFSC, UTR, AMOUNT, STATUS."""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass

from app.admin.browser import AdminBrowser, LayoutChanged
from app.admin.login import ensure_logged_in
from app.admin.pool import get_pool
from app.config import get_settings
from app.utils.logging import get_logger

log = get_logger("admin.payouts")

LABELS = {
    "BENEFICIARY": "beneficiary",
    "BANK ACCOUNT": "bank",
    "ACCOUNT": "account",
    "IFSC": "ifsc",
    "UTR": "utr",
    "AMOUNT": "amount",
    "STATUS": "status",
}


@dataclass
class Payout:
    withdraw_id: str
    account: str | None = None
    ifsc: str | None = None
    beneficiary: str | None = None
    bank: str | None = None
    utr: str | None = None
    amount: float | None = None
    status: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def parse_payout_page(withdraw_id: str, text: str) -> Payout | None:
    """The View page's text -> Payout. None when the page is not that withdrawal's page."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    if not any(x.upper() == f"PAYOUT {withdraw_id}".upper() or x.upper() == withdraw_id.upper() for x in lines):
        return None
    p = Payout(withdraw_id=withdraw_id)
    for label, value in zip(lines, lines[1:]):
        field = LABELS.get(label.upper())
        if not field or getattr(p, field) is not None or value.upper() in LABELS:
            continue
        if field == "amount":
            m = re.search(r"[\d,]+(?:\.\d+)?", value)
            p.amount = float(m.group(0).replace(",", "")) if m else None
        elif field == "account":
            digits = re.sub(r"[\s\-]", "", value)
            p.account = digits if re.fullmatch(r"[0-9A-Za-z]{6,24}", digits) else None
        else:
            setattr(p, field, value)
    return p


async def read_payout(browser: AdminBrowser, withdraw_id: str) -> Payout | None:
    await ensure_logged_in(browser)
    page = browser.page
    resp = await page.goto(browser.url(f"/admin/payouts/{withdraw_id}"), wait_until="domcontentloaded")
    if resp is not None and resp.status == 404:
        return None
    text = await page.inner_text("body")
    payout = parse_payout_page(withdraw_id, text)
    if payout is None:
        return None
    if payout.account is None:
        raise LayoutChanged(f"payout page for {withdraw_id} shows no ACCOUNT field")
    log.info("payout read", withdraw_id=withdraw_id, status=payout.status, account_tail=payout.account[-4:])
    return payout


async def find_payout(withdraw_id: str) -> Payout | None:
    """The exact withdrawal, through a pooled logged-in tab. None: Illunise has no such withdrawal id."""
    async with get_pool().page() as browser:
        return await read_payout(browser, withdraw_id)


# ------------------------------------------------------------------ refund (Betix reversed the withdrawal)
# Live page, 2026-09-20: <button id="refundBtn" onclick="refundPayout()"> asks confirm("Refund payout <ID>? ..."),
# then POSTs /admin/payouts/<ID>/refund. That only QUEUES the refund: the payout bot picks it up (polls every
# 20 s), credits the customer's wallet and acks back; the page's STATUS then reads "Refunded" and the button
# "Refunded" (disabled). While it is queued the button reads "Refund queued"; after a failed attempt "Failed".
REFUNDED, ALREADY_REFUNDED, NOT_SUCCESS, NOT_FOUND, NOT_CONFIRMED, REFUND_FAILED = (
    "refunded",
    "already_refunded",
    "not_success",
    "not_found",
    "not_confirmed",
    "refund_failed",
)


@dataclass
class RefundResult:
    outcome: str
    status_before: str | None = None
    status_after: str | None = None
    detail: str = ""
    payout: Payout | None = None

    @property
    def ok(self) -> bool:
        """The panel SHOWS Refunded - the only thing that may be reported as solved."""
        return self.outcome in (REFUNDED, ALREADY_REFUNDED)


def _is(status: str | None, word: str) -> bool:
    return (status or "").strip().lower() == word


async def _button_text(page) -> str:
    try:
        return (await page.inner_text("#refundBtn", timeout=3000)).strip()
    except Exception:  # noqa: BLE001
        return ""


async def refund_on_page(browser: AdminBrowser, withdraw_id: str) -> RefundResult:
    """Refund ONE payout, by its exact id, only from `Success`, and report what the panel shows afterwards.

    Never clicks when: the page is not this very withdrawal, the status is anything but Success, it is already
    Refunded, or a refund is already queued. The browser's confirm() is accepted only when it names this id."""
    s = get_settings()
    payout = await read_payout(browser, withdraw_id)
    if payout is None:
        return RefundResult(NOT_FOUND, detail=f"{withdraw_id} is not in Illunise payouts")
    before = payout.status
    if _is(before, "refunded"):
        return RefundResult(ALREADY_REFUNDED, before, before, "already Refunded - Refund not clicked", payout)
    if not _is(before, "success"):
        return RefundResult(NOT_SUCCESS, before, before, f"status is {before or 'unknown'}, not Success", payout)

    page = browser.page
    label = await _button_text(page)
    queued = "queued" in label.lower()
    if not queued:
        if label.lower() != "refund":
            return RefundResult(
                REFUND_FAILED, before, before, f"the Refund button reads {label or 'nothing'!r}", payout
            )
        dialogs: list[str] = []

        async def on_dialog(dialog):
            dialogs.append(dialog.message)
            if dialog.type == "confirm" and dialog.message.startswith(f"Refund payout {withdraw_id}?"):
                await dialog.accept()
            else:  # another id, or an alert: never confirm what we did not ask for
                await dialog.dismiss()

        page.on("dialog", on_dialog)
        try:
            await page.click("#refundBtn")
            await page.wait_for_timeout(2500)
        finally:
            page.remove_listener("dialog", on_dialog)
        if not dialogs or not dialogs[0].startswith(f"Refund payout {withdraw_id}?"):
            return RefundResult(
                REFUND_FAILED, before, before, f"unexpected dialog: {(dialogs or ['none'])[0][:120]}", payout
            )
        errors = [d for d in dialogs[1:] if "\u274c" in d or "error" in d.lower()]
        if errors:
            return RefundResult(REFUND_FAILED, before, before, errors[0][:160], payout)
        log.info("refund clicked", withdraw_id=withdraw_id)

    # The refund is processed by the payout bot, not by the click: wait until the PANEL says Refunded.
    waited = 0
    after = before
    while waited < s.refund_wait_seconds:
        await asyncio.sleep(s.refund_poll_seconds)
        waited += s.refund_poll_seconds
        now = await read_payout(browser, withdraw_id)
        after = now.status if now else None
        if _is(after, "refunded"):
            log.info("refund confirmed by the panel", withdraw_id=withdraw_id, waited=waited)
            return RefundResult(REFUNDED, before, after, f"Success -> Refunded after {waited}s", now)
        if "failed" in (await _button_text(browser.page)).lower():
            return RefundResult(REFUND_FAILED, before, after, "the panel reports the refund as Failed", now)
    return RefundResult(NOT_CONFIRMED, before, after, f"still {after or 'unknown'} {waited}s after Refund", payout)


async def refund_payout(withdraw_id: str) -> RefundResult:
    async with get_pool().page() as browser:
        return await refund_on_page(browser, withdraw_id)
