"""Does this bank statement belong to the account a withdrawal was paid to?

Deterministic first: the statement's own text is searched for the account number (banks print it in full, in
groups, or masked: "XXXXXX1231", "5012XXXX1231"). Only when the text settles nothing (a scanned statement) is
the AI asked to read the printed account number, and that reading goes through the very same comparison.
Anything short of a match is NOT a match: the caller keeps the case for a person."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MATCH, MISMATCH, UNKNOWN = "match", "mismatch", "unknown"

MASK = r"[Xx*•#]"
MASKED_RE = re.compile(rf"(?<![0-9A-Za-z])(\d{{0,8}}){MASK}{{2,}}(\d{{4,6}})(?!\d)")
LABELLED_RE = re.compile(
    r"(?:a/?c|acc(?:oun)?t)\.?\s*(?:no\.?|number|num|#)?\s*[:\-]?\s*((?:\d[\s\-]?){8,20})(?!\d)", re.I
)


@dataclass
class AccountCheck:
    result: str  # match | mismatch | unknown
    how: str  # full | masked | labelled | ai | none
    seen: str | None = None  # the account number the statement shows (masked for display by the caller)

    @property
    def ok(self) -> bool:
        return self.result == MATCH


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def compare_account(account: str, text: str) -> AccountCheck:
    """Look for `account` (the payout's account number) in a statement's text."""
    acct = _digits(account)
    if len(acct) < 6 or not text:
        return AccountCheck(UNKNOWN, "none")
    spaced = r"[\s\-]?".join(acct)  # "5012 3456 1231" and "5012-3456-1231" are the same number
    if re.search(rf"(?<!\d){spaced}(?!\d)", text):
        return AccountCheck(MATCH, "full", acct)
    masked = [(m.group(1), m.group(2)) for m in MASKED_RE.finditer(text)]
    for head, tail in masked:
        if acct.endswith(tail) and acct.startswith(head):
            return AccountCheck(MATCH, "masked", f"{head}XXXX{tail}")
    labelled = [_digits(m.group(1)) for m in LABELLED_RE.finditer(text)]
    labelled = [x for x in labelled if x and x != acct]
    if labelled:
        return AccountCheck(MISMATCH, "labelled", labelled[0])
    if masked:
        head, tail = masked[0]
        return AccountCheck(MISMATCH, "masked", f"{head}XXXX{tail}")
    return AccountCheck(UNKNOWN, "none")


TABLE_HEAD_RE = re.compile(
    r"\bdate\b.*\b(narration|particulars|description|details|remarks|withdrawals?|debit|balance)\b", re.I
)


def header_of(text: str, fallback_lines: int = 45) -> str:
    """The part of page one ABOVE the transaction table: whose statement this is. The rows below name other
    people's accounts (transfers in and out) and must never decide whose statement it is."""
    lines = (text or "").splitlines()
    for n, line in enumerate(lines):
        if n >= 3 and TABLE_HEAD_RE.search(line):
            return "\n".join(lines[:n])
    return "\n".join(lines[:fallback_lines])


def pdf_text(path: Path, max_pages: int = 1) -> str:
    """Page one's text (the account header lives there). "" for a scanned / unreadable PDF."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return ""
        return "\n".join((pg.extract_text() or "") for pg in reader.pages[:max_pages])
    except Exception:  # noqa: BLE001
        return ""


async def check_statement(path: Path, account: str) -> AccountCheck:
    """`path` is a readable (already decrypted) statement PDF."""
    check = compare_account(account, header_of(pdf_text(path)))
    if check.result != UNKNOWN:
        return check
    from app.ai.analyzer import get_analyzer

    read = await get_analyzer().read_statement_account(path)  # may raise AIUnavailable: the caller holds the case
    value = (read or {}).get("value")
    if not value or float((read or {}).get("confidence") or 0) < 0.6:
        return AccountCheck(UNKNOWN, "ai")
    again = compare_account(account, f"Account No: {value}")
    return AccountCheck(again.result, "ai", again.seen or str(value))


def mask_account(value: str | None) -> str:
    v = value or ""
    return ("•" * max(0, len(v) - 4) + v[-4:]) if v else "-"
