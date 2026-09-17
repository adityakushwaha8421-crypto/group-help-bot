"""Deterministic order matching engine. Scores every candidate against the evidence; never picks
'the first result'. Returns MATCHED only above the configured threshold and with a clear margin."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.ai.extractor import Extraction, normalize_amount, normalize_registration, normalize_upi, normalize_utr


@dataclass
class Candidate:
    illunise_order_id: str | None
    betex_order_id: str | None
    registration_number: str | None = None  # the customer's registered mobile
    amount: float | None = None  # order amount as created (₹14,000.00)
    padded_amount: float | None = None  # amount the customer actually paid, if the panel exposes it
    found_by: str = "mobile"  # which search produced it: mobile | utr | padded_amount
    order_time: datetime | None = None  # order CREATED time
    status: str | None = None
    utr: str | None = None
    upi_id: str | None = None
    payer_name: str | None = None
    gateway: str | None = None
    # Betix's own order id (platOrderNo), read from the "raw BetixPay response" JSON on the View page.
    betix_plat_order_no: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scored:
    candidate: Candidate
    score: float
    signals: dict[str, Any]


@dataclass
class MatchResult:
    decision: str  # MATCHED | AMBIGUOUS | NO_MATCH | NO_CANDIDATES
    best: Scored | None
    runner_up: Scored | None
    scored: list[Scored]
    reason: str

    @property
    def confidence(self) -> float | None:
        return self.best.score if self.best else None


def _time_score(
    payment_time: datetime | None, order_created: datetime | None, rule: dict[str, int]
) -> tuple[float | None, float | None, str | None]:
    """Score how well `order_created` sits BEFORE `payment_time`.

    lead = payment_time - order_created (minutes). The order is always created first, so:
      lead < min_before (created AFTER the payment)      -> 0.0, rejected
      0 .. max_before                                    -> 1.0 at the payment minute, sliding down to 0.7 at
                                                            max_before: the CLOSEST order scores highest
      max_before .. max_before + tolerance               -> 0.6 (just outside the window)
      .. 2 x max_before                                  -> 0.3
      beyond                                             -> 0.0"""
    if payment_time is None or order_created is None:
        return None, None, None
    from app.utils.timeutil import ensure_utc

    lead = (ensure_utc(payment_time) - ensure_utc(order_created)).total_seconds() / 60.0
    lo, hi, tol = rule["min_before"], rule["max_before"], rule["tolerance"]
    if lead < lo:
        return 0.0, lead, "created AFTER the payment: not this order"
    if lead <= hi:
        return round(1.0 - 0.3 * (lead - lo) / max(hi - lo, 1), 4), lead, "created before the payment"
    if lead <= hi + tol:
        return 0.6, lead, "created just outside the window"
    if lead <= 2 * hi:
        return 0.3, lead, "created too long before the payment"
    return 0.0, lead, "created far too long before the payment"


def _name_score(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    a, b = a.strip().lower(), b.strip().lower()
    if a == b:
        return 1.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    if r >= 0.85:
        return 1.0
    if r >= 0.6:
        return 0.5
    # last/first token containment
    ta, tb = set(a.split()), set(b.split())
    if ta & tb:
        return 0.5
    return 0.0


def score_candidate(
    ev: Extraction,
    registration: str | None,
    cand: Candidate,
    *,
    weights: dict[str, float],
    time_window_minutes: int = 15,
    amount_tolerance: float = 1.0,
    time_rule: dict[str, int] | None = None,
    gateway_name: str | None = None,
    compatible_statuses: set[str] | None = None,
    expired_statuses: set[str] | None = None,
    success_statuses: set[str] | None = None,
) -> Scored:
    rule = time_rule or {"min_before": 0, "max_before": time_window_minutes, "tolerance": 3}
    signals: dict[str, Any] = {}
    parts: list[tuple[str, float | None]] = []

    reg_ev = normalize_registration(registration or ev.registration_number.value)
    reg_c = normalize_registration(cand.registration_number)
    if reg_ev and reg_c:
        s = 1.0 if reg_ev == reg_c else (0.6 if (reg_ev in reg_c or reg_c in reg_ev) else 0.0)
    elif reg_ev and not reg_c:
        s = None  # panel didn't expose the field; do not penalise, do not reward
    else:
        s = None
    signals["registration"] = {"evidence": reg_ev, "candidate": reg_c, "score": s}
    parts.append(("registration", s))

    # Amount: exact against the padded (paid) amount when the panel exposes it, else within the tolerance
    # of the order amount (the customer pays ₹13,999.35 for a ₹14,000.00 order).
    amt_ev = normalize_amount(ev.amount.value)
    amt_c = normalize_amount(cand.amount)
    amt_p = normalize_amount(cand.padded_amount)
    if amt_ev is not None and (amt_c is not None or amt_p is not None):
        if amt_p is not None and abs(amt_ev - amt_p) < 0.005:
            s, how = 1.0, "exact padded amount"
        elif amt_c is not None and abs(amt_ev - amt_c) < 0.005:
            s, how = 1.0, "exact order amount"
        elif amt_c is not None and abs(amt_ev - amt_c) <= amount_tolerance + 1e-9:
            s, how = 1.0, f"within tolerance {amount_tolerance:.2f} of order amount"
        else:
            s, how = 0.0, "mismatch"
    else:
        s, how = None, "not comparable"
    signals["amount"] = {"evidence": amt_ev, "candidate": amt_c, "padded": amt_p, "score": s, "how": how}
    parts.append(("amount", s))

    s, lead, how = _time_score(ev.payment_time.value, cand.order_time, rule)
    signals["time"] = {
        "payment": ev.payment_time.value.isoformat() if ev.payment_time.value else None,
        "order_created": cand.order_time.isoformat() if cand.order_time else None,
        "lead_minutes": round(lead, 1) if lead is not None else None,
        "rule": f"created {rule['min_before']}-{rule['max_before']} min before payment (+{rule['tolerance']})",
        "score": s,
        "how": how,
    }
    parts.append(("time", s))

    # Gateway: the order must belong to Betix.
    if gateway_name and cand.gateway:
        s = 1.0 if gateway_name.lower().replace(" ", "") in cand.gateway.lower().replace(" ", "") else 0.0
    else:
        s = None
    signals["gateway"] = {"expected": gateway_name, "candidate": cand.gateway, "score": s}
    parts.append(("gateway", s))

    u_ev, u_c = normalize_utr(ev.utr.value), normalize_utr(cand.utr)
    s = (1.0 if u_ev == u_c else 0.0) if (u_ev and u_c) else None
    signals["utr"] = {"evidence": u_ev, "candidate": u_c, "score": s}
    parts.append(("utr", s))
    utr_exact = s == 1.0

    # Status, three tiers:
    #   compatible (pending / success / paid ...)          -> 1.0
    #   EXPIRED: the customer paid after the order's window; the panel keeps the order and, when the gateway
    #            saw the money, its UTR. Same UTR -> this IS the order (1.0); otherwise still plausible (0.5).
    #            Never capped: this is exactly the case Betix has to confirm by hand.
    #   failed / cancelled / refunded ...                  -> 0.0 and the score is capped
    how = None
    status_tier = None
    if compatible_statuses and cand.status:
        st = cand.status.strip().lower()
        if any(st.startswith(c) for c in compatible_statuses):
            s = 1.0
            status_tier = "open"
        elif expired_statuses and any(st.startswith(c) for c in expired_statuses):
            s = 1.0 if utr_exact else 0.5
            status_tier = "expired"
            how = (
                "expired, but the panel holds the same UTR: paid after expiry"
                if utr_exact
                else "expired; paid after expiry?"
            )
        else:
            s = 0.0
    else:
        s = None
    signals["status"] = {"candidate": cand.status, "score": s, "how": how}
    parts.append(("status", s))

    # A different UTR on an order that has NOT been paid yet (pending / paying / expired) is the reference of an
    # earlier attempt, not proof that this payment belongs somewhere else. When the customer's number, the amount,
    # the gateway and the creation time all line up, it stops counting against the order. On an order Illunise
    # already shows as SUCCESS a different UTR still rules it out: that order was completed by another payment.
    already_paid = bool(success_statuses) and (cand.status or "").strip().lower() in (success_statuses or set())
    if (
        signals["utr"]["score"] == 0.0
        and status_tier == "open"
        and not already_paid
        and signals["registration"]["score"] == 1.0
        and signals["amount"]["score"] == 1.0
        and (signals["time"]["score"] or 0.0) >= 0.7
        and signals["gateway"]["score"] in (1.0, None)
    ):
        signals["utr"] = {
            **signals["utr"],
            "score": None,
            "how": "different UTR on an unpaid order: an earlier attempt, so it does not count against it",
        }
        parts = [(k, None if k == "utr" else v) for k, v in parts]

    # A different registered number is overruled by a STRONGER identifier: the very same UTR, or the very same
    # padded amount (the gateway pads every order with unique paise). Either one pins the payment to this order;
    # the customer simply gave another number.
    padded_exact = signals["amount"].get("how") == "exact padded amount"
    if signals["registration"]["score"] == 0.0 and (utr_exact or padded_exact):
        why = "same UTR" if utr_exact else "same padded amount"
        signals["registration"] = {
            **signals["registration"],
            "score": 1.0,
            "how": f"different registered number, but {why} - the stronger identifier wins",
        }
        parts = [(k, 1.0 if k == "registration" else v) for k, v in parts]

    p_ev, p_c = normalize_upi(ev.upi_id.value), normalize_upi(cand.upi_id)
    s = (1.0 if p_ev == p_c else 0.0) if (p_ev and p_c) else None
    signals["upi"] = {"evidence": p_ev, "candidate": p_c, "score": s}
    parts.append(("upi", s))

    s = _name_score(ev.payer_name.value, cand.payer_name)
    signals["payer"] = {"evidence": ev.payer_name.value, "candidate": cand.payer_name, "score": s}
    parts.append(("payer", s))

    # Weighted average over the signals that could actually be compared.
    avail = [(k, v) for k, v in parts if v is not None]
    total_w = sum(weights.get(k, 0) for k, _ in avail)
    score = sum(weights.get(k, 0) * v for k, v in avail) / total_w if total_w else 0.0

    # Hard rules: a comparable signal that DISAGREES caps the score, whatever the rest says.
    if signals["registration"]["score"] == 0.0:
        score = min(score, 0.3)
    if signals["amount"]["score"] == 0.0:
        score = min(score, 0.5)
    if signals["utr"]["score"] == 0.0:
        score = min(score, 0.5)
    if signals["gateway"]["score"] == 0.0:
        score = min(score, 0.5)
    if signals["time"]["score"] == 0.0:
        score = min(score, 0.6)  # created after the payment, or far too long before it
    elif signals["time"]["score"] is not None and signals["time"]["score"] < 0.7:
        score = min(score, 0.7)  # outside the before-payment window: never auto-selectable
    if signals["status"]["score"] == 0.0:
        score = min(score, 0.6)
    # With only one comparable signal we can never be confident.
    if len(avail) < 2:
        score = min(score, 0.6)
    signals["_comparable"] = [k for k, _ in avail]
    return Scored(cand, round(score, 4), signals)


def pinned_by(sc: Scored) -> tuple[str, str] | None:
    """The unique identifier this candidate shares with the evidence, if any: ("utr", value) when the panel holds
    the very same UTR, ("padded_amount", value) when the paid paise amount is identical. None otherwise."""
    if sc.signals.get("utr", {}).get("score") == 1.0:
        return "utr", str(sc.signals["utr"]["candidate"])
    amt = sc.signals.get("amount", {})
    if amt.get("how") == "exact padded amount":
        return "padded_amount", f"{amt['padded']:.2f}"
    return None


def _lead(sc: Scored) -> float | None:
    return sc.signals.get("time", {}).get("lead_minutes")


def match_orders(
    ev: Extraction,
    registration: str | None,
    candidates: list[Candidate],
    *,
    weights: dict[str, float],
    threshold: float,
    ambiguity_gap: float,
    time_window_minutes: int = 15,
    amount_tolerance: float = 1.0,
    time_rule: dict[str, int] | None = None,
    gateway_name: str | None = None,
    compatible_statuses: set[str] | None = None,
    expired_statuses: set[str] | None = None,
    success_statuses: set[str] | None = None,
    time_tiebreak_minutes: int = 2,
) -> MatchResult:
    if not candidates:
        return MatchResult("NO_CANDIDATES", None, None, [], "no candidate orders returned by the admin search")
    scored = sorted(
        (
            score_candidate(
                ev,
                registration,
                c,
                weights=weights,
                time_window_minutes=time_window_minutes,
                amount_tolerance=amount_tolerance,
                time_rule=time_rule,
                gateway_name=gateway_name,
                compatible_statuses=compatible_statuses,
                expired_statuses=expired_statuses,
                success_statuses=success_statuses,
            )
            for c in candidates
        ),
        key=lambda s: s.score,
        reverse=True,
    )
    best, runner = scored[0], (scored[1] if len(scored) > 1 else None)
    if best.score < threshold:
        return MatchResult(
            "NO_MATCH", best, runner, scored, f"best candidate score {best.score:.2f} below threshold {threshold:.2f}"
        )
    # A UNIQUE identifier is decisive and overrides the closeness test: the gateway's UTR (or the padded paise
    # amount) belongs to exactly one order. A sibling order that merely lacks the field is not "as good" - it
    # scored close only because a missing field is neither rewarded nor penalised.
    # The very same UTR belongs to exactly one order: it is the order, even if a sibling scored a shade higher.
    holders = [sc for sc in scored if sc.signals.get("utr", {}).get("score") == 1.0]
    if len(holders) == 1 and holders[0].score >= threshold and holders[0].candidate.betex_order_id:
        best = holders[0]
        runner = next((sc for sc in scored if sc is not best), None)
        held = best.signals["utr"]["candidate"]
        return MatchResult(
            "MATCHED",
            best,
            runner,
            scored,
            f"score {best.score:.2f} >= {threshold:.2f}; unique UTR {held} pins this order",
        )
    pin = pinned_by(best)
    if pin and runner and not any(pinned_by(sc) == pin for sc in scored[1:]):
        if not best.candidate.betex_order_id:
            return MatchResult(
                "NO_MATCH", best, runner, scored, "matched order has no Betex Pay order id in the admin record"
            )
        what = "UTR" if pin[0] == "utr" else "padded amount"
        return MatchResult(
            "MATCHED",
            best,
            runner,
            scored,
            f"score {best.score:.2f} >= {threshold:.2f}; unique {what} {pin[1]} pins this order "
            f"(runner-up {runner.score:.2f} does not hold it)",
        )
    # Otherwise the order created CLOSEST before the payment is the one, provided it is clearly closer than the
    # runner-up (a different minute, by at least `time_tiebreak_minutes`).
    lead_b, lead_r = _lead(best), (_lead(runner) if runner else None)
    if (
        runner
        and lead_b is not None
        and lead_r is not None
        and lead_b >= 0
        and (lead_r - lead_b) >= time_tiebreak_minutes - 1e-9
        and best.candidate.betex_order_id
    ):
        return MatchResult(
            "MATCHED",
            best,
            runner,
            scored,
            f"score {best.score:.2f} >= {threshold:.2f}; closest to the payment time: created {lead_b:.0f} min "
            f"before it (runner-up {runner.score:.2f}: {lead_r:.0f} min before)",
        )
    if runner and runner.score >= threshold - 1e-9 and (best.score - runner.score) < ambiguity_gap:
        return MatchResult(
            "AMBIGUOUS", best, runner, scored, f"top candidates too close: {best.score:.2f} vs {runner.score:.2f}"
        )
    if runner and (best.score - runner.score) < ambiguity_gap and runner.score >= 0.75:
        return MatchResult(
            "AMBIGUOUS",
            best,
            runner,
            scored,
            f"runner-up {runner.score:.2f} within ambiguity gap of best {best.score:.2f}",
        )
    if not best.candidate.betex_order_id:
        return MatchResult(
            "NO_MATCH", best, runner, scored, "matched order has no Betex Pay order id in the admin record"
        )
    return MatchResult("MATCHED", best, runner, scored, f"score {best.score:.2f} >= {threshold:.2f}")
