"""Deterministic extraction (regex first). AI is the secondary layer, see analyzer.py."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.utils.timeutil import parse_datetime_loose, utcnow

UTR_RE = re.compile(
    r"\b(?:UTR|RRN|Ref(?:erence)?(?:\s*No\.?)?|Txn(?:\s*ID)?|Transaction\s*ID)\s*[:#\-]?\s*([A-Z0-9]{10,22})\b", re.I
)
UTR_BARE_RE = re.compile(r"(?<![\d@.])\b(\d{12})\b(?![\d@.])")
UPI_RE = re.compile(r"\b([a-z0-9][a-z0-9._\-]{1,60}@[a-z][a-z0-9]{1,20})\b", re.I)
AMOUNT_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")
AMOUNT_WORD_RE = re.compile(
    r"\b(?:amount|paid|amt)\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)",
    re.I,
)
# ---- statement password -------------------------------------------------------------------------------------
# "Password is 10753260494", "PDF ka password 10753260494 hai", "Pw:-abc123", "The password for the PDF = x".
# The keyword names the FIELD; the value is the first token after it that is not connective filler.
# Betix withdrawal id as the operator types it ("WD-84425-67115"); the group receives it as "BXWD-84425-67115"
WITHDRAWAL_RE = re.compile(r"\b(?:BX)?(WD-\d{3,10}-\d{3,10})\b", re.I)


def extract_withdrawal_id(text: str | None) -> str | None:
    m = WITHDRAWAL_RE.search(text or "")
    return m.group(1).upper() if m else None


PASSWORD_KEY_RE = re.compile(r"\b(?:pass\s*word|password|passcode|passkey|pwd|pass|p\.?w\.?|pin)\b", re.I)
# Words that sit between the keyword and the value in English, Hindi and Hinglish. Never a password themselves.
PASSWORD_FILLER = {
    "is",
    "are",
    "was",
    "will",
    "be",
    "the",
    "this",
    "that",
    "it",
    "its",
    "hai",
    "he",
    "h",
    "hain",
    "ha",
    "hoga",
    "ho",
    "raha",
    "rha",
    "ka",
    "ke",
    "ki",
    "ko",
    "ek",
    "mera",
    "meri",
    "apka",
    "aapka",
    "yeh",
    "ye",
    "wo",
    "woh",
    "for",
    "of",
    "to",
    "on",
    "in",
    "my",
    "your",
    "our",
    "and",
    "or",
    "pdf",
    "file",
    "doc",
    "document",
    "statement",
    "bank",
    "attached",
    "attachment",
    "open",
    "opening",
    "use",
    "using",
    "try",
    "send",
    "sent",
    "sending",
    "given",
    "same",
    "below",
    "above",
    "here",
}
PASSWORD_STRIP = " \t:=-–—>~.,;!?'\"()[]{}*_`"


def read_password(text: str | None) -> str | None:
    """The statement password out of any natural wording. Returns the VALUE, never the word "password"."""
    if not text:
        return None
    for key in PASSWORD_KEY_RE.finditer(text):
        rest = text[key.end() :]
        for raw in re.split(r"[\s\n]+", rest):
            token = raw.strip(PASSWORD_STRIP)
            if not token:
                continue  # a bare ":", "-", ":-", "=" separator
            low = token.lower()
            if low in PASSWORD_FILLER or PASSWORD_KEY_RE.fullmatch(token):
                continue  # connective wording, or the keyword repeated
            if 3 <= len(token) <= 40 and re.search(r"[A-Za-z0-9]", token):
                return token
            break  # something that cannot be a password: this keyword has no value after it
    return None


# Indian mobile in any common format: 7733931348, +91 7733931348, +917733931348, 077339 31348, 773-393-1348
MOBILE_RE = re.compile(r"(?<![\d@])(?:\+?\s*91[\s\-.]*)?0?([6-9](?:[\s\-.]?\d){9})(?![\d@])")
DATETIME_RE = re.compile(
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?:,?\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)?"
    r"|\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?(?:\s*[+-]\d{2}:?\d{2})?)?"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4}(?:,?\s+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)?"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}(?:,?\s+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)?)"
)


@dataclass
class Field:
    value: Any = None
    confidence: float = 0.0
    source: str = "none"

    def as_dict(self) -> dict:
        v = self.value
        if isinstance(v, datetime):
            v = v.isoformat()
        return {"value": v, "confidence": round(float(self.confidence), 3), "source": self.source}


def normalize_mobile(value: Any) -> str | None:
    """Canonical 10-digit Indian mobile, or None when the value is not one."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 and digits[0] in "6789" else None


