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


def gateway_pending(case: Case, missing: list[str]) -> bool:
    """The operator still has to answer: a payment case with the screenshot and the mobile in and no choice yet
    (while ASK_GATEWAY is on). A withdrawal is never asked."""
    return (
        get_settings().ask_gateway
        and case.kind != KIND_WITHDRAWAL
        and case.gateway_choice is None
        and not hard_missing(missing)
    )


async def choose_gateway(session: AsyncSession, case: Case, choice: str, actor: str) -> bool:
    """Record the answer. False when the case already has one (a second tap changes nothing)."""
    if choice not in CHOICES:
        raise ValueError(f"unknown gateway {choice!r}")
    if case.gateway_choice is not None:
        return False
    case.gateway_choice = choice
    await audit(session, "GATEWAY_CHOSEN", case_id=case.case_id, actor=actor, result=choice)
    return True
