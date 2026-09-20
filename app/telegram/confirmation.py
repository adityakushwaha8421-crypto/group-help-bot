"""Classify Betix group replies. Regex-first, modelled on the real betixpay_cs_bot templates and the
reviewers' phrasing seen in the Betix support group export. AI classification (analyzer) is only a
secondary signal for unknown human text and never upgrades authority."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- system bot templates (verbatim shapes observed) ----
SYS_SUCCESS = [
    re.compile(r"✅\s*STATUS\s*:\s*Success(?:ful)?", re.I),
    re.compile(r"Payment\s+confirmed", re.I),
    re.compile(r"OrderStatus\s*:\s*Paid\s*\|\s*.*CallbackStatus\s*:\s*Success", re.I | re.S),
    re.compile(r"Resubmit\s+utr\s+result\s*:\s*Success", re.I),
    re.compile(r"^\s*Already\s+matched", re.I | re.M),
    re.compile(r"Order\s+already\s+confirmed", re.I),
    re.compile(r"Status\s*│\s*✅\s*Confirmed", re.I),
    re.compile(r"Status\s*:\s*✅\s*Confirmed", re.I),
    re.compile(r"^✅\s*Order\b.*Order ID", re.I | re.S),
]
SYS_FAILED = [
    re.compile(r"🛑\s*STATUS\s*:\s*Fail", re.I),
    re.compile(r"Payment\s+unsuccessful", re.I),
    re.compile(r"OrderStatus\s*:\s*(Failed|Cancel\w*|Expired|Closed)", re.I),
    re.compile(r"payment\s+has\s+not\s+been\s+received", re.I),
    re.compile(r"❌\s*UPI\s+Does\s+not\s+belong\s+to\s+us", re.I),
]
# A WITHDRAWAL that came back. Not a failed payment and not a pending one: it starts the Withdrawal Reversed flow
# (look the payout up, ask the operator to approve the refund). More wordings: BETIX_REVERSED_PATTERNS (config).
REVERSED = [
    re.compile(r"OrderStatus\s*:\s*Revers(?:ed|al)\b", re.I),
    re.compile(r"STATUS\s*:\s*Revers(?:ed|al)\b", re.I),
    re.compile(r"\brevers(?:ed|al)\b", re.I),
]
WITHDRAWAL_ID_RE = re.compile(r"\b(?:BX)?WD-\d{3,6}-\d{3,6}\b", re.I)


def is_reversed(text: str, *, status_line_only: bool = False) -> re.Match | None:
    """`status_line_only`: the system bot's long templates and notices may merely MENTION a reversal; for them only
    a status line ("OrderStatus: Reversed") or a configured pattern counts. A person's short "Reversed" counts."""
    from app.config import get_settings

    extra = []
    for raw in (get_settings().betix_reversed_patterns or "").split("||"):
        if raw.strip():
            try:
                extra.append(re.compile(raw.strip(), re.I))
            except re.error:
                continue  # a broken pattern in the config never stops the bot
    return _first([*(REVERSED[:2] if status_line_only else REVERSED), *extra], text or "")