def extract_mobile(text: str | None) -> str | None:
    """The customer's mobile typed by the operator ("7733931348", "+91 77339 31348", "mobile: 7733931348").

    A number the operator introduced as a PDF password ("password is 9175404354") is never the mobile."""
    if not text:
        return None
    claimed = normalize_mobile(read_password(text) or "")
    wd_spans = [m.span() for m in WITHDRAWAL_RE.finditer(text)]  # "WD-84425-67115" is not a mobile number
    for m in MOBILE_RE.finditer(text):
        if any(a <= m.start() < b for a, b in wd_spans):
            continue
        mob = normalize_mobile(m.group(1))
        if mob and mob != claimed:
            return mob
    return None


@dataclass
class Extraction:
    mobile: Field = field(default_factory=Field)
    registration_number: Field = field(default_factory=Field)
    amount: Field = field(default_factory=Field)
    payment_time: Field = field(default_factory=Field)
    utr: Field = field(default_factory=Field)
    upi_id: Field = field(default_factory=Field)
    payer_name: Field = field(default_factory=Field)
    receiver_name: Field = field(default_factory=Field)
    bank_name: Field = field(default_factory=Field)
    transaction_reference: Field = field(default_factory=Field)
    payment_status: Field = field(default_factory=Field)
    betex_order_id: Field = field(default_factory=Field)
    statement_password: Field = field(default_factory=Field)
    withdrawal_id: Field = field(default_factory=Field)

    FIELDS = (
        "mobile",
        "registration_number",
        "amount",
        "payment_time",
        "utr",
        "upi_id",
        "payer_name",
        "receiver_name",
        "bank_name",
        "transaction_reference",
        "payment_status",
        "betex_order_id",
        "statement_password",
        "withdrawal_id",
    )

    def as_dict(self) -> dict:
        return {k: getattr(self, k).as_dict() for k in self.FIELDS}

    def merge(self, other: "Extraction") -> "Extraction":
        """Keep the higher-confidence value per field; deterministic text sources win ties."""
        out = Extraction()
        for k in self.FIELDS:
            a, b = getattr(self, k), getattr(other, k)
            if b.value is not None and (a.value is None or b.confidence > a.confidence):
                setattr(out, k, b)
            else:
                setattr(out, k, a)
        return out

    @classmethod
    def from_dict(cls, d: dict | None) -> "Extraction":
        e = cls()
        if not d:
            return e
        for k in cls.FIELDS:
            f = d.get(k) or {}
            v = f.get("value")
            if k == "payment_time" and isinstance(v, str):
                v = parse_datetime_loose(v)
            setattr(e, k, Field(v, float(f.get("confidence") or 0), f.get("source") or "none"))
        return e


