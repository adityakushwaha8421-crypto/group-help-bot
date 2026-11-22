"""Payee-UPI helpers for the /pi order check.

The payee ("Paid to") UPI is OCR'd from the payment screenshot (often masked or shortened: "4913@pthdfc",
"XXXXXX1014-4@ibl", "******5011@ptyes") and compared with the "Order's UPI" the Betix bot prints in its answer to
`/pi <ORDER-ID>`: same handle + same visible ending. `/upi` is never used."""

from __future__ import annotations

import re

ORDER_UPI = re.compile(r"Order'?s\s+UPI\s*:[ \t]*([^\s<>\[\]]+@[A-Za-z0-9.\-_]+)", re.I)
# Leading mask on a screenshot UPI: "XXXXXX1014-4", "******4913", "••4913".
MASK = re.compile(r"^(?:[xX]{2,}|[*•·]+)")
# A mask anywhere: "boim-0741XXXX7519", "mo**42@ptyes" - the visible pieces around it are what can be compared
ANY_MASK = re.compile(r"(?:[xX]{2,}|[*•·]+)")


MERCHANT_ORDER = re.compile(r"MerchantOrderNo\s*:\s*([A-Za-z]+-\d+)", re.I)


def parse_pi_answer(text: str | None) -> tuple[str | None, str | None]:
    """A Betix bot answer to `/pi <ORDER-ID>`: (MerchantOrderNo, Order's UPI), each verbatim or None."""
    t = text or ""
    om, um = MERCHANT_ORDER.search(t), ORDER_UPI.search(t)
    return (om.group(1).upper() if om else None), (um.group(1).strip() if um else None)


PI_UTR = re.compile(r"UTR\s*:\s*([A-Za-z0-9]{6,})", re.I)


def parse_pi_utr(text: str | None) -> str | None:
    """The UTR the Betix bot shows on the order in its `/pi` answer ("🧾UTR: 611532946151"); None for "UTR: NA"."""
    m = PI_UTR.search(text or "")
    return m.group(1) if m else None


UPI_SHAPE = re.compile(r"^[A-Za-z0-9.\-_*•·]+@[A-Za-z][A-Za-z0-9.]*$")


def _squash(v: str) -> str:
    return re.sub(r"[\s•·]+", "", v or "").lower()


def ocr_upi(reading: dict | None) -> tuple[str | None, str]:
    """The payee UPI from one OCR reading of the screenshot, or (None, why). Accepted only when it looks like a UPI
    and the text the reader quotes from the image really contains it (nothing completed or invented)."""
    if not isinstance(reading, dict) or not reading.get("value"):
        return None, "no payee UPI printed on the screenshot"
    value = str(reading["value"]).strip().lstrip("•·").strip()
    if not UPI_SHAPE.match(value.replace(" ", "")):
        return None, f"OCR value is not a UPI ({value[:40]})"
    quoted = reading.get("evidence_text")
    if not quoted or _squash(value.lstrip("•·*")) not in _squash(str(quoted)):
        return None, "OCR value not found in the quoted screenshot text"
    return value.replace(" ", ""), "payment screenshot OCR"


def _split(upi: str | None) -> tuple[str, str] | None:
    v = (upi or "").strip().lower().replace(" ", "")
    if "@" not in v:
        return None
    local, handle = v.rsplit("@", 1)
    return (local, handle) if handle else None


def upi_ending_match(screenshot_upi: str | None, order_upi: str | None, *, min_chars: int = 3) -> tuple[bool, str]:
    """Does the (possibly masked / shortened) screenshot UPI fit the order's UPI?
    Same handle AND the visible characters are the end of the order's UPI (at least `min_chars` of them)."""
    a, b = _split(screenshot_upi), _split(order_upi)
    if b is None:
        return False, "the Betix response has no Order's UPI"
    if a is None:
        return False, "no receiver UPI on the screenshot"
    (la, ha), (lb, hb) = a, b
    if ha != hb:
        return False, f"handle differs: @{ha} on the screenshot, @{hb} on the order"
    if la == lb:
        return True, "same UPI"
    parts = [p for p in ANY_MASK.split(la) if p]  # the visible pieces around the hidden characters
    head = parts[0] if parts and la.startswith(parts[0]) else ""
    tail = parts[-1] if parts and la.endswith(parts[-1]) else ""
    if len(parts) == 1 and not ANY_MASK.search(la):
        head, tail = "", la  # a plain shortened form ("4913") is an ending
    visible = len(head) + len(tail)
    if visible < min_chars:
        return False, f"only {visible} visible character(s) on the screenshot ({la or '-'}@{ha})"
    if lb.startswith(head) and lb.endswith(tail) and len(head) + len(tail) <= len(lb):
        shown = f"{head}…{tail}" if head else tail
        return True, f"the order's UPI fits the visible part {shown}@{ha}"
    return False, f"visible part differs: {la}@{ha} on the screenshot, {lb}@{hb} on the order"