SYS_PENDING = [
    re.compile(r"📌\s*STATUS\s*:\s*Still\s+Pending", re.I),
    re.compile(r"OrderStatus\s*:\s*(Pending|Paying|Init|Processing)", re.I),
    re.compile(r"Payment\s+has\s+not\s+been\s+confirmed", re.I),
    re.compile(r"Status\s*│\s*⏳\s*Pending", re.I),
    re.compile(r"UTR\s+was\s+not\s+found", re.I),
    re.compile(r"Resubmit\s+utr\s+result\s*:\s*Fail", re.I),
]
SYS_ACK = [
    re.compile(r"order\s+issue\s+has\s+been\s+recorded", re.I),
    re.compile(r"Recognition\s+results", re.I),
    re.compile(r"UPI\s+Belongs\s+to\s+us", re.I),
]
SYS_NOT_FOUND = [
    re.compile(r"^\s*(Order\s+)?not\s+found\s*!?\s*$", re.I),
    re.compile(r"Unable\s+to\s+recognize\s+UTR", re.I),
]
# ---- human reviewer phrases ----
HUMAN_SUCCESS = re.compile(
    r"^\s*(?:@\w+\s*)?(?:ok(?:ay)?[,\s]*)?(?:it'?s\s+|this\s+is\s+|payment\s+(?:is\s+)?)?"
    r"(?:success(?:ful(?:ly)?)?|confirm(?:ed)?|payment\s+confirmed|received|credited|done|paid|added|approved|"
    r"both\s+done|updated|settled|completed|matched|resolved)"
    r"(?:\s*(?:✔️|✅|👍|sir|team|dear|bro|bhai|now|already|thanks|thank\s+you|\.|!))*\s*$",
    re.I,
)
HUMAN_CHECKING = re.compile(
    r"^\s*(?:let\s+(?:us|me)\s+check|checking|pls\s+wait|please\s+wait|we\s+are\s+checking|"
    r"will\s+check|noted|we'?re\s+(?:already\s+)?following\s+up)",
    re.I,
)
HUMAN_FAILED = re.compile(
    r"\b(?:not\s+received|not\s+ours|reversed|refund(?:ed)?|failed|fake|not\s+found|"
    r"wrong\s+upi|invalid)\b",
    re.I,
)
# "Not received YET - we will notify you of any updates", "there is a delay, please wait another 48 hours": the
# payment is still being traced. That is PENDING (keep monitoring, follow-ups continue, no alert), not a failure.
HUMAN_PENDING = re.compile(
    r"\bnot\s+(?:yet\s+)?rece?i?e?ved\s+yet\b|\bnot\s+yet\s+rece?i?e?ved\b|\bhave\s+not\s+rece?i?e?ved\s+it\s+yet\b|"
    r"\bwill\s+(?:notify|update|inform|let\s+you\s+know)\b|\bnotify\s+you\b|\bkeep\s+you\s+(?:posted|updated)\b|"
    r"\bthere\s+is\s+a\s+delay\b|\bwait\s+(?:another|for\s+(?:another\s+)?\d+|\d+)\s*(?:hours?|hrs?|days?|minutes?|mins?)\b",
    re.I,
)
HUMAN_NEED_MORE = re.compile(r"\b(?:share|provide|send)\b.*\b(?:statement|video|pdf|utr|screenshot)\b", re.I)
# App share boilerplate that rides along with a forwarded file ("Edit, Sign and Share PDF files on the go.
# Download the Acrobat Reader app: https://...") must never read as a request for evidence.
SHARE_BOILERPLATE = re.compile(r"download\s+the\s+.{0,40}\bapp\b|https?://|acrobat|on\s+the\s+go", re.I)


@dataclass
class Classification:
    outcome: str  # SUCCESS | FAILED | PENDING | ACK | CHECKING | NEED_MORE_EVIDENCE | NOT_FOUND | UNKNOWN | IRRELEVANT
    confidence: float
    method: str = "regex"
    matched: str | None = None
    order_ids: list[str] = field(default_factory=list)
    plat_order_nos: list[str] = field(default_factory=list)
    utrs: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "confidence": self.confidence,
            "method": self.method,
            "matched": self.matched,
            "order_ids": self.order_ids,
            "plat_order_nos": self.plat_order_nos,
            "utrs": self.utrs,
            "extra": self.extra,
        }


def _first(patterns, text):
    for p in patterns:
        m = p.search(text)
        if m:
            return m
    return None


def extract_ids(
    text: str, betex_pattern: re.Pattern[str], plat_pattern: re.Pattern[str]
) -> tuple[list[str], list[str], list[str]]:
    orders = {m.group(0).upper() for m in betex_pattern.finditer(text or "")}
    # A withdrawal is known to Betix as "BXWD-4748-68114" (MerchantOrderNo): that is the case's order id here too
    for m in WITHDRAWAL_ID_RE.finditer(text or ""):
        wd = m.group(0).upper()
        orders.add(wd if wd.startswith("BX") else "BX" + wd)
    orders = sorted(orders)
    plats = sorted({m.group(0) for m in plat_pattern.finditer(text or "")})
    utrs = sorted({m.group(1) for m in re.finditer(r"(?:UTR\s*:?\s*(?:<code>)?\s*)(\d{12})", text or "", re.I)})
    return orders, plats, utrs


def classify_system_message(text: str, betex_pattern: re.Pattern[str], plat_pattern: re.Pattern[str]) -> Classification:
    t = text or ""
    orders, plats, utrs = extract_ids(t, betex_pattern, plat_pattern)
    rev = is_reversed(t, status_line_only=True)
    if rev:  # before everything else: "OrderStatus: Reversed" is neither a failed nor a pending order
        return Classification("FAILED", 0.97, "regex", rev.group(0)[:80], orders, plats, utrs, {"reversed": True})
    for outcome, pats, conf in (
        ("SUCCESS", SYS_SUCCESS, 0.99),
        ("FAILED", SYS_FAILED, 0.97),
        ("PENDING", SYS_PENDING, 0.95),
        ("NOT_FOUND", SYS_NOT_FOUND, 0.9),
        ("ACK", SYS_ACK, 0.9),
    ):
        m = _first(pats, t)
        if m:
            # "Already matched" without a Paid/Success status is still a success signal from the bot but we keep
            # confidence a notch lower so it can be reviewed in strict mode.
            c = conf if not (outcome == "SUCCESS" and m.re is SYS_SUCCESS[4]) else 0.9
            return Classification(outcome, c, "regex", m.group(0)[:80], orders, plats, utrs)
    # Broadcast notices, balance reports etc.
    if re.search(r"📢|Payout Notice|Account Balance|MerchantId:\s*B\d+\s*\n", t):
        return Classification("IRRELEVANT", 0.9, "regex", None, orders, plats, utrs)
    return Classification("UNKNOWN", 0.0, "regex", None, orders, plats, utrs)


