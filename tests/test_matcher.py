from app.admin.matcher import Candidate, match_orders
from app.ai.extractor import Extraction, Field
from app.utils.timeutil import parse_datetime_loose

W = {"registration": 0.35, "amount": 0.30, "time": 0.15, "utr": 0.10, "upi": 0.05, "payer": 0.05}
KW = dict(weights=W, threshold=0.90, ambiguity_gap=0.10, time_window_minutes=15)


def ev(amount=6499.92, t="2026-09-10 19:32:12", utr=None):
    e = Extraction()
    e.amount = Field(amount, 0.97, "payment_screenshot")
    e.payment_time = Field(parse_datetime_loose(t), 0.95, "payment_screenshot")
    if utr:
        e.utr = Field(utr, 0.96, "payment_screenshot")
    return e


def cand(oid, amount, t, reg="REG123456", betex=None, utr=None):
    return Candidate(
        illunise_order_id=oid,
        betex_order_id=betex or f"ILLUN-{oid}",
        registration_number=reg,
        amount=amount,
        order_time=parse_datetime_loose(t),
        utr=utr,
    )


def test_spec_example_scores_a_over_b():
    r = match_orders(
        ev(),
        "REG123456",
        [
            cand("1785000000000001", 6499.92, "2026-09-10 19:30:00"),
            cand("1785000000000002", 500.0, "2026-09-09 10:00:00"),
        ],
        **KW,
    )
    assert r.decision == "MATCHED"
    assert r.best.candidate.betex_order_id == "ILLUN-1785000000000001"
    assert r.best.score >= 0.95
    assert r.runner_up.score <= 0.51


def test_ambiguous_when_two_candidates_identical():
    # both created in the same minute before the payment (19:32): nothing tells them apart
    r = match_orders(
        ev(), "REG123456", [cand("1", 6499.92, "2026-09-10 19:30:00"), cand("2", 6499.92, "2026-09-10 19:30:40")], **KW
    )
    assert r.decision == "AMBIGUOUS"


def test_utr_breaks_tie():
    r = match_orders(
        ev(utr="611532946151"),
        "REG123456",
        [
            cand("1", 6499.92, "2026-09-10 19:30:00", utr="611532946151"),
            cand("2", 6499.92, "2026-09-10 19:35:00", utr="999999999999"),
        ],
        **KW,
    )
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "1"


def test_amount_mismatch_caps_score_and_no_match():
    r = match_orders(ev(amount=100.0), "REG123456", [cand("1", 6499.92, "2026-09-10 19:30:00")], **KW)
    assert r.decision == "NO_MATCH" and r.best.score <= 0.5


def test_time_outside_window_reduces_score():
    r = match_orders(ev(t="2026-09-10 21:00:00"), "REG123456", [cand("1", 6499.92, "2026-09-10 19:30:00")], **KW)
    assert r.decision == "NO_MATCH"
    assert r.best.signals["time"]["score"] == 0.0


def test_no_candidates_and_missing_betex_id():
    assert match_orders(ev(), "REG1", [], **KW).decision == "NO_CANDIDATES"
    c = cand("1", 6499.92, "2026-09-10 19:30:00")
    c.betex_order_id = None
    r = match_orders(ev(), "REG123456", [c], **KW)
    assert r.decision == "NO_MATCH" and "no Betex Pay order id" in r.reason


def test_single_signal_never_confident():
    e = Extraction()
    e.amount = Field(500.0, 0.9, "payment_screenshot")
    c = Candidate("1", "ILLUN-1", registration_number=None, amount=500.0)
    r = match_orders(e, None, [c], **KW)
    assert r.decision == "NO_MATCH" and r.best.score <= 0.6


# ---------------------------------------------------------------- the before-payment time rule + new signals
RULE = {"min_before": 0, "max_before": 30, "tolerance": 3}
KW2 = dict(
    weights={
        "registration": 0.30,
        "amount": 0.25,
        "time": 0.20,
        "gateway": 0.10,
        "utr": 0.10,
        "status": 0.05,
        "upi": 0,
        "payer": 0,
    },
    threshold=0.90,
    ambiguity_gap=0.10,
    amount_tolerance=1.0,
    time_rule=RULE,
    gateway_name="BetixPay",
    compatible_statuses={"pending", "success", "paid"},
    expired_statuses={"expired", "timeout"},
    success_statuses={"success", "successful", "paid", "completed"},
)