COMPANY_WORDS = {"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "INC", "CO", "COMPANY", "OPC", "CORP", "CORPORATION"}


def _name_words(value: str | None) -> list[str]:
    """Upper-case words of a name, punctuation and the "..." of a cut-off line dropped, company suffixes removed."""
    words = re.sub(r"[^A-Z0-9 ]+", " ", (value or "").upper()).split()
    return [w for w in words if w not in COMPANY_WORDS]


def name_match(screenshot_name: str | None, order_name: str | None, *, min_chars: int = 5) -> tuple[bool, str]:
    """Is the payee name printed on the screenshot the order's payee name? Screens cut names short
    ("CARTZUNO INNOVATIONS PRIVA...") and write the company form differently (Pvt Ltd / Private Limited), so: the
    same words, or one name the beginning of the other (at least `min_chars` letters), company suffixes aside."""
    a, b = _name_words(screenshot_name), _name_words(order_name)
    if not a or not b:
        return False, "no name to compare"
    ja, jb = "".join(a), "".join(b)
    short, long = (ja, jb) if len(ja) <= len(jb) else (jb, ja)
    if ja == jb:
        return True, "same name"
    if len(short) >= min_chars and long.startswith(short):
        return True, "one name is the beginning of the other"
    return False, f"different names ({' '.join(a)} / {' '.join(b)})"


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def upi_reading_slip(
    screenshot_upi: str | None, order_upi: str | None, *, max_edits: int = 3, min_length: int = 10
) -> tuple[bool, str]:
    """The SAME UPI, read off the screenshot with a slip of up to `max_edits` characters (a letter misread, dropped
    or added - anywhere, name or bank handle). The screenshot is read by OCR and real payments went to Manual
    Review over one letter - live 2026-11-13: SK26101181@nsdlpbbpay read for SK26101181@nsdlpbpay,
    ci26102159-4@yblz for ci26102159-4@ypbiz. The screenshot's UPI must be printed in full (a masked one is the
    ending check's business) and the UPI long enough that three characters are a slip, not another UPI."""
    a, b = _split(screenshot_upi), _split(order_upi)
    if a is None or b is None:
        return False, "no UPI to compare"
    if ANY_MASK.search(a[0]):
        return False, "the screenshot's UPI is masked"
    full_a, full_b = f"{a[0]}@{a[1]}", f"{b[0]}@{b[1]}"
    if len(full_b) < min_length:
        return False, "the UPI is too short to allow for a reading slip"
    edits = _edit_distance(full_a, full_b)
    if 0 < edits <= max_edits:
        return True, f"{edits} character(s) differ between {full_a} on the screenshot and {full_b} on the order"
    return False, f"different UPI ({full_a} on the screenshot, {full_b} on the order)"


def upi_shortened_slip(
    screenshot_upi: str | None, order_upi: str | None, *, max_edits: int = 3, min_chars: int = 3
) -> tuple[bool, str]:
    """The order's UPI, printed SHORTENED or masked on the screenshot AND read with a slip. Live 2026-11-22: Google
    Pay prints "••••82-3@ypbiz" for zc26112382-3@ypbiz; it was read as "82-3@yphbiz" - the dots gone and a letter
    too many in the handle. The ending check wants the handle letter for letter; the whole-UPI tolerance cannot
    see a shortened name. Here: the visible piece(s) must be the end (and start) of the order's UPI name, and the
    slips in them and in the handle together stay within `max_edits` - with at most one slip per four visible
    characters of the name, so a short ending still has to be that ending."""
    a, b = _split(screenshot_upi), _split(order_upi)
    if a is None or b is None:
        return False, "no UPI to compare"
    (la, ha), (lb, hb) = a, b
    parts = [x for x in ANY_MASK.split(la) if x]
    if not ANY_MASK.search(la):
        head, tail = "", la  # a plain shortened form ("82-3") is an ending
    else:
        head = parts[0] if parts and la.startswith(parts[0]) else ""
        tail = parts[-1] if parts and la.endswith(parts[-1]) and not (len(parts) == 1 and head) else ""
    visible = len(head) + len(tail)
    if visible < min_chars or visible > len(lb):
        return False, f"only {visible} usable character(s) of the UPI name on the screenshot"
    in_name = (_edit_distance(head, lb[: len(head)]) if head else 0) + (
        _edit_distance(tail, lb[len(lb) - len(tail) :]) if tail else 0
    )
    in_handle = _edit_distance(ha, hb)
    edits = in_name + in_handle
    if edits == 0:
        return False, "fits exactly (the ending check's business)"
    if in_name > visible // 4 or edits > max_edits:
        return False, f"different UPI ({la}@{ha} on the screenshot, {lb}@{hb} on the order)"
    shown = f"{head}…{tail}" if head and tail else (tail or head)
    return True, f"{shown}@{ha} on the screenshot is {lb}@{hb} read with {edits} character(s) off"


def upi_near_match(
    screenshot_upi: str | None, order_upi: str | None, *, max_edits: int = 3, min_chars: int = 3
) -> tuple[bool, str]:
    """The order's UPI, misread: the whole UPI a few characters off, or a shortened / masked one with a slip."""
    ok, why = upi_reading_slip(screenshot_upi, order_upi, max_edits=max_edits)
    if ok:
        return ok, why
    return upi_shortened_slip(screenshot_upi, order_upi, max_edits=max_edits, min_chars=min_chars)