def classify_human_message(
    text: str, betex_pattern: re.Pattern[str], plat_pattern: re.Pattern[str], *, has_media: bool = False
) -> Classification:
    t = (text or "").strip()
    orders, plats, utrs = extract_ids(t, betex_pattern, plat_pattern)
    if has_media and not HUMAN_SUCCESS.match(t) and not (t and HUMAN_FAILED.search(t) and len(t) < 300):
        # A person posting a file (statement PDF, video, screenshot) is SHARING evidence, not asking for it.
        return Classification("IRRELEVANT", 0.9, "regex", "media post", orders, plats, utrs)
    if not t:
        return Classification("UNKNOWN", 0.0, "regex", None, orders, plats, utrs)
    if t.startswith("/"):
        return Classification("IRRELEVANT", 0.95, "regex", "command", orders, plats, utrs)
    if HUMAN_SUCCESS.match(t):
        return Classification("SUCCESS", 0.9, "regex", t[:60], orders, plats, utrs)
    if HUMAN_NEED_MORE.search(t) and not SHARE_BOILERPLATE.search(t):
        return Classification("NEED_MORE_EVIDENCE", 0.85, "regex", t[:60], orders, plats, utrs)
    if HUMAN_CHECKING.match(t):
        return Classification("CHECKING", 0.9, "regex", t[:60], orders, plats, utrs)
    if HUMAN_PENDING.search(t):
        return Classification("PENDING", 0.85, "regex", t[:60], orders, plats, utrs)
    if HUMAN_FAILED.search(t) and len(t) < 300:
        extra = {"reversed": True} if is_reversed(t) else {}
        return Classification("FAILED", 0.75, "regex", t[:60], orders, plats, utrs, extra)
    if re.search(r"^\s*(?:success|confirmed|done|paid)\b", t, re.I):
        return Classification("SUCCESS", 0.7, "regex", t[:60], orders, plats, utrs)
    return Classification("UNKNOWN", 0.0, "regex", None, orders, plats, utrs)


# Who is speaking. The GROUP is the source of authority for humans: any member of the configured
# Betix support group may confirm a payment; nobody outside it can.
AUTHORITY_SYSTEM_BOT = "system_bot"  # the Betix verification bot (configured)
AUTHORITY_GROUP_MEMBER = "group_member"  # a human member of the Betix support group
AUTHORITY_SELF = "self"  # our own bot / user account
AUTHORITY_UNKNOWN = "unknown"  # anyone else: not in the group, or membership denied


def authority_for_sender(
    *,
    sender_id: int | None,
    username: str | None,
    is_bot: bool,
    in_betix_chat: bool,
    is_member: bool | None,
    system_bot_ids: set[int],
    system_bot_usernames: set[str],
    our_ids: set[int] = frozenset(),
    our_usernames: set[str] = frozenset(),
    reviewer_ids: set[int] = frozenset(),
    reviewer_usernames: set[str] = frozenset(),
) -> str:
    """Resolve the sender's authority.

    in_betix_chat  the message was posted in the configured Betix group
    is_member      result of a membership lookup (True/False), or None when it could not be checked;
                   None falls back to in_betix_chat (a message in the group implies membership when sent)
    reviewer_*     OPTIONAL extra restriction; empty = every group member may confirm
    """
    u = (username or "").lstrip("@").lower()
    if (sender_id is not None and sender_id in system_bot_ids) or (u and u in system_bot_usernames):
        return AUTHORITY_SYSTEM_BOT
    if (sender_id is not None and sender_id in our_ids) or (u and u in our_usernames):
        return AUTHORITY_SELF
    if not in_betix_chat:
        return AUTHORITY_UNKNOWN
    if is_member is False:
        return AUTHORITY_UNKNOWN
    if is_bot:
        return AUTHORITY_UNKNOWN  # other bots in the group are not human confirmers
    if reviewer_ids or reviewer_usernames:
        allowed = (sender_id is not None and sender_id in reviewer_ids) or (u and u in reviewer_usernames)
        if not allowed:
            return AUTHORITY_UNKNOWN
    return AUTHORITY_GROUP_MEMBER


def is_confirmed(*, mode: str, system_success: bool, reviewer_success: bool) -> bool:
    """either (default) / monitor: the Betix system bot OR a human group member confirmed the case.
    strict: BOTH must have confirmed."""
    if mode == "strict":
        return system_success and reviewer_success
    return system_success or reviewer_success
