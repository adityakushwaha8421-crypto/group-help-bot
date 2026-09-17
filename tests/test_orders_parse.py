import re

from app.admin.orders import build_column_index, row_to_candidate

COLS = {
    "illunise_order_id": ["order id", "id"],
    "betex_order_id": ["merchant order no", "txn id"],
    "registration_number": ["registration no", "mobile"],
    "amount": ["amount"],
    "status": ["status"],
    "created_at": ["created at", "date"],
    "utr": ["utr"],
}
BX = re.compile(r"\bILLUN-\d{8,20}\b", re.I)


def test_header_mapping_is_case_and_punctuation_insensitive():
    idx = build_column_index(["#", "Order_ID", "Registration No.", "Amount", "Status", "Created At", "Txn ID"], COLS)
    assert idx["illunise_order_id"] == 1 and idx["registration_number"] == 2 and idx["betex_order_id"] == 6


def test_row_to_candidate_finds_betex_id_anywhere():
    headers = ["Order ID", "Registration No", "Amount", "Status", "Created At", "Reference"]
    cells = ["55", "REG123456", "₹6,499.92", "Pending", "10-09-2026 19:31", "pg ref ILLUN-178621243657290"]
    c = row_to_candidate(headers, cells, COLS, BX, "Asia/Kolkata")
    assert c.illunise_order_id == "55" and c.betex_order_id == "ILLUN-178621243657290"
    assert c.amount == 6499.92 and c.registration_number == "REG123456"
    assert c.order_time is not None and c.order_time.tzinfo is not None
    assert c.raw["Reference"].startswith("pg ref")
