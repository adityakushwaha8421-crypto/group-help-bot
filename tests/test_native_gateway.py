"""BETIX or NATIVE: never asked - read from the Illunise order.

The orders created under the customer's mobile just before the payment carry the gateway. A "BetixPay" order makes
it a Betix case (the Betix group, /pi, reminders). A Native order ("BETIXPAYNATIVE" in the panel) makes it a
Native case: the screenshot goes to the Native group captioned with EVERY such order id (one or several, closest
to the payment first), nothing else is needed, and a round-figure payment does not wait for a statement / video."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.cases import manager
from app.cases.gateway import is_native_gateway, is_round
from app.config import get_settings, reset_settings_cache
from app.db.models import GATEWAY_BETIX, GATEWAY_NATIVE, AuditLog, CaseStatus, Followup
from app.db.repository import get_case, list_evidence
from app.telegram import input_bot
from app.telegram.betix_monitor import handle_group_message, is_betix_chat, set_membership_checker
from app.telegram.progress import card_buttons, progress_card
from app.workers import tasks
from tests.conftest import make_group_msg, make_input
from tests.test_flow import GOOD, MOBILE, submit_four_messages

NATIVE_CHAT, BETIX_CHAT = -1004444, -1009999
A, B, C = "ILLUN-178621243657290", "ILLUN-178621243657291", "ILLUN-178621243657292"
# The fake AI's screenshot: ₹6,499.92 paid at 19:32 (paise: how a Betix order is paid). ROUND = ₹6,500 paid.
NATIVE_ONE = [{**GOOD[0], "amount": 6500.0, "gateway": "BETIXPAYNATIVE", "order_time": "2026-09-10 19:30:00"}]
NATIVE_TWO = [
    {**GOOD[0], "amount": 6500.0, "gateway": "BETIXPAYNATIVE", "order_time": "2026-09-10 19:28:00"},
    {**GOOD[0], "illunise_order_id": "1003", "betex_order_id": B, "amount": 6500.0, "gateway": "BETIXPAYNATIVE",
     "order_time": "2026-09-10 19:31:00"},
]  # fmt: skip
BETIX_SAME_TIME = {**GOOD[0], "illunise_order_id": "1004", "betex_order_id": C, "amount": 6500.0,
                   "gateway": "BetixPay", "order_time": "2026-09-10 19:31:00"}  # fmt: skip


@pytest.fixture(autouse=True)
def native_group(env, monkeypatch):
    """The Native group is configured (tests elsewhere run without one: every case is a Betix case there)."""
    monkeypatch.setenv("NATIVE_GROUP_CHAT_ID", str(NATIVE_CHAT))
    reset_settings_cache()
    yield
    reset_settings_cache()


def paid(fake_ai, amount: float) -> None:
    fake_ai.screenshot = {**fake_ai.screenshot, "amount": {"value": amount, "confidence": 0.97}}


class Chat:
    def __init__(self, sink, inp):
        self.chat = SimpleNamespace(id=inp.chat_id, type="private")
        self.message_id = inp.message_id
        self._sink, self._n = sink, 900

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self._n += 1
        self._sink.append((text, reply_markup))
        return SimpleNamespace(message_id=self._n)


@pytest.fixture
def jobs(monkeypatch, fake_poster):
    calls = []

    async def fake_enqueue(name, *args, job_id=None, defer_seconds=0):
        calls.append((name, args, defer_seconds))

    monkeypatch.setattr(input_bot, "enqueue", fake_enqueue)
    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    manager.set_poster_factory(lambda: fake_poster)
    input_bot._held.clear()
    input_bot._flush_tasks.clear()
    yield calls
    for t in input_bot._flush_tasks.values():
        t.cancel()
    input_bot._held.clear()
    input_bot._flush_tasks.clear()
    manager.set_poster_factory(None)


async def send(sink, *inputs):
    for i in inputs:
        input_bot.hold(Chat(sink, i), i)
    input_bot._flush_tasks[111].cancel()
    return (await input_bot.flush_chat(111))[0]


async def two_items(db, base: int = 0) -> str:
    """The payment screenshot and the mobile - all a Native case ever has."""
    from app.cases.correlation import attach_message

    async with db.session_scope() as s:
        r = await attach_message(s, make_input(base + 1, "photo"))
        await attach_message(s, make_input(base + 2, "text", MOBILE))
        r.case.progress_chat_id, r.case.progress_message_id = 111, 4242
        return r.case.case_id


async def run(db, order_search, cands, base: int = 0):
    """Screenshot + mobile, the FORCE SEND run (what happens after the wait, or at once for a round figure)."""
    case_id = await two_items(db, base)
    order_search(cands)
    async with db.session_scope() as s:
        outcome = await manager.process_case(s, case_id, force=True, force_send=True)
    return case_id, outcome


# ---------------------------------------------------------------- settings
def test_the_native_group_id_is_accepted_with_or_without_its_sign(monkeypatch):
    for given, want in (("-1003515687583", -1003515687583), ("1003515687583", -1003515687583),
                        ("3515687583", -1003515687583), ("", None), ("x", None)):  # fmt: skip
        monkeypatch.setenv("NATIVE_GROUP_CHAT_ID", given)
        reset_settings_cache()
        assert get_settings().native_chat == want, given


def test_the_native_group_is_monitored_like_a_betix_group():
    assert is_betix_chat(NATIVE_CHAT) and is_betix_chat(BETIX_CHAT) and not is_betix_chat(-1005555)


def test_the_panels_gateway_name_says_native_or_betix():
    assert is_native_gateway("BETIXPAYNATIVE") and is_native_gateway("BetixPay Native")
    assert not is_native_gateway("BetixPay") and not is_native_gateway(None) and not is_native_gateway("")


def test_a_round_figure_has_no_paise():
    assert is_round(10000.0) and is_round(100) and is_round(99.999)
    assert not is_round(100.2) and not is_round(6499.92) and not is_round(None)


# ---------------------------------------------------------------- nothing is asked
async def test_the_card_asks_nothing_and_the_case_starts_by_itself(db, fake_bot, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    text, markup = sink[-1]
    assert markup is None and "Native" not in text and "gateway" not in text.lower()
    assert "Waiting 30s for the bank statement and payment video" in text
    # the usual wait for the statement / video, and the amount is read meanwhile
    assert jobs == [
        ("process_case_job", (case_id, 1, True, True), 30),
        ("amount_job", (case_id, 1), 0),
    ]
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.gateway_choice is None and card_buttons(c) is None


async def test_a_complete_case_is_processed_at_once_as_always(db, fake_bot, jobs):
    sink = []
    case_id = await send(
        sink, make_input(1, "photo"), make_input(2, "text", MOBILE), make_input(3, "document"), make_input(4, "video")
    )
    assert jobs == [("process_case_job", (case_id, 1, True), 0)]


async def test_without_a_native_group_no_amount_is_read_early(db, fake_bot, jobs, monkeypatch):
    monkeypatch.setenv("NATIVE_GROUP_CHAT_ID", "")
    reset_settings_cache()
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    assert jobs == [("process_case_job", (case_id, 1, True, True), 30)]


async def test_an_old_cards_button_says_there_is_nothing_to_choose(db, fake_bot):
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    query = SimpleNamespace(
        data="gw:native:CASE-1",
        from_user=SimpleNamespace(id=111, username="me"),
        message=SimpleNamespace(chat=SimpleNamespace(id=111), message_id=901),
        answer=answer,
    )
    await input_bot.on_gateway_button(query)
    assert "No need to choose" in answers[0]
    assert fake_bot.markup_edits == [(111, 901, None)]  # the buttons are taken away


# ---------------------------------------------------------------- a round figure does not wait
async def test_a_round_figure_is_processed_without_the_wait(db, fake_bot, fake_ai, no_download, jobs):
    paid(fake_ai, 10000.0)
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    assert await tasks.amount_job({}, case_id, 1) == "now"
    assert jobs[-1] == ("process_case_job", (case_id, 2, True, True), 0)  # now, and the 30 s job (v1) is stale
    assert await tasks.process_case_job({}, case_id, 1, True, True) == "stale"
    assert fake_ai.calls == 1


async def test_paise_keep_the_wait_for_the_statement_and_video(db, fake_bot, fake_ai, no_download, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    n = len(jobs)
    assert await tasks.amount_job({}, case_id, 1) == "wait"  # ₹6,499.92
    assert len(jobs) == n
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.amount == 6499.92 and c.processing_version == 1 and c.gateway_choice is None


async def test_an_unreadable_amount_keeps_the_wait(db, fake_bot, fake_ai, no_download, jobs):
    fake_ai.screenshot = {}
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    n = len(jobs)
    assert await tasks.amount_job({}, case_id, 1) == "wait" and len(jobs) == n


async def test_a_newer_file_makes_the_older_amount_job_stand_down(db, fake_bot, fake_ai, no_download, jobs):
    paid(fake_ai, 10000.0)
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    await send(sink, make_input(3, "document"))  # the statement arrives: version 2, its own jobs
    assert await tasks.amount_job({}, case_id, 1) == "gone"
    assert await tasks.amount_job({}, case_id, 2) == "now"


# ---------------------------------------------------------------- the panel decides the group
async def test_a_native_order_sends_the_case_to_the_native_group(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    paid(fake_ai, 6500.0)
    case_id, outcome = await run(db, order_search, NATIVE_ONE)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.gateway_choice == GATEWAY_NATIVE and c.gateway == "BETIXPAYNATIVE"
        assert c.native_order_ids == [A] and c.betex_pay_order_id == A and c.order_amount == 6500.0
        rows = (await s.execute(select(AuditLog).where(AuditLog.action == "GATEWAY_DETECTED"))).scalars().all()
        assert len(rows) == 1 and rows[0].result == "native" and rows[0].details["gateway"] == "BETIXPAYNATIVE"
        assert "ORDER MATCHED" in progress_card(c) and "Native group" in progress_card(c)
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value and c.betix_chat_id == NATIVE_CHAT
        assert (await s.execute(select(Followup))).scalars().all() == []  # no "Any update?" in the Native group
        assert "SENT TO NATIVE GROUP" in progress_card(c)
        from app.cases.correlation import missing_items

        assert missing_items(c, await list_evidence(s, case_id)) == []  # nothing else is ever needed
    assert fake_poster.chats == [NATIVE_CHAT]
    assert fake_poster.media == [("payment_screenshot", A)]  # only the screenshot, captioned with the order id
    assert fake_poster.texts == []  # no amount note, no /pi, no statement, no video


async def test_a_betix_order_sends_the_case_to_the_betix_group(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id = await submit_four_messages(db)
    holder = order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        c = await get_case(s, case_id)
        assert c.gateway_choice == GATEWAY_BETIX and c.gateway == "BetixPay" and c.native_order_ids is None
        assert "Sending the evidence to the Betix group" in progress_card(c)
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.betix_chat_id == BETIX_CHAT and "SENT TO BETIX" in progress_card(c)
        assert len((await s.execute(select(Followup))).scalars().all()) > 0
    assert set(fake_poster.chats) == {BETIX_CHAT}
    assert holder["kw"] == {}  # paise: the usual search, no "read every View page"


async def test_several_native_orders_all_go_in_the_caption_closest_first(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    paid(fake_ai, 6500.0)
    case_id, outcome = await run(db, order_search, NATIVE_TWO)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.native_order_ids == [B, A]  # B was created 1 min before the payment, A 4 min
        assert "ORDERS FOUND" in progress_card(c) and B in progress_card(c) and A in progress_card(c)
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media == [("payment_screenshot", f"{B}\n{A}")]  # ONE post for the payment


async def test_a_round_figure_search_reads_every_view_page(db, fake_bot, fake_ai, order_search, no_download):
    paid(fake_ai, 6500.0)
    case_id = await two_items(db)
    holder = order_search(NATIVE_TWO)
    async with db.session_scope() as s:
        await manager.process_case(s, case_id, force=True, force_send=True)
    assert holder["kw"] == {"read_all": True}  # no "clear match, stop": several orders may share the payment


async def test_round_figure_with_both_gateways_at_the_same_time_is_native(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """₹6,500 paid exactly; a Native order AND a Betix order of ₹6,500 were created just before. A round figure
    is how a Native order is paid: Native, and the Betix order is not sent anywhere."""
    paid(fake_ai, 6500.0)
    case_id, outcome = await run(db, order_search, [NATIVE_ONE[0], BETIX_SAME_TIME])
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.gateway_choice == GATEWAY_NATIVE and c.native_order_ids == [A]


async def test_paise_with_both_gateways_at_the_same_time_is_betix(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """₹6,499.92 paid (padded paise: a Betix payment); a Betix order answers for it: Betix, whatever Native order
    sits next to it."""
    case_id, outcome = await run(db, order_search, [NATIVE_ONE[0], BETIX_SAME_TIME])
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.gateway_choice == GATEWAY_BETIX and c.betex_pay_order_id == C and c.native_order_ids is None
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert set(fake_poster.chats) == {BETIX_CHAT}


async def test_only_native_orders_fit_so_it_is_native_even_with_paise(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """₹6,499.92 paid, the only order near the payment is a Native one of ₹6,500: the panel says Native."""
    case_id, outcome = await run(db, order_search, NATIVE_ONE)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).gateway_choice == GATEWAY_NATIVE


async def test_native_orders_that_do_not_fit_never_count(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    paid(fake_ai, 6500.0)
    cands = [
        {**NATIVE_TWO[0]},  # ₹6,500 Native, 4 min before: fits
        {**NATIVE_TWO[1], "amount": 6000.0},  # another amount: no
        {**NATIVE_ONE[0], "betex_order_id": "ILLUN-178621243657293", "illunise_order_id": "1005",
         "order_time": "2026-09-10 19:40:00"},  # created AFTER the payment: no
        {**NATIVE_ONE[0], "betex_order_id": "ILLUN-178621243657294", "illunise_order_id": "1006",
         "registration_number": "9000000001"},  # another customer's: no
        {**BETIX_SAME_TIME},  # a Betix order: never in a Native caption
    ]  # fmt: skip
    case_id, outcome = await run(db, order_search, cands)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A]


async def test_a_betix_case_never_selects_a_native_order(db, fake_bot, fake_ai, order_search, no_download):
    """The panel names the Native gateway BETIXPAYNATIVE, which CONTAINS "BetixPay": never scored as Betix."""
    from app.admin.matcher import match_orders
    from app.ai.extractor import Extraction
    from tests.conftest import make_candidates

    cands = make_candidates([{**NATIVE_ONE[0]}, {**NATIVE_ONE[0], "betex_order_id": B, "gateway": "BetixPay"}])
    ex = Extraction.from_dict(
        {
            "amount": {"value": 6500.0, "confidence": 0.9},
            "payment_time": {"value": "2026-09-10 19:32:12", "confidence": 0.9},
        }
    )
    st = get_settings()
    r = match_orders(ex, MOBILE, cands, weights=st.match_weights, threshold=0.9, ambiguity_gap=0.05,
                     time_rule=st.time_rule, gateway_name="BetixPay", other_gateways=("BetixPayNative",))  # fmt: skip
    by_id = {sc.candidate.betex_order_id: sc for sc in r.scored}
    assert by_id[A].signals["gateway"]["score"] == 0.0 and "another gateway" in by_id[A].signals["gateway"]["how"]
    assert by_id[B].signals["gateway"]["score"] == 1.0 and by_id[A].score <= 0.5


async def test_no_order_at_all_is_manual_review_and_nothing_is_posted(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    paid(fake_ai, 6500.0)
    case_id, outcome = await run(db, order_search, [{**NATIVE_ONE[0], "amount": 6000.0}])
    assert outcome == "escalated"
    assert any("MANUAL REVIEW" in t for _, t in fake_bot.sent)
    assert fake_poster.media == [] and fake_poster.texts == []
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ESCALATED.value and c.gateway_choice is None


async def test_a_native_order_is_never_asked_with_pi(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    paid(fake_ai, 6500.0)
    await run(db, order_search, NATIVE_TWO)
    assert not any(t.startswith("/pi") for t, _ in fake_poster.texts)


async def test_without_a_native_group_a_native_order_is_not_routed(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    monkeypatch.setenv("NATIVE_GROUP_CHAT_ID", "")
    reset_settings_cache()
    paid(fake_ai, 6500.0)
    case_id, outcome = await run(db, order_search, NATIVE_ONE)
    assert outcome != "ready" and fake_poster.media == []
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).gateway_choice is None


async def test_live_case_an_order_created_the_same_minute_as_the_payment_is_taken(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """2026-11-01 11:34: ₹10,000 paid at 11:34, order ILLUN-179351304657290 of ₹10,000 created at 11:34 (Success,
    gateway BETIXPAYNATIVE). It is THE order, and it is a Native one."""
    fake_ai.screenshot = {
        **fake_ai.screenshot,
        "amount": {"value": 10000.0, "confidence": 0.97},
        "payment_time": {"value": "2026-11-01 11:34:20", "confidence": 0.95},
        "utr": {"value": "219439804510", "confidence": 0.96},
    }
    live = [{"illunise_order_id": "ILLUN-179351304657290", "betex_order_id": "ILLUN-179351304657290",
             "registration_number": MOBILE, "amount": 10000.0, "order_time": "2026-11-01 11:34:00",
             "status": "Success", "gateway": "BETIXPAYNATIVE", "utr": "219439804510"}]  # fmt: skip
    case_id, outcome = await run(db, order_search, live)
    # Illunise already shows it Success WITH this payment's UTR: credited, nothing to send - said so, no alert
    assert outcome == "already_success"
    assert not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)
    # the same order NOT yet carrying this UTR: it goes to the Native group
    fake_ai.screenshot["utr"] = {"value": "111111111111", "confidence": 0.96}
    case_id, outcome = await run(db, order_search, [{**live[0], "status": "Pending", "utr": None}], base=20)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.native_order_ids == ["ILLUN-179351304657290"] and c.gateway_choice == GATEWAY_NATIVE
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media[-1] == ("payment_screenshot", "ILLUN-179351304657290")
    assert fake_poster.chats[-1] == NATIVE_CHAT


# ---------------------------------------------------------------- the groups stay apart
async def test_a_members_done_in_the_native_group_confirms_the_case(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    reset_settings_cache()
    paid(fake_ai, 6500.0)

    async def everyone(chat_id, user_id):
        return True

    set_membership_checker(everyone)
    try:
        case_id, _ = await run(db, order_search, NATIVE_ONE)
        async with db.session_scope() as s:
            assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
            r = await handle_group_message(
                s,
                make_group_msg(
                    700, "done ✔️", sender_username="wendy", sender_id=5001, reply_to=501, chat_id=NATIVE_CHAT
                ),
            )
            assert r["action"] == "verified"
            assert (await get_case(s, case_id)).status == CaseStatus.VERIFIED.value
        assert any("PAYMENT CONFIRMED" in t for _, t in fake_bot.sent)
    finally:
        set_membership_checker(None)


async def test_a_bare_done_in_the_native_group_never_confirms_a_betix_case(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    """Proximity ("one case posted recently, an authoritative sender") stays inside the group the case lives in."""
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    reset_settings_cache()

    async def everyone(chat_id, user_id):
        return True

    set_membership_checker(everyone)
    try:
        case_id = await submit_four_messages(db)
        order_search(GOOD)
        async with db.session_scope() as s:
            assert await manager.process_case(s, case_id, force=True) == "ready"
            assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"  # the Betix group
            r = await handle_group_message(
                s, make_group_msg(701, "done", sender_username="wendy", sender_id=5001, chat_id=NATIVE_CHAT)
            )
            assert r["action"] == "unlinked"
            assert (await get_case(s, case_id)).status == CaseStatus.WAITING_FOR_CONFIRMATION.value
            r = await handle_group_message(s, make_group_msg(702, "done", sender_username="wendy", sender_id=5001))
            # the same words in the Betix group DO reach the case (by proximity: weak, so the operator is told)
            assert r["case_id"] == case_id and r["correlation"] == "proximity"
    finally:
        set_membership_checker(None)


async def test_add_on_a_native_case_needs_nothing(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    from app.telegram import add_evidence
    from tests.test_add_evidence import Msg

    paid(fake_ai, 6500.0)
    case_id, _ = await run(db, order_search, NATIVE_ONE)
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
    sink = []
    add_evidence._sessions.clear()
    await input_bot.cmd_add(Msg(make_input(100, "text", chat_id=111, user_id=111), sink), SimpleNamespace(args=MOBILE))
    for t in input_bot._add_timers.values():
        t.cancel()
    input_bot._add_timers.clear()
    add_evidence._sessions.clear()
    assert "already with the Native group" in sink[-1] and "already has everything" in sink[-1]
