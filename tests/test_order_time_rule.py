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