def mobile_cand(oid, amount, created, *, status="Success", gateway="BetixPay", utr=None, mobile="9876543210"):
    return Candidate(
        illunise_order_id=oid,
        betex_order_id=oid,
        registration_number=mobile,
        amount=amount,
        order_time=parse_datetime_loose(created),
        status=status,
        gateway=gateway,
        utr=utr,
    )


def test_order_created_just_before_payment_wins_over_older_order():
    """The user's example: Order A created at 15:09, Order B at 14:20, payment at 15:10."""
    e = ev(amount=1485.0, t="2026-09-10 15:10:00")
    r = match_orders(
        e,
        "9876543210",
        [
            mobile_cand("ILLUN-178903323", 1485.0, "2026-09-10 15:09:00"),
            mobile_cand("ILLUN-178900000", 1485.0, "2026-09-10 14:20:00"),
        ],
        **KW2,
    )
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "ILLUN-178903323"
    assert r.best.signals["time"]["lead_minutes"] == 1.0 and r.best.signals["time"]["score"] >= 0.98
    assert r.runner_up.signals["time"]["score"] < 0.7 and r.runner_up.score <= 0.7


def test_padded_amount_matches_within_tolerance_and_exact_padded_wins():
    e = ev(amount=13999.35, t="2026-09-09 12:04:00")
    c = mobile_cand("ILLUN-1", 14000.0, "2026-09-09 12:03:33")
    r = match_orders(e, "9876543210", [c], **KW2)
    assert r.decision == "MATCHED" and r.best.signals["amount"]["how"].startswith("within tolerance")
    c.padded_amount = 13999.35
    r = match_orders(e, "9876543210", [c], **KW2)
    assert r.best.signals["amount"]["how"] == "exact padded amount"
    r = match_orders(ev(amount=13990.0, t="2026-09-09 12:04:00"), "9876543210", [c], **KW2)
    assert r.decision == "NO_MATCH" and r.best.signals["amount"]["score"] == 0.0


def test_order_created_after_payment_is_rejected():
    """The order is always created BEFORE the customer pays: anything created after the payment minute is out."""
    e = ev(amount=500.0, t="2026-09-10 15:00:00")
    same_minute = mobile_cand("A", 500.0, "2026-09-10 15:00:00")
    assert match_orders(e, "9876543210", [same_minute], **KW2).best.signals["time"]["score"] == 1.0
    for created in ("2026-09-10 15:01:00", "2026-09-10 15:03:00", "2026-09-10 15:20:00"):
        r = match_orders(e, "9876543210", [mobile_cand("B", 500.0, created)], **KW2)
        assert r.best.signals["time"]["score"] == 0.0 and r.decision == "NO_MATCH", created
        assert "AFTER" in r.best.signals["time"]["how"]


def test_gateway_and_status_signals_cap_the_score():
    e = ev(amount=500.0, t="2026-09-10 15:00:00")
    other_gw = mobile_cand("A", 500.0, "2026-09-10 14:58:00", gateway="F2Pay")
    expired = mobile_cand("B", 500.0, "2026-09-10 14:58:00", status="Failed")
    good = mobile_cand("C", 500.0, "2026-09-10 14:58:00")
    r = match_orders(e, "9876543210", [good, other_gw, expired], **KW2)
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "C"
    by = {s.candidate.illunise_order_id: s for s in r.scored}
    assert by["A"].signals["gateway"]["score"] == 0.0 and by["A"].score <= 0.5
    assert by["B"].signals["status"]["score"] == 0.0 and by["B"].score <= 0.6


