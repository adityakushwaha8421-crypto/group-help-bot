"""`/search` — a stand-alone Illunise ORDER-ID lookup for the operator.

    /search                (or /search 7733931348)
    [payment screenshot]
    7733931348             the customer's mobile number; a registration number or UTR is accepted too

The bot extracts amount / time / UTR from the screenshot, searches the Illunise admin panel by the identifier,
scores the candidates with the same deterministic matcher the case flow uses, and answers with the order id.
Read-only: no case is created, nothing is posted to Betix, no follow-ups, no notifications.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from app.admin.browser import AdminError, LayoutChanged, LoginFailed, ManualAuthRequired
from app.admin.matcher import MatchResult, match_orders
from app.ai.analyzer import AIUnavailable, get_analyzer
from app.ai.extractor import Extraction, extract_from_text, extract_mobile
from app.cases import manager
from app.cases.correlation import IncomingInput
from app.config import get_settings
from app.evidence.manager import classify_media
from app.telegram.ui import b, code, esc, i, money, para
from app.utils.logging import get_logger
from app.utils.timeutil import fmt_local

log = get_logger("order_search")
SESSION_TTL_MINUTES = 30
INTRO_LINES = [
    "📥 Send me the payment screenshot and the customer's mobile number.",
    i("A registration number or UTR works instead of the mobile."),
    f"I'll reply with the Illunise order id. Nothing is posted to Betix. {code('/cancel')} stops the search.",
]


@dataclass
class SearchSession:
    chat_id: int
    user_id: int
    identifier: str | None = None
    is_mobile: bool = False
    screenshot_file_id: str | None = None
    text_extraction: Extraction = field(default_factory=Extraction)
    started_at: float = field(default_factory=time.monotonic)


_sessions: dict[int, SearchSession] = {}
_downloader = None  # tests inject a fake; production downloads via the Bot API


def set_downloader(fn) -> None:
    global _downloader
    _downloader = fn


def active(chat_id: int) -> SearchSession | None:
    sess = _sessions.get(chat_id)
    if sess and time.monotonic() - sess.started_at > SESSION_TTL_MINUTES * 60:
        _sessions.pop(chat_id, None)
        return None
    return sess


def start(chat_id: int, user_id: int, arg: str | None = None) -> SearchSession:
    sess = SearchSession(chat_id=chat_id, user_id=user_id)
    _sessions[chat_id] = sess
    if arg:
        set_identifier(sess, arg)
    return sess


def cancel(chat_id: int) -> bool:
    return _sessions.pop(chat_id, None) is not None


def finish(chat_id: int) -> None:
    _sessions.pop(chat_id, None)


def set_identifier(sess: SearchSession, text: str) -> bool:
    """A mobile number in any format wins; otherwise any single token (registration no., UTR) is the query."""
    mob = extract_mobile(text or "")
    if mob:
        sess.identifier, sess.is_mobile = mob, True
        return True
    token = (text or "").strip()
    if token and " " not in token and 4 <= len(token) <= 40 and not token.startswith("/"):
        sess.identifier, sess.is_mobile = token, False
        return True
    return False


def missing(sess: SearchSession) -> list[str]:
    out = []
    if not sess.screenshot_file_id:
        out.append("payment screenshot")
    if not sess.identifier:
        out.append("mobile number")
    return out


def status_lines(sess: SearchSession) -> list[str]:
    lines = ["✅ Payment screenshot" if sess.screenshot_file_id else "⏳ Payment screenshot — please send it"]
    if sess.identifier:
        lines.append(f"✅ {'Mobile number' if sess.is_mobile else 'Identifier'} · {code(sess.identifier)}")
    else:
        lines.append("⏳ Mobile number — please send it (a registration number or UTR works too)")
    return lines


def intro_card(sess: SearchSession) -> str:
    return para(f"🔎 {b('Order search')}", INTRO_LINES, status_lines(sess))


def take(sess: SearchSession, inp: IncomingInput) -> None:
    """Absorb one operator message into the search session."""
    s = get_settings()
    if inp.file_id and classify_media(inp.kind, inp.mime_type, inp.filename) == "payment_screenshot":
        sess.screenshot_file_id = inp.file_id
    text = (inp.text or "").strip()
    if text:
        if not sess.identifier:
            set_identifier(sess, text)
        sess.text_extraction = sess.text_extraction.merge(
            extract_from_text(text, registration_pattern=s.registration_pattern, betex_pattern=s.betex_order_id_pattern)
        )


async def _download_photo(file_id: str) -> Path:
    from app.telegram import notifications

    bot = notifications.get_bot()
    path = Path(get_settings().evidence_dir) / "search" / f"{file_id[-40:]}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    tg = await bot.get_file(file_id)
    await bot.download_file(tg.file_path, destination=str(path))
    return path


def format_result(sess: SearchSession, merged: Extraction, result: MatchResult, tz: str) -> str:
    what = "Mobile" if sess.is_mobile else "Identifier"
    inputs = [
        f"👤 {what}: {code(sess.identifier)}",
        f"💰 Amount: {money(merged.amount.value)}",
        f"🕒 Payment: {esc(fmt_local(merged.payment_time.value, tz, '%d %b %Y %H:%M'))}",
    ]
    if merged.utr.value:
        inputs.append(f"🔢 UTR: {code(merged.utr.value)}")

    def line(sc, icon="▫️"):
        c = sc.candidate
        return (
            f"{icon} {code(c.betex_order_id or c.illunise_order_id)} — match {b(f'{sc.score:.2f}')}\n"
            f"      🕒 created {esc(fmt_local(c.order_time, tz, '%d %b %H:%M'))} · 💰 {esc(money(c.amount))}"
            f" · {esc(c.status or '-')}" + (f" · UTR {code(c.utr)}" if c.utr else "")
        )

    if result.decision == "MATCHED":
        body = [f"🧾 {b('Order ID')}", line(result.best, "✅")]
        if result.runner_up:
            body += [i("Next best:"), line(result.runner_up)]
        if (result.best.candidate.status or "").strip().lower() in get_settings().success_statuses:
            body.append("♻️ Illunise already shows this order as successful — nothing to send to Betix.")
        else:
            body.append("🎯 " + b("Match found") + ".")
    elif result.decision == "AMBIGUOUS":
        body = [f"⚠️ {b('More than one order fits')} — check these in the panel:"]
        body += [line(sc) for sc in result.scored[:3]]
    elif result.decision == "NO_CANDIDATES":
        body = [f"❌ No orders found in Illunise for {code(sess.identifier)}."]
    else:
        body = [f"🚫 {b('No order matched')} this payment — amount, time or gateway do not line up."]
        if result.best:
            body += [i("Closest:"), line(result.best)]
    return para(f"🔎 {b('Order search result')}", inputs, body)


def error_card(text: str, hint: str | None = None) -> str:
    return para(f"🔎 {b('Order search')}", "❌ " + esc(text), i(hint) if hint else "")


async def run(sess: SearchSession) -> str:
    """Do the lookup and return the reply text."""
    s = get_settings()
    try:
        path = await (_downloader or _download_photo)(sess.screenshot_file_id)
    except Exception as exc:  # noqa: BLE001
        return error_card(f"Could not download the screenshot: {exc}")
    try:
        ex, _ = await get_analyzer().analyze_files(
            [path],
            doc_type_hint="payment_screenshot",
            context_hint=f"Customer identifier supplied by the operator: {sess.identifier}.",
        )
    except AIUnavailable as exc:
        return error_card(f"AI extraction is unavailable right now: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("search extraction failed")
        return error_card(f"Could not read the screenshot: {exc!r}")
    merged = ex.merge(sess.text_extraction)
    if merged.amount.value is None:
        return error_card(
            "Could not read the payment amount from the screenshot.",
            "Send a clearer screenshot, or type the amount (e.g. ₹1485) and send the screenshot again.",
        )
    try:
        candidates = await manager._get_order_search()(sess.identifier, merged.amount.value, merged.payment_time.value)
    except ManualAuthRequired as exc:
        return error_card(f"Admin login needs manual authentication: {exc}", "Run: python -m app.admin.login --manual")
    except LoginFailed as exc:
        return error_card(f"Admin login failed: {exc}")
    except LayoutChanged as exc:
        return error_card(f"Admin site layout changed / selectors not found: {exc}")
    except AdminError as exc:
        return error_card(f"Admin automation error: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("search order lookup crashed")
        return error_card(f"Order search error: {exc!r}")
    result = match_orders(
        merged,
        sess.identifier if sess.is_mobile else None,
        candidates,
        weights=s.match_weights,
        threshold=s.order_match_threshold,
        ambiguity_gap=s.order_match_ambiguity_gap,
        time_window_minutes=s.payment_time_window_minutes,
        amount_tolerance=s.order_amount_tolerance,
        time_rule=s.time_rule,
        gateway_name=s.betix_gateway_name,
        compatible_statuses=s.compatible_statuses,
        expired_statuses=s.expired_statuses,
        success_statuses=s.success_statuses,
        time_tiebreak_minutes=s.order_time_tiebreak_minutes,
    )
    log.info(
        "order search",
        identifier_tail=(sess.identifier or "")[-4:],
        decision=result.decision,
        candidates=len(candidates),
        best=result.best.candidate.betex_order_id if result.best else None,
    )
    return format_result(sess, merged, result, s.timezone)