def normalize_amount(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip()
    s = re.sub(r"[₹,\s]|Rs\.?|INR", "", s, flags=re.I)
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def normalize_registration(v: Any) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", "", str(v)).upper()
    return s or None


def normalize_utr(v: Any) -> str | None:
    if v is None:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", str(v)).upper()
    return s or None


def normalize_upi(v: Any) -> str | None:
    if v is None:
        return None
    return str(v).strip().lower() or None


def extract_registration(text: str, pattern: re.Pattern[str]) -> str | None:
    """A plain-text message that *is* a registration/reference number (deterministic)."""
    if not text:
        return None
    s = text.strip()
    m = pattern.match(s)
    if not m:
        return None
    value = m.group(1) if m.groups() else m.group(0)
    # reject things that are clearly other tokens: pure words, urls, commands
    if value.startswith("/") or "http" in value.lower():
        return None
    if value.isalpha() and len(value) < 8:
        return None
    return normalize_registration(value)


def extract_from_text(
    text: str,
    *,
    registration_pattern: re.Pattern[str] | None = None,
    betex_pattern: re.Pattern[str] | None = None,
    source: str = "telegram_text",
) -> Extraction:
    e = Extraction()
    if not text:
        return e
    if betex_pattern:
        m = betex_pattern.search(text)
        if m:
            e.betex_order_id = Field(m.group(0).upper(), 0.99, source)
    pw = read_password(text)
    if pw:
        e.statement_password = Field(pw, 0.95, source)
    wd = extract_withdrawal_id(text)
    if wd:
        e.withdrawal_id = Field(wd, 0.99, source)
    m = UTR_RE.search(text)
    if m:
        e.utr = Field(normalize_utr(m.group(1)), 0.95, source)
    else:
        m = UTR_BARE_RE.search(text)
        if m and len(text.strip()) <= 40:  # a bare 12-digit number in a short message
            e.utr = Field(m.group(1), 0.7, source)
    if pw and e.utr.value == pw:  # "password 611532946151 hai" is a password, not a UTR
        e.utr = Field()
    m = UPI_RE.search(text)
    if m:
        e.upi_id = Field(normalize_upi(m.group(1)), 0.85, source)
    m = AMOUNT_WORD_RE.search(text) or AMOUNT_RE.search(text)
    if m:
        amt = normalize_amount(m.group(1))
        if amt is not None:
            e.amount = Field(amt, 0.9 if "₹" in text or re.search(r"rs|inr|amount", text, re.I) else 0.6, source)
    m = DATETIME_RE.search(text)
    if m:
        dt = parse_datetime_loose(m.group(1))
        if dt:
            e.payment_time = Field(dt, 0.85, source)
    mobile = extract_mobile(text)
    if mobile:
        e.mobile = Field(mobile, 0.98, source)
        # a bare 10-digit number is the mobile, never a UTR
        if e.utr.value and re.sub(r"\D", "", str(e.utr.value)).endswith(mobile):
            e.utr = Field()
    if registration_pattern:
        reg = extract_registration(text, registration_pattern)
        if (
            reg
            and not (e.betex_order_id.value and reg == e.betex_order_id.value)
            and not (e.utr.value == reg)
            and not (e.withdrawal_id.value and e.withdrawal_id.value in reg.upper())
            and normalize_mobile(reg) is None
        ):
            e.registration_number = Field(reg, 0.97, source)
    return e


MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_PART = (
    rf"(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?[\s\-/.,]*(?P<m1>{MONTHS})[a-z]*\.?"
    rf"|(?P<m2>{MONTHS})[a-z]*\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)"
    r"(?:\s*,?\s*'(?P<y2>\d{2}))?(?!\s*,?\s*\d{4})"
)
_TIME_PART = r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ap>am|pm)?"
# date then time ("13 Sept, 11:09 PM", "22nd sep'26, 09:02am") - or time then date ("09:02am, 22nd sep'26",
# "11:09 PM on 13 Sept"): payment apps print both orders
NOYEAR_RE = re.compile(rf"\b{_DATE_PART}\s*,?\s*(?:at\s+|on\s+)?{_TIME_PART}", re.I)
NOYEAR_TIME_FIRST_RE = re.compile(rf"(?<![\d:]){_TIME_PART}\s*,?\s*(?:on\s+|at\s+|\|\s*|-\s*)?{_DATE_PART}", re.I)


