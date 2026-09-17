"""When the AI service cannot be used (no credits, outage, bad key) the screenshot was never READ. The bot must
not blame the screenshot ("UTR Not Found"): it holds the case, tells the admins once what is actually wrong, and
retries on its own. Live 2026-09-14: OpenAI returned 429 "You have no credits remaining" and a perfectly readable
Paytm receipt was refused twice."""

import httpx
import openai
import pytest

from app.ai.analyzer import AIUnavailable, unavailable_reason
from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence
from app.telegram.progress import progress_card
from tests.conftest import make_input
from tests.test_flow import GOOD, submit_four_messages


def _err(cls, status, message):
    resp = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return cls(message, response=resp, body=None)


@pytest.mark.parametrize(
    "exc,reason",
    [
        (_err(openai.RateLimitError, 429, "You have no credits remaining. Add credits to continue"), "no credits left"),
        (_err(openai.RateLimitError, 429, "Rate limit reached for gpt"), "rate limited"),
        (_err(openai.AuthenticationError, 401, "Incorrect API key provided"), "key was rejected"),
        (_err(openai.InternalServerError, 500, "The server had an error"), "server error"),
        (openai.APIConnectionError(request=httpx.Request("POST", "https://x")), "could not reach"),
    ],
)
def test_api_outages_are_recognised(exc, reason):
    assert reason in unavailable_reason(exc)


def test_our_own_mistakes_are_not_outages():
    assert unavailable_reason(_err(openai.BadRequestError, 400, "Invalid schema")) is None
    assert unavailable_reason(ValueError("bad json")) is None


@pytest.fixture
def ai_down(fake_ai):
    async def boom(files, *, doc_type_hint, context_hint=""):
        raise AIUnavailable("no credits left on the OpenAI account")

    real = fake_ai.analyze_files
    fake_ai.analyze_files = boom
    yield fake_ai
    fake_ai.analyze_files = real


@pytest.fixture
def retries(monkeypatch):
    calls = []

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        calls.append((name, args, defer_seconds))

    monkeypatch.setattr(manager, "enqueue", fake_enqueue)
    return calls


async def test_an_outage_holds_the_case_instead_of_blaming_the_screenshot(
    db, fake_bot, ai_down, order_search, no_download, fake_poster, retries
):
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ai_unavailable"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_INPUT.value and c.betex_pay_order_id is None
        assert c.failure_reason.startswith(manager.AI_UNAVAILABLE) and "no credits" in c.failure_reason
        card = progress_card(c)
        assert "ON HOLD" in card and "no credits" in card and "retry automatically" in card
    assert not any("UTR Not Found" in t for _, t in fake_bot.sent)  # never the wrong message
    alerts = [t for _, t in fake_bot.sent if "MANUAL REVIEW NEEDED" in t]
    assert len(alerts) == 1 and "AI service is unavailable" in alerts[0] and "no credits" in alerts[0]
    assert fake_poster.media == [] and fake_poster.texts == []  # nothing reached Betix
    # a retry was scheduled, with the same force flag it was processed with
    assert [(n, a[0], a[2], a[3], d) for n, a, d in retries] == [("process_case_job", case_id, True, False, 600)]


async def test_the_retry_continues_the_case_once_the_service_is_back(
    db, fake_bot, ai_down, order_search, no_download, fake_poster, retries
):
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ai_unavailable"
    ai_down.analyze_files = ai_down.__class__.analyze_files.__get__(ai_down)  # credits added
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        c = await get_case(s, case_id)
        assert c.betex_pay_order_id == "ILLUN-178621243657290" and c.utr == "611532946151"
        assert not (c.failure_reason or "").startswith(manager.AI_UNAVAILABLE)


async def test_the_admins_are_told_once_per_case(db, fake_bot, ai_down, order_search, no_download, retries):
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    for _ in range(3):  # three retries while still down
        async with db.session_scope() as s:
            assert await manager.process_case(s, case_id, force=True) == "ai_unavailable"
    assert sum(1 for _, t in fake_bot.sent if "AI service is unavailable" in t) == 1
    assert len(retries) == 3  # but every retry re-arms the next one


async def test_a_resent_screenshot_stays_on_the_held_case(db, fake_bot, ai_down, order_search, no_download, retries):
    case_id = await submit_four_messages(db)
    order_search(GOOD)
    async with db.session_scope() as s:
        await manager.process_case(s, case_id, force=True)
    async with db.session_scope() as s:  # the operator resends the screenshot (different file id): same case
        r = await attach_message(s, make_input(9, "photo"))
        assert r.case.case_id == case_id and not r.created
        shots = [e for e in await list_evidence(s, case_id) if e.type == "payment_screenshot"]
        assert len(shots) == 2


async def test_force_sent_cases_keep_their_force_flag_on_retry(
    db, fake_bot, ai_down, order_search, no_download, retries
):
    async with db.session_scope() as s:
        case_id = (await attach_message(s, make_input(1, "photo"))).case.case_id
        await attach_message(s, make_input(2, "text", "7733931348"))
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True, force_send=True) == "ai_unavailable"
    assert retries[0][1][3] is True  # force_send carried into the retry
