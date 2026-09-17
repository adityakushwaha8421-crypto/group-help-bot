from app.ai.extractor import Extraction, extract_from_text, extract_registration, extraction_from_ai
from app.config import get_settings


def test_registration_plain_and_prefixed():
    p = get_settings().registration_pattern
    assert extract_registration("REG123456", p) == "REG123456"
    assert extract_registration("Registration no: REG-9981", p) == "REG-9981"
    assert (
        extract_registration("reg # 7788990011", p) == "7788990011"
    )  # raw regex; extract_from_text routes it to mobile
    assert extract_registration("hello team please check", p) is None
    assert extract_registration("/start", p) is None
    assert extract_registration("done", p) is None


def test_text_extraction_fields():
    s = get_settings()
    e = extract_from_text(
        "Paid ₹6,499.92 on 10/09/2026 19:32 UTR 611532946151 to merchant@ptsbi Password:- abc123",
        registration_pattern=s.registration_pattern,
        betex_pattern=s.betex_order_id_pattern,
    )
    assert e.amount.value == 6499.92
    assert e.utr.value == "611532946151"
    assert e.upi_id.value == "merchant@ptsbi"
    assert e.statement_password.value == "abc123"
    assert e.payment_time.value is not None and e.payment_time.value.year == 2026
    assert e.registration_number.value is None


def test_betex_order_id_and_bare_utr():
    s = get_settings()
    e = extract_from_text(
        "ILLUN-178621243657290", registration_pattern=s.registration_pattern, betex_pattern=s.betex_order_id_pattern
    )
    assert e.betex_order_id.value == "ILLUN-178621243657290"
    assert e.registration_number.value is None  # an order id is not a registration number
    e2 = extract_from_text(
        "612172440900", registration_pattern=s.registration_pattern, betex_pattern=s.betex_order_id_pattern
    )
    assert e2.utr.value == "612172440900"


def test_ai_payload_conversion_and_merge():
    ai = extraction_from_ai(
        {
            "amount": {"value": "₹1,208", "confidence": 0.9},
            "payment_time": {"value": "03 Aug 2026, 11:56 PM", "confidence": 0.8},
            "utr": {"value": None, "confidence": 0},
        },
        "payment_screenshot",
    )
    assert ai.amount.value == 1208.0
    assert ai.payment_time.value.hour == 18  # 23:56 IST -> 18:26 UTC
    assert ai.utr.value is None
    other = Extraction()
    other.amount.value, other.amount.confidence, other.amount.source = 1200.0, 0.5, "video"
    merged = ai.merge(other)
    assert merged.amount.value == 1208.0 and merged.amount.source == "payment_screenshot"
    rt = Extraction.from_dict(merged.as_dict())
    assert rt.amount.value == 1208.0 and rt.payment_time.value == ai.payment_time.value
