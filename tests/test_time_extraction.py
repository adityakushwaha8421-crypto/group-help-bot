"""The payment screenshot's own date/time is preserved - even when the app prints no year - and a bank statement
can never overwrite it with another transaction's row.

Live 2026-09-15: Paytm printed "13 Sept, 11:09 PM"; the model returned payment_time=null (no year), so the
statement's "Sept 14, 08:30 am" (a Rs 140 row) became the payment time and the real order, created one minute
before the payment, was rejected as "far too long before"."""

from datetime import datetime, timezone

from app.ai.extractor import extraction_from_ai, parse_datetime_noyear
from app.cases import manager
from app.db.repository import get_case
from tests.test_flow import GOOD, submit_four_messages

IST = timezone.utc  # values below are compared in UTC
NOW = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)  # 15 Sep 2026 10:30 IST


def test_year_less_timestamps_get_the_current_year():
    assert parse_datetime_noyear("13 Sept, 11:09 PM", NOW) == datetime(2026, 9, 13, 17, 39, tzinfo=timezone.utc)
    assert parse_datetime_noyear("Sep 13 at 11:09 pm", NOW) == datetime(2026, 9, 13, 17, 39, tzinfo=timezone.utc)
    assert parse_datetime_noyear("13 Sep, 23:09", NOW) == datetime(2026, 9, 13, 17, 39, tzinfo=timezone.utc)
    assert parse_datetime_noyear("2 Jan, 12:05 AM", NOW) == datetime(2026, 1, 1, 18, 35, tzinfo=timezone.utc)


def test_a_future_date_means_last_year():
    # on 15 Sep 2026, "20 Sept, 9:00 PM" cannot be this year's
    assert parse_datetime_noyear("20 Sept, 9:00 PM", NOW).year == 2025


def test_text_with_a_year_or_without_a_time_is_left_alone():
    assert parse_datetime_noyear("13 Sept 2026, 11:09 PM", NOW) is None  # parse_datetime_loose handles these
    assert parse_datetime_noyear("13 Sept", NOW) is None
    assert parse_datetime_noyear("", NOW) is None


def test_the_models_null_with_quoted_text_still_yields_the_time():
    payload = {  # exactly what the model returned live
        "amount": {"value": 3949.52, "confidence": 0.99},
        "payment_time": {"value": None, "confidence": 0, "evidence_text": "13 Sept, 11:09 PM"},
        "utr": {"value": "614053567568", "confidence": 0.99},
    }
    e = extraction_from_ai(payload, "payment_screenshot")
    assert e.payment_time.value is not None
    assert e.payment_time.value.astimezone(timezone.utc).strftime("%m-%d %H:%M") == "09-13 17:39"
    assert e.payment_time.confidence >= 0.8 and e.payment_time.source == "payment_screenshot"


async def test_a_statements_other_row_never_overrides_the_screenshot(db, fake_bot, fake_ai, order_search, no_download):
    fake_ai.by_type = {
        "payment_screenshot": {
            "amount": {"value": 3949.52, "confidence": 0.99},
            "payment_time": {"value": None, "confidence": 0, "evidence_text": "13 Sept, 11:09 PM"},
            "utr": {"value": "614053567568", "confidence": 0.99},
        },
        "payment_video": {
            "amount": {"value": 3949.52, "confidence": 0.99},
            "payment_time": {"value": "2026-09-13 23:09:00", "confidence": 0.99},
        },
        "bank_statement": {  # the model picked a different transaction on the statement
            "amount": {"value": 140.0, "confidence": 0.99},
            "payment_time": {"value": "2026-09-14 08:30:00", "confidence": 0.99},
            "utr": {"value": "999999999999", "confidence": 0.99},
        },
    }
    order = {
        **GOOD[0],
        "amount": 3950.0,
        "order_time": "2026-09-13 23:08:00",
        "status": "Expired",
        "utr": "614053567568",
        "registration_number": "7733931348",
    }
    order_search([order])
    case_id = await submit_four_messages(db)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        c = await get_case(s, case_id)
        assert c.payment_time.astimezone(timezone.utc).strftime("%m-%d %H:%M") == "09-13 17:39"
        assert c.amount == 3949.52 and c.utr == "614053567568"
        assert c.betex_pay_order_id == order["betex_order_id"]


# ------------------------------------------------------------------ the YEAR is never the model's guess
def test_a_guessed_year_is_replaced_by_the_real_one():
    """Live 2026-09-20 (Rs 199 at 11:34): the screenshot prints "20 Sep, 11:34 AM" - no year. A model that is not
    told today's date writes 2025; Illunise was then searched on 20 Sep 2025 and "no order" was found, by mobile
    and by amount alike, while the panel showed four Rs 200 orders at 11:33-11:34."""
    from datetime import datetime, timezone

    from app.ai.extractor import extraction_from_ai, fix_guessed_year

    payload = {
        "payment_time": {"value": "2025-09-20 11:34:00", "confidence": 0.95, "evidence_text": "20 Sep, 11:34 AM"}
    }
    got = extraction_from_ai(payload, "payment_screenshot").payment_time.value
    assert got.year == datetime.now(timezone.utc).year and (got.month, got.day) == (9, 20)

    now = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)
    guessed = datetime(2025, 9, 20, 6, 4, tzinfo=timezone.utc)
    # the printed text has the time BEFORE the date (our pattern does not read it): the year is still corrected
    assert fix_guessed_year(guessed, "11:34 AM on 20 Sep", now).year == 2026
    assert fix_guessed_year(guessed, "20 Sep 2025, 11:34 AM", now).year == 2025  # a PRINTED year is kept
    late = datetime(2025, 12, 31, 18, 0, tzinfo=timezone.utc)
    assert fix_guessed_year(late, "31 Dec, 11:30 PM", datetime(2026, 1, 2, tzinfo=timezone.utc)).year == 2025