def test_closest_order_before_the_payment_wins():
    """Two identical orders both created before the payment: the one created closest to the payment is chosen."""
    e = ev(amount=500.0, t="2026-09-10 15:00:00")
    r = match_orders(
        e,
        "9876543210",
        [mobile_cand("B", 500.0, "2026-09-10 14:55:00"), mobile_cand("A", 500.0, "2026-09-10 14:58:00")],
        **KW2,
    )
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "A"
    assert "closest to the payment time: created 2 min before it" in r.reason


def test_two_identical_orders_in_the_same_minute_are_ambiguous():
    e = ev(amount=500.0, t="2026-09-10 15:00:00")
    r = match_orders(
        e,
        "9876543210",
        [mobile_cand("A", 500.0, "2026-09-10 14:58:00"), mobile_cand("B", 500.0, "2026-09-10 14:58:30")],
        **KW2,
    )
    assert r.decision == "AMBIGUOUS"


def test_utr_resolves_identical_orders():
    e = ev(amount=500.0, t="2026-09-10 15:00:00", utr="625200004074")
    r = match_orders(
        e,
        "9876543210",
        [
            mobile_cand("A", 500.0, "2026-09-10 14:58:00", utr="625200004074"),
            mobile_cand("B", 500.0, "2026-09-10 14:55:00", utr="111100002222"),
        ],
        **KW2,
    )
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "A"


# ---------------------------------------------------------------- expired orders (live case 2026-09-10, ₹2,999.05)
def test_expired_order_with_the_same_utr_is_the_order():
    """Mobile, amount (padded), time (0 min lead), gateway and UTR all agree; the panel says Expired because the
    customer paid after the window. That order IS the one - it must not be capped at 0.60."""
    e = ev(amount=2999.05, t="2026-09-10 19:18:00", utr="877586526811")
    right = mobile_cand("ILLUN-178904809881641", 3000.0, "2026-09-10 19:18:00", status="Expired", utr="877586526811")
    others = [
        mobile_cand("ILLUN-178905012581641", 2000.0, "2026-09-10 19:52:00", status="Pending"),
        mobile_cand("ILLUN-178904677981641", 1500.0, "2026-09-10 18:56:00", status="Success", utr="048739815993"),
    ]
    r = match_orders(e, "9876543210", [right, *others], **KW2)
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "ILLUN-178904809881641"
    assert r.best.score >= 0.99 and r.best.signals["status"]["score"] == 1.0
    assert "same UTR" in r.best.signals["status"]["how"]


def test_expired_order_without_utr_still_matches_on_mobile_amount_time():
    e = ev(amount=2999.05, t="2026-09-10 19:18:00")
    c = mobile_cand("A", 3000.0, "2026-09-10 19:18:00", status="Expired")
    r = match_orders(e, "9876543210", [c], **KW2)
    assert r.decision == "MATCHED" and r.best.signals["status"]["score"] == 0.5 and r.best.score >= 0.9


def test_expired_order_with_a_different_utr_is_not_the_order():
    e = ev(amount=2999.05, t="2026-09-10 19:18:00", utr="877586526811")
    c = mobile_cand("A", 3000.0, "2026-09-10 19:18:00", status="Expired", utr="111122223333")
    r = match_orders(e, "9876543210", [c], **KW2)
    assert r.decision == "NO_MATCH" and r.best.score <= 0.5


def test_failed_or_cancelled_orders_stay_capped():
    e = ev(amount=2999.05, t="2026-09-10 19:18:00", utr="877586526811")
    for st in ("Failed", "Cancelled", "Refunded"):
        c = mobile_cand("A", 3000.0, "2026-09-10 19:18:00", status=st, utr="877586526811")
        r = match_orders(e, "9876543210", [c], **KW2)
        assert r.decision == "NO_MATCH" and r.best.score <= 0.6, st


