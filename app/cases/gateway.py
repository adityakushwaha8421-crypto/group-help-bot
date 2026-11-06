"""BETIX or NATIVE: read from the Illunise order, never asked.

The order(s) created under the customer's mobile around the payment time say which gateway the customer paid
through: an order of the Betix gateway ("BetixPay") makes it a Betix case - the Betix group, /pi, reminders; an
order of the Native gateway ("BETIXPAYNATIVE" in the panel) makes it a Native case - the screenshot goes to the
Native group once per such order id (one id per message). The answer is kept on the case (Case.gateway_choice)."""

from __future__ import annotations

from app.config import get_settings
from app.db.models import GATEWAY_BETIX, GATEWAY_NATIVE, KIND_WITHDRAWAL, Case

LABELS = {GATEWAY_BETIX: "Betix", GATEWAY_NATIVE: "Native"}


def native_enabled() -> bool:
    """Native routing needs somewhere to send to (NATIVE_GROUP_CHAT_ID) and the gateway's name in the panel."""
    s = get_settings()
    return s.native_chat is not None and bool(s.native_gateway_name.strip())


def is_native_gateway(gateway: str | None) -> bool:
    """Does the panel's gateway name say Native ("BETIXPAYNATIVE", "BetixPay Native")?"""
    name = get_settings().native_gateway_name.lower().replace(" ", "")
    return bool(name) and name in (gateway or "").lower().replace(" ", "")


def is_native(case: Case) -> bool:
    return case.kind != KIND_WITHDRAWAL and case.gateway_choice == GATEWAY_NATIVE


def gateway_label(case: Case) -> str:
    return LABELS.get(case.gateway_choice or "", "Betix")


def is_round(amount: float | None) -> bool:
    """A round figure: whole rupees, no paise (₹10,000.00 yes; ₹100.20 and ₹6,499.92 no). Betix pads every order
    with unique paise, a Native order is paid as a round figure."""
    return amount is not None and abs(float(amount) - round(float(amount))) < 0.005
