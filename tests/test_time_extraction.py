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
