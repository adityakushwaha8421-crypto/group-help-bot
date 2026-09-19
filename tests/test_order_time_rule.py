"""The Illunise order is created BEFORE the customer pays.

Created after the payment -> rejected. Created before -> a match, and the closer to the payment the stronger.
A near-miss on the clock never rejects an otherwise clear match. Live 2026-09-12 (ILLUN-17892043039970): order
created 14:41, payment 14:43, same mobile, ₹600 order for ₹599.00 - a clear match."""

import pytest

from app.admin.matcher import Candidate, match_orders
from app.ai.extractor import Extraction, Field
from app.utils.timeutil import parse_datetime_loose
from tests.test_matcher import KW2

MOB = "8652833104"


def ev(amount=599.0, t="2026-09-12 14:43:00", utr="618900952540"):
    e = Extraction()
    e.amount = Field(amount, 0.99, "payment_screenshot")
    e.payment_time = Field(parse_datetime_loose(t), 0.99, "payment_screenshot")
    e.utr = Field(utr, 0.99, "payment_screenshot")
    return e


def order(oid, created, *, amount=600.0, status="Pending", utr=None, mobile=MOB):
    return Candidate(
        illunise_order_id=oid,
        betex_order_id=oid,
        registration_number=mobile,
        amount=amount,
        order_time=parse_datetime_loose(created),
        status=status,
        gateway="BetixPay",
        utr=utr,
    )


@pytest.mark.parametrize(
    "created,matched",
    [
        ("2026-09-12 14:41:00", True),  # 2 min before  -> strong
        ("2026-09-12 14:42:00", True),  # 1 min before  -> very strong
        ("2026-09-12 14:43:00", True),  # same minute   -> possible
        ("2026-09-12 14:44:00", False),  # after the payment -> rejected
        ("2026-09-12 14:50:00", False),
    ],
)
def test_the_order_must_be_created_before_the_payment(created, matched):
    r = match_orders(ev(utr=None), MOB, [order("ILLUN-1", created)], **KW2)
    assert (r.decision == "MATCHED") is matched


def test_the_closest_order_before_the_payment_wins():
    orders = [
        order("ILLUN-FAR", "2026-09-12 14:20:00"),
        order("ILLUN-NEAR", "2026-09-12 14:41:00"),
        order("ILLUN-AFTER", "2026-09-12 14:44:00"),
    ]
    r = match_orders(ev(utr=None), MOB, orders, **KW2)
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "ILLUN-NEAR"
    assert [sc.candidate.illunise_order_id for sc in r.scored][-1] == "ILLUN-AFTER"


def test_a_stale_utr_on_an_unpaid_order_does_not_reject_it():
    """The live case: the pending order carries an earlier attempt's UTR. Mobile, amount, gateway and a 2-minute
    lead all agree, so the order is still the match."""
    o = order("ILLUN-17892043039970", "2026-09-12 14:41:00", utr="371648545677")
    r = match_orders(ev(), MOB, [o], **KW2)
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "ILLUN-17892043039970"
    assert r.best.score >= 0.95 and r.best.signals["utr"]["score"] is None
    assert "earlier attempt" in r.best.signals["utr"]["how"]


def test_a_different_utr_on_an_order_already_paid_still_rejects_it():
    o = order("ILLUN-PAID", "2026-09-12 14:41:00", status="Success", utr="371648545677")
    r = match_orders(ev(), MOB, [o], **KW2)
    assert r.decision == "NO_MATCH" and r.best.score <= 0.5


def test_the_same_utr_still_wins_over_a_sibling():
    ours = order("ILLUN-OURS", "2026-09-12 14:41:00", utr="618900952540")
    twin = order("ILLUN-TWIN", "2026-09-12 14:42:00")
    r = match_orders(ev(), MOB, [twin, ours], **KW2)
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "ILLUN-OURS"


def test_a_wrong_amount_is_still_rejected_whatever_the_time():
    r = match_orders(ev(), MOB, [order("ILLUN-1", "2026-09-12 14:41:00", amount=2500.0)], **KW2)
    assert r.decision == "NO_MATCH"


# ---------------------------------------------------------------- live 2026-09-18: expired order, earlier attempt's UTR
def test_expired_order_created_a_minute_before_with_a_stale_utr_is_selected(env):
    """Rs 2,878.99 paid 18 Sep 05:24; ILLUN-178968923584262 (Rs 2,879) created 05:23, Expired, and the panel holds
    the UTR of an earlier attempt. Everything else lines up, so the stale UTR must not cap it at 0.50."""
    from datetime import datetime, timedelta, timezone

    from app.admin.matcher import Candidate, match_orders
    from app.ai.extractor import Extraction, Field
    from app.config import get_settings

    s = get_settings()
    ist = timezone(timedelta(hours=5, minutes=30))
    ev = Extraction()
    ev.amount = Field(2878.99, 0.99, "payment_screenshot")
    ev.utr = Field("111122223333", 0.99, "payment_screenshot")
    ev.payment_time = Field(datetime(2026, 9, 18, 5, 24, tzinfo=ist), 0.99, "payment_screenshot")

    def cand(status, utr=None, ref="626195940467"):
        return Candidate(
            illunise_order_id="ILLUN-178968923584262", betex_order_id="ILLUN-178968923584262", amount=2879.0,
            registration_number="9022708364", order_time=datetime(2026, 9, 18, 5, 23, tzinfo=ist), status=status,
            utr=utr, gateway_ref=ref, gateway="BETIXPAY",
        )  # fmt: skip

    def run(c):
        return match_orders(
            ev, "9022708364", [c], weights=s.match_weights, threshold=s.order_match_threshold,
            ambiguity_gap=s.order_match_ambiguity_gap, time_window_minutes=s.payment_time_window_minutes,
            amount_tolerance=s.order_amount_tolerance, time_rule=s.time_rule, gateway_name=s.betix_gateway_name,
            compatible_statuses=s.compatible_statuses, expired_statuses=s.expired_statuses,
            success_statuses=s.success_statuses, time_tiebreak_minutes=s.order_time_tiebreak_minutes,
        )  # fmt: skip

    r = run(cand("Expired"))
    assert r.decision == "MATCHED" and r.best.candidate.betex_order_id == "ILLUN-178968923584262"
    assert r.best.signals["time"]["score"] == 1.0  # one minute before: no deduction at all
    assert r.best.signals["amount"]["score"] == 1.0  # Rs 2,879 = Rs 2,878.99 within the Rs 1 tolerance
    assert r.best.signals["utr"]["score"] is None and r.best.score >= 0.95  # the refNo never counts against it
    # the same refNo as the screenshot's UTR, on the other hand, pins the order
    ev.utr = Field("626195940467", 0.99, "payment_screenshot")
    pinned = run(cand("Expired"))
    assert pinned.decision == "MATCHED" and pinned.best.signals["utr"]["score"] == 1.0
    ev.utr = Field("111122223333", 0.99, "payment_screenshot")
    # a REAL different UTR in the panel's own UTR field still rules the order out (paid by another payment)
    assert run(cand("Success", utr="999988887777", ref=None)).decision != "MATCHED"
    assert run(cand("Expired", utr="999988887777", ref=None)).decision != "MATCHED"
