"""`/summary` — a one-card overview of ALL cases by stage (or the last N days with /summary N), plus the open ones."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import OPEN_STATUSES, Case, CaseStatus
from app.telegram.ui import b, case_label, code, esc, i, money, para
from app.utils.timeutil import fmt_local, utcnow

S = CaseStatus
COLLECTING = {S.WAITING_FOR_INPUT.value}
WORKING = {
    S.ANALYZING_EVIDENCE.value,
    S.SEARCHING_ORDER.value,
    S.CHECKING_ORDER_UPI.value,
    S.ORDER_MATCH_FOUND.value,
    S.READY_FOR_BETIX.value,
}
WAITING_BETIX = {
    S.POSTED_TO_BETIX.value,
    S.WAITING_FOR_CONFIRMATION.value,
    S.FOLLOWUP_1_SENT.value,
    S.FOLLOWUP_2_SENT.value,
}
ICON = {
    "WAITING_FOR_INPUT": "⏳",
    "ANALYZING_EVIDENCE": "🧠",
    "SEARCHING_ORDER": "🔎",
    "ORDER_MATCH_FOUND": "🎯",
    "ORDER_MATCH_AMBIGUOUS": "⚠️",
    "CHECKING_ORDER_UPI": "🏦",
    "READY_FOR_BETIX": "📤",
    "POSTED_TO_BETIX": "📨",
    "WAITING_FOR_CONFIRMATION": "👀",
    "FOLLOWUP_1_SENT": "🔔",
    "FOLLOWUP_2_SENT": "🔔",
    "VERIFIED": "✅",
    "ESCALATED": "🚨",
    "FAILED": "⛔",
    "ALREADY_SUCCESS": "♻️",
    "ALREADY_SENT": "🔁",
}


def period_start(days: int, tz: str) -> datetime:
    """Midnight (local) of today for days=1, of (days-1) days ago otherwise, as an aware UTC datetime."""
    local_now = utcnow().astimezone(ZoneInfo(tz))
    start = (local_now - timedelta(days=max(days, 1) - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(ZoneInfo("UTC"))


async def cases_since(session: AsyncSession, since: datetime) -> list[Case]:
    res = await session.execute(select(Case).where(Case.created_at >= since).order_by(Case.id))
    return list(res.scalars())


async def open_cases(session: AsyncSession) -> list[Case]:
    res = await session.execute(select(Case).where(Case.status.in_([s.value for s in OPEN_STATUSES])).order_by(Case.id))
    return list(res.scalars())


def counts(cases: list[Case]) -> dict[str, int]:
    st = [c.status for c in cases]
    return {
        "total": len(cases),
        "verified": st.count(S.VERIFIED.value),
        "waiting": sum(1 for x in st if x in WAITING_BETIX),
        "working": sum(1 for x in st if x in WORKING),
        "collecting": sum(1 for x in st if x in COLLECTING),
        "ambiguous": st.count(S.ORDER_MATCH_AMBIGUOUS.value),
        "escalated": st.count(S.ESCALATED.value),
        "already_success": st.count(S.ALREADY_SUCCESS.value),
        "already_sent": st.count(S.ALREADY_SENT.value),
        "failed": st.count(S.FAILED.value),
    }


def _case_line(c: Case, tz: str) -> str:
    who = f"@{c.original_username}" if c.original_username else (c.original_first_name or "-")
    icon = ICON.get(c.status, "\u25ab\ufe0f")
    return (
        f"{icon} {code(case_label(c))}\n"
        f"     {esc(c.status.replace('_', ' ').lower())} \u00b7 {esc(who)} \u00b7 "
        f"{esc(c.mobile or 'no mobile')} \u00b7 {esc(fmt_local(c.created_at, tz, '%d %b %H:%M'))}"
    )


async def build_summary(session: AsyncSession, days: int = 0, *, max_open: int = 10) -> str:
    tz = get_settings().timezone
    if days and days > 0:
        since = period_start(days, tz)
        recent = await cases_since(session, since)
        label = "Today" if days == 1 else f"Last {days} days"
        foot = i(f"Since {fmt_local(since, tz, '%d %b %Y %H:%M')} \u00b7 /summary for all time")
    else:
        recent = list((await session.execute(select(Case).order_by(Case.id))).scalars())
        label = "All time"
        foot = i("All cases ever \u00b7 /summary 7 for the last 7 days")
    n = counts(recent)
    amount = sum((c.amount or 0) for c in recent if c.status == S.VERIFIED.value)
    rows = [
        f"\U0001f5c3 Total cases: {b(n['total'])}",
        f"\u2705 Confirmed: {b(n['verified'])}" + (f" \u00b7 {money(amount)}" if amount else ""),
    ]
    for icon, name, key in (
        ("\U0001f440", "Waiting on Betix", "waiting"),
        ("\u2699\ufe0f", "Being processed", "working"),
        ("\u23f3", "Collecting evidence", "collecting"),
        ("\u26a0\ufe0f", "Several possible orders", "ambiguous"),
        ("\U0001f6a8", "Needing a manual check", "escalated"),
        ("\u267b\ufe0f", "Already successful", "already_success"),
        ("\U0001f501", "Duplicate submissions", "already_sent"),
        ("\u26d4", "Closed without a match", "failed"),
    ):
        if n[key]:
            rows.append(f"{icon} {name}: {b(n[key])}")
    opened = await open_cases(session)
    shown = opened[-max_open:]
    if opened:
        more = f"\n{i(f'… and {len(opened) - max_open} more \u00b7 /cases')}" if len(opened) > max_open else ""
        open_block = (
            f"\U0001f5c2 {b(f'Open cases ({len(opened)})')}\n" + "\n".join(_case_line(c, tz) for c in shown) + more
        )
    else:
        open_block = "\U0001f4ed No open cases right now. \U0001f389"
    return para(f"\U0001f4ca {b(label.upper())}", rows, open_block, foot)
