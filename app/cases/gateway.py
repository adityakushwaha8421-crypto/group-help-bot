"""BETIX or NATIVE: the operator's one-tap answer that decides a payment case's route.

Asked on the case card as soon as the payment screenshot and the mobile are in; NOTHING is processed until it is
answered. Betix (the default when the question is off): the full flow - statement and video waited for, the
Betix group, /pi, follow-ups. Native: nothing else is needed, the order amount is a round figure, and the
screenshot goes to the Native group captioned with every order id created under the mobile at the payment time."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.correlation import hard_missing
from app.config import get_settings
from app.db.models import GATEWAY_BETIX, GATEWAY_NATIVE, KIND_WITHDRAWAL, Case
from app.db.repository import audit

CHOICES = (GATEWAY_BETIX, GATEWAY_NATIVE)
LABELS = {GATEWAY_BETIX: "Betix", GATEWAY_NATIVE: "Native"}


def is_native(case: Case) -> bool:
    return case.kind != KIND_WITHDRAWAL and case.gateway_choice == GATEWAY_NATIVE


def gateway_label(case: Case) -> str:
    return LABELS.get(case.gateway_choice or "", "Betix")


def is_round(amount: float | None) -> bool:
    """A round figure: whole rupees, no paise (₹10,000.00 yes; ₹100.20 and ₹6,499.92 no)."""
    return amount is not None and abs(float(amount) - round(float(amount))) < 0.005


def auto_gateway(case: Case) -> str | None:
    """The gateway the paid amount decides by itself: paise (₹100.20) is a Betix payment - Betix pads every
    order with unique paise, a Native order is a round figure. A round figure decides nothing (either gateway)."""
    if case.kind == KIND_WITHDRAWAL or case.amount is None:
        return None
    return None if is_round(case.amount) else GATEWAY_BETIX


def gateway_pending(case: Case, missing: list[str]) -> bool:
    """The operator still has to answer: a payment case with the screenshot and the mobile in, no choice yet,
    and the amount not deciding it (unknown yet, or a round figure) - while ASK_GATEWAY is on. A withdrawal is
    never asked."""
    return (
        get_settings().ask_gateway
        and case.kind != KIND_WITHDRAWAL
        and case.gateway_choice is None
        and not hard_missing(missing)
        and auto_gateway(case) is None
    )


async def apply_auto_gateway(session: AsyncSession, case: Case) -> str | None:
    """Let the amount decide when it can: a paid amount with paise makes the case a Betix case without asking.
    Returns the choice it just made, None when nothing was decided (already chosen, amount unknown or round)."""
    if case.gateway_choice is not None:
        return None
    choice = auto_gateway(case)
    if choice is None:
        return None
    case.gateway_choice = choice
    await audit(
        session,
        "GATEWAY_AUTO",
        case_id=case.case_id,
        result=choice,
        details={"amount": case.amount, "why": "the paid amount is not a round figure"},
    )
    return choice


def auto_reason(case: Case) -> str | None:
    """Why the gateway was decided without asking, for the card - None when the operator chose it."""
    if case.gateway_choice == GATEWAY_BETIX and auto_gateway(case) == GATEWAY_BETIX:
        return f"₹{case.amount:,.2f} is not a round figure"
    return None


async def choose_gateway(session: AsyncSession, case: Case, choice: str, actor: str) -> bool:
    """Record the answer. False when the case already has one (a second tap changes nothing)."""
    if choice not in CHOICES:
        raise ValueError(f"unknown gateway {choice!r}")
    if case.gateway_choice is not None:
        return False
    case.gateway_choice = choice
    await audit(session, "GATEWAY_CHOSEN", case_id=case.case_id, actor=actor, result=choice)
    return True