# ---------------------------------------------------------------- live case 2026-09-11, ₹199.83 (two ₹200 orders 1 min apart)
def test_unique_utr_beats_a_sibling_order_that_has_no_utr():
    """Two ₹200 orders for the same mobile, created 18:27 and 18:28, both Expired. The 18:28 order holds the
    screenshot's UTR; the 18:27 one holds none, so nothing counted against it and it scored 0.97 - close enough
    to be called AMBIGUOUS. The UTR is unique: the order that holds it is the order."""
    e = ev(amount=199.83, t="2026-09-10 18:28:00", utr="456768625025")
    right = mobile_cand("ILLUN-178904509913205", 200.0, "2026-09-10 18:28:00", status="Expired", utr="456768625025")
    twin = mobile_cand("ILLUN-178904506713205", 200.0, "2026-09-10 18:27:00", status="Expired")
    r = match_orders(e, "9876543210", [twin, right], **KW2)
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "ILLUN-178904509913205"
    assert r.runner_up.candidate.illunise_order_id == "ILLUN-178904506713205" and r.runner_up.score >= 0.9
    assert "unique UTR 456768625025" in r.reason


def test_unique_utr_does_not_rescue_a_low_score():
    """The pin only settles closeness; it never lifts a candidate over the threshold (e.g. amount mismatch)."""
    e = ev(amount=199.83, t="2026-09-10 18:28:00", utr="456768625025")
    wrong_amount = mobile_cand("A", 900.0, "2026-09-10 18:28:00", status="Expired", utr="456768625025")
    twin = mobile_cand("B", 200.0, "2026-09-10 18:27:00", status="Expired")
    r = match_orders(e, "9876543210", [wrong_amount, twin], **KW2)
    assert r.best.candidate.illunise_order_id != "A" or r.decision != "MATCHED"


def test_two_orders_with_the_same_utr_stay_ambiguous():
    e = ev(amount=199.83, t="2026-09-10 18:28:00", utr="456768625025")
    a = mobile_cand("A", 200.0, "2026-09-10 18:28:00", status="Expired", utr="456768625025")
    b = mobile_cand("B", 200.0, "2026-09-10 18:27:00", status="Expired", utr="456768625025")
    assert match_orders(e, "9876543210", [a, b], **KW2).decision == "AMBIGUOUS"


# ---------------------------------------------------------------- live case 2026-09-11, ₹5,699.85 (six ₹5,700 orders, same UTR on all)
def test_same_utr_on_every_order_the_one_created_just_before_the_payment_wins():
    """The customer created six ₹5,700 orders and submitted the same UTR to each, so the UTR pins nothing.
    Payment 18:24. Created 18:23 -> the order. Created 18:27 / 18:30 / 18:37 -> after the payment: rejected."""
    e = ev(amount=5699.85, t="2026-09-10 18:24:00", utr="661902584929")
    mk = lambda oid, t: mobile_cand(oid, 5700.0, t, status="Expired", utr="661902584929", mobile="8087457374")
    orders = [
        mk("ILLUN-178904504782951", "2026-09-10 18:27:00"),
        mk("ILLUN-178904483482951", "2026-09-10 18:23:00"),
        mk("ILLUN-178904524182951", "2026-09-10 18:30:00"),
        mk("ILLUN-178904565282951", "2026-09-10 18:37:00"),
        mk("ILLUN-178904850782951", "2026-09-10 19:25:00"),
        mk("ILLUN-178909623982951", "2026-09-11 08:40:00"),
    ]
    r = match_orders(e, "8087457374", orders, **KW2)
    assert r.decision == "MATCHED" and r.best.candidate.illunise_order_id == "ILLUN-178904483482951"
    assert r.best.score >= 0.95 and r.runner_up.score <= 0.6
    assert all(sc.signals["time"]["score"] == 0.0 for sc in r.scored[1:])


def test_closest_order_scores_highest_among_earlier_orders():
    e = ev(amount=500.0, t="2026-09-10 15:00:00")
    leads = {}
    for m in (0, 1, 5, 15, 30):
        r = match_orders(
            e, "9876543210", [mobile_cand("A", 500.0, f"2026-09-10 {14 if m else 15}:{(60 - m) % 60:02d}:00")], **KW2
        )
        leads[m] = r.best.signals["time"]["score"]
        assert r.decision == "MATCHED", m  # inside the window: still auto-selectable
    # a minute or two before the payment costs nothing (both clocks show minutes only); after that, closest wins
    assert leads[0] == leads[1] == 1.0 and leads[1] > leads[5] > leads[15] > leads[30] >= 0.7