def parse_datetime_noyear(text: str | None, now=None):
    """A payment-app timestamp printed without a year ("13 Sept, 11:09 PM", "Sep 13 at 23:09"), as an aware UTC
    datetime. The payment is recent, so the year is the current one - or last year when that would put the date
    in the future. None when the text carries a year (parse_datetime_loose handles it) or no time at all."""
    if not text:
        return None
    m = NOYEAR_RE.search(str(text)) or NOYEAR_TIME_FIRST_RE.search(str(text))
    if not m:
        return None
    day = int(m.group("d1") or m.group("d2"))
    mon = (m.group("m1") or m.group("m2")).lower()[:3]
    month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(mon) + 1
    hour, minute, sec = int(m.group("h")), int(m.group("mi")), int(m.group("s") or 0)
    ap = (m.group("ap") or "").lower()
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    from zoneinfo import ZoneInfo

    local_now = (now or utcnow()).astimezone(ZoneInfo("Asia/Kolkata"))
    year = 2000 + int(m.group("y2")) if m.group("y2") else local_now.year  # "sep'26": the year IS printed
    try:
        when = datetime(year, month, day, hour, minute, sec)
    except ValueError:
        return None
    if not m.group("y2") and when > local_now.replace(tzinfo=None) + timedelta(days=1):
        when = when.replace(year=year - 1)
    return parse_datetime_loose(when.strftime("%Y-%m-%d %H:%M:%S"))


def fix_guessed_year(when, printed: str | None, now=None):
    """`when` came from the model; `printed` is the text on the screenshot. When that text shows NO year, the year
    in `when` is a guess: move it to the latest year that does not put the payment in the future."""
    if when is None or re.search(r"\b(19|20)\d{2}\b|'\d{2}\b", printed or ""):
        return when  # the year is printed ("2026", "'26"): keep it
    now = now or utcnow()
    fixed = when
    try:
        fixed = when.replace(year=now.year)
        if fixed > now + timedelta(days=1):
            fixed = when.replace(year=now.year - 1)
    except ValueError:  # 29 Feb
        return when
    return fixed


def extraction_from_ai(payload: dict, source: str) -> Extraction:
    """Convert the model's structured JSON (each field {value, confidence}) into an Extraction."""
    e = Extraction()
    for k in Extraction.FIELDS:
        f = payload.get(k)
        if not isinstance(f, dict):
            continue
        v = f.get("value")
        if v in (None, "", "null", "N/A", "NA"):
            if k == "payment_time":
                # The model saw the time but would not commit to a year ("13 Sept, 11:09 PM" -> null). The text it
                # quotes is enough: parse it ourselves rather than lose the screenshot's own timestamp.
                dt = parse_datetime_noyear(f.get("evidence_text"))
                if dt:
                    e.payment_time = Field(dt, max(float(f.get("confidence") or 0), 0.8), source)
            continue
        conf = float(f.get("confidence") or 0)
        if k == "amount":
            v = normalize_amount(v)
        elif k == "payment_time":
            # The screenshot printed NO year ("20 Sep, 11:34 AM"): the year in the model's value is its own guess,
            # and a model that was never told today's date guesses an old one - then Illunise is searched on the
            # wrong day and no order is ever found. The year is ours to work out, from the printed text.
            printed = parse_datetime_noyear(f.get("evidence_text"))
            v = printed or fix_guessed_year(
                parse_datetime_loose(str(v)) or parse_datetime_noyear(str(v)), f.get("evidence_text")
            )
        elif k == "registration_number":
            v = normalize_registration(v)
        elif k == "mobile":
            v = normalize_mobile(v)
        elif k == "utr":
            v = normalize_utr(v)
        elif k == "upi_id":
            v = normalize_upi(v)
            if v and re.match(r"^(?:[x*•·]{2,}|[*•·])", v):
                v = None  # masked ("XXXXXX4913@pthdfc"): not a full UPI; the payee UPI check reads receiver_upi
        if v is None:
            continue
        setattr(e, k, Field(v, conf, source))
    return e
