"""Does this bank statement belong to the account a withdrawal was paid to?

Deterministic first: the statement's own text is searched for the account number (banks print it in full, in
groups, or partly hidden: "XXXXXXXX1809", "2735XXXXXXX1809", "XXXX XXXX 1809", "A/c ending 1809" - then every
visible digit is compared: first digits, last 4 / last 6). Only when the text settles nothing (a scanned statement) is
the AI asked to read the printed account number, and that reading goes through the very same comparison.
Anything short of a match is NOT a match: the caller keeps the case for a person."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MATCH, MISMATCH, UNKNOWN = "match", "mismatch", "unknown"

MASK_CHARS = "Xx*\u2022#"
# A partly hidden number: digits and mask characters, possibly printed in groups ("XXXX XXXX 1809", "2735-XXXX-1809")
MASKED_TOKEN_RE = re.compile(rf"(?<![0-9A-Za-z])(?:[0-9{MASK_CHARS}][ \-]?){{5,27}}[0-9{MASK_CHARS}]")
# "A/c ending 1809", "account ending with 021809", "A/c ....1809"
ENDING_RE = re.compile(r"(?:ending|ends)\s*(?:with|in)?\s*[:\-]?\s*(\d{3,8})(?!\d)|\.{3,}\s?(\d{3,8})(?!\d)", re.I)
ACCOUNT_LABEL_RE = re.compile(r"(?:a/?c|acc(?:oun)?t)\b", re.I)
LABELLED_RE = re.compile(
    r"(?:a/?c|acc(?:oun)?t)\.?\s*(?:no\.?|number|num|#)?\s*[:\-]?\s*((?:\d[\s\-]?){8,20})(?!\d)", re.I
)
MIN_VISIBLE = 4  # digits that must be visible AND agree before a hidden number counts as the same account
# SBI hides all but the last THREE digits ("XXXXXXXX835"). Three agreeing digits alone are a 1-in-1000 coincidence,
# so they count only TOGETHER with a second proof from the payout: the holder's name or the branch IFSC.
MIN_VISIBLE_WITH_PROOF = 3


@dataclass
class AccountCheck:
    result: str  # match | mismatch | unknown
    how: str  # full | masked | labelled | ai | none
    seen: str | None = None  # the account number the statement shows (masked for display by the caller)
    note: str = ""  # what was looked at, for the manual-review message
    weak: int = 0  # visible digits that agree when they are too few to decide on their own

    @property
    def ok(self) -> bool:
        return self.result == MATCH


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _same_number(a: str, b: str) -> bool:
    """Banks and panels disagree about leading zeros: 00273501000021809 is 273501000021809."""
    return bool(a) and a.lstrip("0") == b.lstrip("0")


def masked_tokens(text: str) -> list[tuple[str, bool]]:
    """Every partly hidden number in `text` as (token, labelled): the token with its groups joined
    ("XXXXXXXX1809", "2735XXXXXXX1809"), and whether an account label stands right before it."""
    out = []
    for m in MASKED_TOKEN_RE.finditer(text or ""):
        tok = re.sub(r"[ \-]", "", m.group(0))
        hidden = sum(ch in MASK_CHARS for ch in tok)
        if hidden < 2 or len(tok) - hidden < 3:
            continue  # a plain number, or almost nothing visible
        before = text[max(0, m.start() - 30) : m.start()]
        out.append((tok, bool(ACCOUNT_LABEL_RE.search(before))))
    return out


def compare_masked(acct: str, token: str) -> tuple[str, int]:
    """One partly hidden number against the payout's account. (verdict, visible digits that were compared)

    Every VISIBLE digit is used: the leading digits, the trailing digits (last 4 / last 6 ...) and - when the
    token is as long as the account, so the positions are known - the ones in between.
    "agree" with >= MIN_VISIBLE digits is a match; "agree" with fewer is too little to decide; one wrong digit is
    "differ"."""
    is_mask = [ch in MASK_CHARS for ch in token]
    lead = len(token) - len(token.lstrip("0123456789"))
    tail = len(token) - len(token.rstrip("0123456789"))
    visible = sum(not m for m in is_mask)
    if len(token) == len(acct):  # positions are certain: compare digit by digit
        ok = all(m or ch == acct[i] for i, (ch, m) in enumerate(zip(token, is_mask)))
        return ("agree" if ok else "differ"), visible
    head, end = token[:lead], (token[len(token) - tail :] if tail else "")
    # The number of mask characters is not reliable (banks print "XXXX1809" for a 15-digit account) and leading
    # zeros come and go: what can be compared is the visible START and the visible END.

    def fits(number: str, start: str) -> bool:
        return len(start) + len(end) <= len(number) and number.startswith(start) and number.endswith(end)

    bare = acct.lstrip("0") if len(acct.lstrip("0")) >= 6 else acct
    ok = fits(acct, head) or fits(bare, head) or fits(bare, head.lstrip("0"))
    return ("agree" if ok else "differ"), len(head) + len(end)


def compare_account(account: str, text: str, *, bare_full_numbers: bool = True) -> AccountCheck:
    """Look for `account` (the payout's account number) in a statement's text.

    match    - printed in full (any grouping, leading zeros aside), or partly hidden with every visible digit
               agreeing and at least MIN_VISIBLE of them (last 4, last 6, first digits + last digits ...)
    mismatch - the statement names ANOTHER account: a different full number, or a hidden account number whose
               visible digits contradict the payout's
    unknown  - nothing decisive is visible: the AI reads the header next, then a person"""
    acct = _digits(account)
    if len(acct) < 6 or not text:
        return AccountCheck(UNKNOWN, "none")
    core = acct.lstrip("0")
    if len(core) < 6:  # an all-zero / near-zero number: nothing is left to compare without its zeros
        core = acct
    spaced = r"[\s\-]?".join(core)  # "5012 3456 1231" and "5012-3456-1231" are the same number
    # `bare_full_numbers=False`: the text may hold transaction rows, where the very same digits are the OTHER
    # party of a transfer. Then a full number counts only inside an "Account No: ..." field.
    where = text if bare_full_numbers else labelled_accounts(text)
    if re.search(rf"(?<![1-9])0*{spaced}(?!\d)", where):
        return AccountCheck(MATCH, "full", acct)

    tokens = masked_tokens(text)
    for m in ENDING_RE.finditer(text):  # "A/c ending 1809" is a hidden number too
        digits = m.group(1) or m.group(2)
        before = text[max(0, m.start() - 30) : m.start()]
        tokens.append(("XXXX" + digits, bool(ACCOUNT_LABEL_RE.search(before)) or bool(m.group(1))))
    weak, weak_digits, differing = None, 0, []
    for tok, labelled in tokens:
        verdict, seen_digits = compare_masked(acct, tok)
        if verdict == "agree" and seen_digits >= MIN_VISIBLE:
            return AccountCheck(MATCH, "masked", tok)
        if verdict == "agree":
            if weak is None or seen_digits > weak_digits:
                weak, weak_digits = tok, seen_digits
        elif labelled:
            differing.append(tok)  # only a number that IS the account can prove another account

    labelled_full = [_digits(m.group(1)) for m in LABELLED_RE.finditer(text)]
    labelled_full = [x for x in labelled_full if x and not _same_number(x, acct)]
    if labelled_full:
        return AccountCheck(MISMATCH, "labelled", labelled_full[0])
    if differing and not weak:
        return AccountCheck(MISMATCH, "masked", differing[0])
    return AccountCheck(UNKNOWN, "none", weak, weak=weak_digits if weak else 0)


def same_person(payout_name: str | None, text: str | None) -> bool:
    """Is the payout's beneficiary named in `text`? Every part of the name (3+ letters) must be there, in any
    order ("CHATTERJEE SOURAV", "Mr. Sourav Kumar Chatterjee"); a one-word name needs that word."""
    parts = [w for w in re.findall(r"[A-Za-z]{3,}", (payout_name or "").upper()) if w not in {"MRS", "SHRI", "SMT"}]
    words = set(re.findall(r"[A-Za-z]{3,}", (text or "").upper()))
    return bool(parts) and all(w in words for w in parts)


def second_proof(text: str, *, beneficiary: str | None, ifsc: str | None) -> str | None:
    """What else in the statement ties it to the payout: the branch IFSC, or the holder's name."""
    if ifsc and re.search(rf"\b{re.escape(ifsc.strip())}\b", text or "", re.I):
        return f"IFSC {ifsc.strip().upper()}"
    if same_person(beneficiary, text):
        return "the account holder's name"
    return None


TABLE_HEAD_RE = re.compile(
    r"\bdate\b.*\b(narration|particulars|description|details|remarks|withdrawals?|debit|balance)\b", re.I
)


def split_header(text: str, fallback_lines: int = 45) -> tuple[str, bool]:
    """(the part of the text ABOVE the transaction table, whether that table was actually found).
    The rows below name other people's accounts (transfers in and out) and must never decide whose statement it
    is. When no table heading is found the first lines are returned, and they may well BE rows."""
    lines = (text or "").splitlines()
    for n, line in enumerate(lines):
        if TABLE_HEAD_RE.search(line) and ":" not in line:
            return "\n".join(lines[:n]), True
    return "\n".join(lines[:fallback_lines]), False


def header_of(text: str, fallback_lines: int = 45) -> str:
    return split_header(text, fallback_lines)[0]


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


# "Account No : 12345", "Account Number\n12345", "A/c No. XXXX1809", "Acc No-123": a number that SAYS it is the account
ACCOUNT_FIELD_RE = re.compile(
    r"(?:a/?c|acc(?:oun)?t)\.?\s*(?:no\b\.?|number|num\b\.?|#)\s*[:\-.]?\s*([0-9Xx*\u2022# \-]{4,40})", re.I
)


def labelled_accounts(text: str) -> str:
    """Every "Account No: ..." field found ANYWHERE in the text, one per line. PDF text extraction does not keep
    the page order - SBI's "Recent Transactions" statement yields its header AFTER the rows - so the holder's
    account number cannot be looked for "at the top" only. A field that names itself the account number can be
    trusted wherever it lands; bare numbers in the rows cannot, and are not looked at here."""
    found = [f"Account No: {m.group(1).strip()}" for m in ACCOUNT_FIELD_RE.finditer(text or "")]
    return "\n".join(dict.fromkeys(found))


def compare_statement_text(account: str, text: str) -> AccountCheck:
    """The header first, then every labelled account field of the pages read. A match anywhere wins; "another
    account" is only said when nothing matched."""
    top, is_header = split_header(text)
    head = compare_account(account, top, bare_full_numbers=is_header)
    if head.result == MATCH:
        return head
    fields = labelled_accounts(text)
    anywhere = compare_account(account, fields) if fields else AccountCheck(UNKNOWN, "none")
    if anywhere.result == MATCH:
        return anywhere
    for check in (head, anywhere):
        if check.result == MISMATCH:
            return check
    best = head if head.weak >= anywhere.weak else anywhere
    if not text.strip():
        note = "the statement has no readable text (scanned?)"
    elif best.weak:
        note = "too few digits of the account number are visible"
    else:
        note = "the statement's text shows no account number"
    return AccountCheck(UNKNOWN, "none", best.seen or head.seen or anywhere.seen, note, best.weak)


def with_proof(check: AccountCheck, proof_text: str, *, beneficiary: str | None, ifsc: str | None) -> AccountCheck:
    """Too few visible digits agree (3): a match after all when the statement ALSO carries the payout's IFSC or
    the beneficiary's name."""
    if check.result != UNKNOWN or check.weak < MIN_VISIBLE_WITH_PROOF:
        return check
    proof = second_proof(proof_text, beneficiary=beneficiary, ifsc=ifsc)
    if proof:
        return AccountCheck(MATCH, "masked+proof", check.seen, f"last {check.weak} digits agree, and so does {proof}")
    who = f" ({beneficiary})" if beneficiary else ""
    note = (
        f"only the last {check.weak} digits are visible and they agree, but neither the holder's name{who} nor the "
        "IFSC of the payout is on the statement"
    )
    return AccountCheck(UNKNOWN, check.how, check.seen, note, check.weak)


async def check_statement(
    path: Path, account: str, *, beneficiary: str | None = None, ifsc: str | None = None
) -> AccountCheck:
    """`path` is a readable (already decrypted) statement PDF. `beneficiary` / `ifsc`: the payout's, used as the
    second proof when the statement hides all but three digits of the account."""
    text = pdf_text(path, max_pages=3)
    check = with_proof(compare_statement_text(account, text), text, beneficiary=beneficiary, ifsc=ifsc)
    if check.result != UNKNOWN:
        return check
    from app.ai.analyzer import get_analyzer

    read = await get_analyzer().read_statement_account(path)  # may raise AIUnavailable: the caller holds the case
    read = read or {}
    if "account_number" not in read and "value" in read:  # the older, account-only shape
        read = {"account_number": read}
    field = read.get("account_number") or {}
    value, conf = field.get("value"), float(field.get("confidence") or 0)
    if not value:
        return AccountCheck(
            UNKNOWN, "ai", check.seen, f"{check.note}; the AI found no account number either", check.weak
        )
    again = compare_account(account, f"Account No: {value}")
    if again.result == UNKNOWN and again.weak:
        seen_by_ai = " ".join(str((read.get(k) or {}).get("value") or "") for k in ("holder_name", "ifsc"))
        again = with_proof(again, f"{text}\n{seen_by_ai}", beneficiary=beneficiary, ifsc=ifsc)
    if again.result == MATCH or conf >= 0.6:
        note = again.note or (
            f"the AI read {mask_account(_digits(str(value)) or str(value))}: too few digits to decide"
            if again.result == UNKNOWN
            else ""
        )
        return AccountCheck(again.result, "ai", again.seen or str(value), note, again.weak)
    return AccountCheck(UNKNOWN, "ai", str(value), f"the AI was not sure of the account number it read ({conf:.0%})")


def mask_account(value: str | None) -> str:
    v = value or ""
    return ("•" * max(0, len(v) - 4) + v[-4:]) if v else "-"
