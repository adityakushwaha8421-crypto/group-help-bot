"""READ-ONLY replay of a historical Betix case against the live Illunise panel.

Input: a screenshot (already extracted by GPT-5.6 Terra, or extracted here) + the customer's mobile.
Steps: search Illunise by mobile -> candidates (+ View-page enrichment) -> deterministic matcher -> report.
Nothing is posted anywhere. Mobiles and UTRs are masked in the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.admin.matcher import match_orders
from app.admin.orders import find_candidates
from app.ai.extractor import Extraction
from app.config import get_settings
from app.utils.timeutil import fmt_local


def mask_mobile(m: str | None) -> str:
    return "-" if not m else m[:2] + "******" + m[-2:]


def mask_utr(u: str | None) -> str:
    return "-" if not u else u[:4] + "****" + u[-4:]


async def replay(mobile: str, extraction: dict, expected_order: str, tz: str) -> dict:
    s = get_settings()
    ev = Extraction.from_dict(extraction)
    candidates = await find_candidates(mobile, ev.amount.value, ev.payment_time.value)
    result = match_orders(
        ev,
        mobile,
        candidates,
        weights=s.match_weights,
        threshold=s.order_match_threshold,
        ambiguity_gap=s.order_match_ambiguity_gap,
        amount_tolerance=s.order_amount_tolerance,
        time_rule=s.time_rule,
        gateway_name=s.betix_gateway_name,
        compatible_statuses=s.compatible_statuses,
    )
    print("\nCASE TEST")
    print(f"MOBILE: {mask_mobile(mobile)}")
    print(f"PAYMENT AMOUNT: ₹{ev.amount.value:,.2f} (screenshot, conf {ev.amount.confidence})")
    print(f"PAYMENT TIME: {fmt_local(ev.payment_time.value, tz)} IST (screenshot, conf {ev.payment_time.confidence})")
    print(f"PAYMENT UTR: {mask_utr(ev.utr.value)}")
    print(
        f"\nCANDIDATE ORDERS (search by mobile returned {len(candidates)}; showing scored top 8 of {len(result.scored)}):"
    )
    for i, sc in enumerate(result.scored[:8], 1):
        c = sc.candidate
        print(
            f"{i}. {c.illunise_order_id} / ₹{(c.amount or 0):,.2f} / {fmt_local(c.order_time, tz)} / UTR {mask_utr(c.utr)} / "
            f"{c.status} / {c.gateway} / mobile {mask_mobile(c.registration_number)} / score {sc.score:.2f}"
        )
    best = result.best
    print(
        f"\nSELECTED ORDER: {best.candidate.illunise_order_id if best and result.decision == 'MATCHED' else '(none)'}"
    )
    if best:
        t = best.signals["time"]
        print(f"TIME DIFFERENCE: payment - order_created = {t['lead_minutes']} min ({t['rule']})")
        print(f"MATCH SCORE: {best.score:.2f} (threshold {s.order_match_threshold})")
        print("MATCHING SIGNALS:")
        for k in ("registration", "amount", "time", "gateway", "utr", "status"):
            sc_ = best.signals[k]["score"]
            print(
                f"  {k:12s}: {'PASS' if sc_ == 1.0 else 'FAIL' if sc_ == 0.0 else 'PARTIAL' if sc_ is not None else 'N/A'}"
                + (f"  ({best.signals[k].get('how')})" if k == "amount" else "")
            )
        print(f"BETIX GATEWAY: {'PASS' if best.signals['gateway']['score'] == 1.0 else 'FAIL'}")
        print(f"BETIX PLAT ORDER (from View page JSON, internal only): {best.candidate.betix_plat_order_no or '-'}")
        if result.runner_up:
            print(f"RUNNER-UP: {result.runner_up.candidate.illunise_order_id} score {result.runner_up.score:.2f}")
    verdict = (
        result.decision
        if result.decision != "MATCHED"
        else ("PASS" if best.candidate.illunise_order_id == expected_order else "FAIL (wrong order)")
    )
    print(f"EXPECTED: {expected_order}")
    print(f"RESULT: {verdict}   [{result.reason}]")
    return {
        "decision": result.decision,
        "selected": best.candidate.illunise_order_id if best else None,
        "expected": expected_order,
        "score": best.score if best else None,
        "candidates": len(candidates),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mobile", required=True)
    ap.add_argument("--extraction-json", required=True, help="GPT-5.6 Terra extraction (Extraction.as_dict())")
    ap.add_argument("--expected", required=True)
    a = ap.parse_args()
    ext = json.loads(Path(a.extraction_json).read_text())
    asyncio.run(replay(a.mobile, ext, a.expected, get_settings().timezone))


if __name__ == "__main__":
    main()
