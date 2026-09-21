"""Up to which date does a bank statement go?

A withdrawal is verified with a statement that COVERS the withdrawal date: a statement that ends before it cannot
show whether the money arrived. The last date is read from the statement's own text - every date on every page
(statement period, transaction rows, "generated on"), the latest one that is not in the future. A scanned
statement has no text: then the AI reads the last date it covers."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
NUMERIC_RE = re.compile(r"(?<![\d/.\-])(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})(?![\d/.\-])")
ISO_RE = re.compile(r"(?<![\d/.\-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d/.\-])")
DMY_RE = re.compile(rf"(?<!\d)(\d{{1,2}})[\s\-/.,]*({MON})[a-z]*[\s\-/.,']*(\d{{4}}|\d{{2}})(?!\d)", re.I)
MDY_RE = re.compile(rf"\b({MON})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)", re.I)


def _make(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def dates_in(text: str) -> list[date]:
    """Every calendar date printed in `text` (Indian day-first order for 01/09/2026)."""
    months = MON.split("|")
    out: list[date | None] = []
    for m in ISO_RE.finditer(text or ""):
        out.append(_make(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in NUMERIC_RE.finditer(text or ""):
        out.append(_make(int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in DMY_RE.finditer(text or ""):
        out.append(_make(int(m.group(3)), months.index(m.group(2).lower()[:3]) + 1, int(m.group(1))))
    for m in MDY_RE.finditer(text or ""):
        out.append(_make(int(m.group(3)), months.index(m.group(1).lower()[:3]) + 1, int(m.group(2))))
    return [d for d in out if d is not None]


def last_date_in(text: str, today: date | None = None) -> date | None:
    """The latest date the text reaches. Dates in the future (a card's expiry, "valid till") and ancient ones
    (a date of birth, the account opening date) are not what a statement covers."""
    today = today or datetime.now().date()
    ok = [d for d in dates_in(text) if date(2015, 1, 1) <= d <= today + timedelta(days=1)]
    return max(ok) if ok else None


def pdf_all_text(path: Path, max_pages: int = 60) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return ""
        return "\n".join((pg.extract_text() or "") for pg in reader.pages[:max_pages])
    except Exception:  # noqa: BLE001
        return ""


async def statement_last_date(path: Path, ai_read: dict | None = None) -> tuple[date | None, str]:
    """(the last date the statement covers, how it was found: text | ai | none).
    `ai_read`: an AI header read already made for this file (it carries "last_date"); none -> asked when needed."""
    found = last_date_in(pdf_all_text(path))
    if found:
        return found, "text"
    if ai_read is None:
        from app.ai.analyzer import get_analyzer

        ai_read = await get_analyzer().read_statement_account(path)  # may raise AIUnavailable
    field = (ai_read or {}).get("last_date") or {}
    value = str(field.get("value") or "")
    if value and float(field.get("confidence") or 0) >= 0.6:
        got = last_date_in(value) or last_date_in(str(field.get("evidence_text") or ""))
        if got:
            return got, "ai"
    return None, "none"
