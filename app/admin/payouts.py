"""Illunise payouts (withdrawals): find one withdrawal and read which bank account it was sent to.

Live page, 2026-09-20: the list is /admin/payouts (GET `q`, `status`, `gateway`), every row links to
/admin/payouts/<WITHDRAW ID>. That View page prints label / value pairs one under the other:
BENEFICIARY, BANK ACCOUNT (bank name, branch), ACCOUNT, IFSC, UTR, AMOUNT, STATUS."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from app.admin.browser import AdminBrowser, LayoutChanged
from app.admin.login import ensure_logged_in
from app.admin.pool import get_pool
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
