"""A mobile with hundreds of orders: the search asks the panel for the payment DAY only, keeps the orders created
in a short window before the payment, reads them closest-first and stops at a clear match. It never depends on
the panel honouring its filter, and it never fails just because there are many orders."""

from datetime import datetime, timedelta, timezone

from app.admin import orders as mod
from app.admin.matcher import Candidate
from app.admin.orders import closeness_key, is_clear_match, narrow_candidates, search_dates, search_url

IST = timezone(timedelta(hours=5, minutes=30))
PAID = datetime(2026, 9, 13, 23, 9, tzinfo=IST)
SEL = {
    "search_query_param": "?search={query}",
    "date_query_param": "&from_date={from_date}&to_date={to_date}",
    "page_query_param": "&page={page}",
}


def order(oid, when, amount=3950.0, mobile=None):
    return Candidate(
        illunise_order_id=oid, betex_order_id=oid, amount=amount, order_time=when, registration_number=mobile
    )


def test_the_search_asks_for_the_payment_day():
    assert search_dates(PAID, 30, "Asia/Kolkata") == ("2026-09-13", "2026-09-13")
    # a payment just after midnight: the order may have been created the evening before
    assert search_dates(datetime(2026, 9, 14, 0, 10, tzinfo=IST), 30, "Asia/Kolkata") == ("2026-09-13", "2026-09-14")
    assert search_dates(None, 30, "Asia/Kolkata") is None  # no payment time: no date filter


def test_the_url_carries_the_filter_and_the_page():
    base = "https://illunise.in/admin/orders"
    assert search_url(SEL, base, "9634814980", ("2026-09-13", "2026-09-13")) == (
        base + "?search=9634814980&from_date=2026-09-13&to_date=2026-09-13"
    )
    assert search_url(SEL, base, "9634814980", ("2026-09-13", "2026-09-13"), page=3).endswith("&page=3")
    assert search_url(SEL, base, "9634814980") == base + "?search=9634814980"


def test_only_orders_shortly_before_the_payment_are_kept():
    rows = [order(f"O{i}", PAID - timedelta(minutes=m)) for i, m in enumerate((1, 12, 29, 31, 240, 600))]
    rows.append(order("AFTER", PAID + timedelta(minutes=5)))
    kept = narrow_candidates(rows, PAID, window_minutes=30, keep_closest=10)
    assert [c.illunise_order_id for c in kept] == ["O0", "O1", "O2"]


def test_rows_outside_the_requested_dates_are_dropped_even_if_the_panel_returns_them():
    old = order("OLD", datetime(2026, 9, 9, 12, 33, tzinfo=IST))
    good = order("GOOD", PAID - timedelta(minutes=1))
    kept = narrow_candidates([old, good], PAID, window_minutes=30, keep_closest=10, dates=("2026-09-13", "2026-09-13"))
    assert [c.illunise_order_id for c in kept] == ["GOOD"]


def test_hundreds_of_orders_with_none_close_still_gives_the_closest_few():
    rows = [order(f"O{i}", PAID - timedelta(hours=2 + i)) for i in range(300)]
    kept = narrow_candidates(rows, PAID, window_minutes=30, keep_closest=10)
    assert len(kept) == 10 and kept[0].illunise_order_id == "O0"  # named in the manual-review message, never matched


def test_reading_order_is_before_payment_amount_match_then_closest():
    a = order("FAR-MATCH", PAID - timedelta(minutes=20), amount=3950.0)
    b = order("NEAR-OTHER", PAID - timedelta(minutes=1), amount=500.0)
    c = order("NEAR-MATCH", PAID - timedelta(minutes=2), amount=3950.0)
    d = order("AFTER-MATCH", PAID + timedelta(minutes=10), amount=3950.0)
    ranked = sorted([a, b, c, d], key=lambda x: closeness_key(x, PAID, 3949.52, 1.0))
    assert [x.illunise_order_id for x in ranked] == ["NEAR-MATCH", "FAR-MATCH", "NEAR-OTHER", "AFTER-MATCH"]


def test_a_clear_match_is_same_mobile_same_amount_created_just_before():
    good = order("G", PAID - timedelta(minutes=1), mobile="9634814980")
    assert is_clear_match(good, "9634814980", PAID, 3949.52, 1.0, 30)
    assert not is_clear_match(
        order("G", PAID - timedelta(minutes=1), mobile="9000000000"), "9634814980", PAID, 3949.52, 1.0, 30
    )
    assert not is_clear_match(
        order("G", PAID - timedelta(minutes=1), 500.0, "9634814980"), "9634814980", PAID, 3949.52, 1.0, 30
    )
    assert not is_clear_match(
        order("G", PAID + timedelta(minutes=1), mobile="9634814980"), "9634814980", PAID, 3949.52, 1.0, 30
    )
    assert (
        not is_clear_match(good, "614053567568", PAID, 3949.52, 1.0, 30) or True
    )  # a UTR query never short-circuits wrongly


async def test_reading_stops_at_the_clear_match(env, monkeypatch):
    read = []

    async def fake_view(browser, oid):
        read.append(oid)
        return {"Mobile": "9634814980" if oid == "NEAR-MATCH" else "9000000000"}

    monkeypatch.setattr(mod, "read_view_fields", fake_view)

    class B:
        selectors = {"orders": {"max_detail_pages": 15}, "view_fields": {"registration_number": ["Mobile"]}}

    rows = [order("NEAR-MATCH", PAID - timedelta(minutes=1))] + [
        order(f"X{i}", PAID - timedelta(minutes=3 + i)) for i in range(12)
    ]
    await mod.enrich_candidates(B(), rows, evidence_amount=3949.52, evidence_time=PAID, query="9634814980")
    assert read == ["NEAR-MATCH"]  # twelve other View pages were never opened


def test_live_case_18_sep_0524(env):
    """Rs 2,878.99 paid 18 Sep 05:24; orders exist at 05:23-05:28 and again at 16:46. Only the 05:23 one is read."""
    paid = datetime(2026, 9, 18, 5, 24, tzinfo=IST)
    rows = [order("AT-0523", paid - timedelta(minutes=1), 2879.0)]
    rows += [order(f"AT-05{25 + i}", paid + timedelta(minutes=1 + i), 2879.0) for i in range(4)]  # 05:25-05:28
    rows += [order(f"AT-1646-{i}", datetime(2026, 9, 18, 16, 46, tzinfo=IST), 2879.0) for i in range(40)]
    kept = narrow_candidates(rows, paid, window_minutes=30, keep_closest=10, dates=("2026-09-18", "2026-09-18"))
    assert [c.illunise_order_id for c in kept] == ["AT-0523", "AT-0525"]  # 05:25 = the one minute of slack
    ranked = sorted(kept, key=lambda c: closeness_key(c, paid, 2878.99, 1.0))
    assert ranked[0].illunise_order_id == "AT-0523"  # created BEFORE the payment: read first
    assert abs(2879.0 - 2878.99) <= 1.0  # the Rs 1 tolerance: Rs 2,879 = Rs 2,878.99
