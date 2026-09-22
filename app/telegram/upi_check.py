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
