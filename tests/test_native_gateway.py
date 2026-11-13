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


async def test_several_native_orders_get_one_post_each_closest_first(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """Two Native orders at the payment time: the screenshot goes out TWICE, each time with ONE order id - never
    two ids in one caption. Both are messages of their own in the Native group."""
    paid(fake_ai, 6500.0)
    case_id, outcome = await run(db, order_search, NATIVE_TWO)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.native_order_ids == [B, A]  # B was created 1 min before the payment, A 4 min
        assert "ORDERS FOUND" in progress_card(c) and B in progress_card(c) and A in progress_card(c)
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.betix_root_message_id == 501  # the first post is the case's anchor
    assert fake_poster.media == [("payment_screenshot", B), ("payment_screenshot", A)]
    assert fake_poster.media_replies == [("payment_screenshot", None), ("payment_screenshot", None)]
    assert fake_poster.chats == [NATIVE_CHAT, NATIVE_CHAT] and fake_poster.texts == []
    assert not any("\n" in (cap or "") for _, cap in fake_poster.media)


async def test_posting_again_never_repeats_an_order(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    paid(fake_ai, 6500.0)
    case_id, _ = await run(db, order_search, NATIVE_TWO)
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
        c = await get_case(s, case_id)
        await fake_poster.post_native(s, c, await list_evidence(s, case_id))  # a retry / restart recovery
    assert fake_poster.media == [("payment_screenshot", B), ("payment_screenshot", A)]


async def test_a_done_on_the_second_orders_post_confirms_the_case(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    reset_settings_cache()
    paid(fake_ai, 6500.0)

    async def everyone(chat_id, user_id):
        return True

    set_membership_checker(everyone)
    try:
        case_id, _ = await run(db, order_search, NATIVE_TWO)
        async with db.session_scope() as s:
            assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
            r = await handle_group_message(  # 502: the second order's own post
                s,
                make_group_msg(710, "done", sender_username="wendy", sender_id=5001, reply_to=502, chat_id=NATIVE_CHAT),
            )
            assert r["action"] == "verified" and r["correlation"] == "reply_chain"
    finally:
        set_membership_checker(None)


async def test_two_screens_of_one_payment_follow_the_first_order_only(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """The receipt and its details page (2 screenshots, 1 payment) with 2 orders: the first order's post carries
    both screens (the second as its reply, as always); the second order gets the screenshot once."""
    from app.cases.correlation import attach_message

    paid(fake_ai, 6500.0)
    case_id = await two_items(db)
    async with db.session_scope() as s:
        await attach_message(s, make_input(3, "photo"))
    order_search(NATIVE_TWO)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True, force_send=True) == "ready"
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert [cap for _, cap in fake_poster.media] == [B, B, A]
    assert [r for _, r in fake_poster.media_replies] == [None, 501, None]


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


# ---------------------------------------------------------------- which Native orders, exactly
LIVE_1107 = [  # 2026-11-07 22:02: ₹10,000 paid; five ₹10,000 Native orders inside the window, one at 22:02
    {"illunise_order_id": "ILLUN-17940691595864", "betex_order_id": "ILLUN-17940691595864", "registration_number": MOBILE,
     "amount": 10000.0, "order_time": "2026-11-07 22:02:00", "status": "Pending", "gateway": "BETIXPAYNATIVE"},
    {"illunise_order_id": "ILLUN-17940682845864", "betex_order_id": "ILLUN-17940682845864", "registration_number": MOBILE,
     "amount": 10000.0, "order_time": "2026-11-07 21:48:00", "status": "Expired", "gateway": "BETIXPAYNATIVE"},
    {"illunise_order_id": "ILLUN-17940682715864", "betex_order_id": "ILLUN-17940682715864", "registration_number": MOBILE,
     "amount": 10000.0, "order_time": "2026-11-07 21:47:00", "status": "Expired", "gateway": "BETIXPAYNATIVE"},
    {"illunise_order_id": "ILLUN-17940682565864", "betex_order_id": "ILLUN-17940682565864", "registration_number": MOBILE,
     "amount": 10000.0, "order_time": "2026-11-07 21:47:00", "status": "Expired", "gateway": "BETIXPAYNATIVE"},
    {"illunise_order_id": "ILLUN-17940682325864", "betex_order_id": "ILLUN-17940682325864", "registration_number": MOBILE,
     "amount": 10000.0, "order_time": "2026-11-07 21:47:00", "status": "Expired", "gateway": "BETIXPAYNATIVE"},
]  # fmt: skip


def paid_live(fake_ai, utr="667778178327"):
    fake_ai.screenshot = {
        **fake_ai.screenshot,
        "amount": {"value": 10000.0, "confidence": 0.97},
        "payment_time": {"value": "2026-11-07 22:02:30", "confidence": 0.95},
        "utr": {"value": utr, "confidence": 0.96},
    }


async def test_live_1107_only_the_order_created_with_the_payment_goes_out(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """Five ₹10,000 Native orders inside the window - four from 21:47, one at 22:02 like the payment. Only the
    22:02 one is the payment's order; the 21:47 ones were not created together with it."""
    paid_live(fake_ai)
    case_id, outcome = await run(db, order_search, LIVE_1107)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == ["ILLUN-17940691595864"]
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media == [("payment_screenshot", "ILLUN-17940691595864")]


async def test_orders_created_together_still_go_out_together(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    paid_live(fake_ai)
    together = [LIVE_1107[0], {**LIVE_1107[1], "order_time": "2026-11-07 22:00:00"}]  # 2 min apart
    case_id, _ = await run(db, order_search, together)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == ["ILLUN-17940691595864", "ILLUN-17940682845864"]


async def test_the_order_holding_this_payments_utr_goes_alone(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """The panel already shows the payment's bank reference on one order (Pending, not yet Success): that is the
    order - the one created together with it is not sent."""
    paid_live(fake_ai)
    cands = [LIVE_1107[0], {**LIVE_1107[1], "order_time": "2026-11-07 22:01:00", "utr": "667778178327"}]
    case_id, outcome = await run(db, order_search, cands)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == ["ILLUN-17940682845864"]


async def test_an_order_success_under_another_utr_never_goes(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    paid_live(fake_ai)
    cands = [
        LIVE_1107[0],
        {**LIVE_1107[1], "order_time": "2026-11-07 22:01:00", "status": "Success", "utr": "111111111111"},
    ]
    case_id, _ = await run(db, order_search, cands)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == ["ILLUN-17940691595864"]


async def test_the_live_order_once_success_with_this_utr_is_already_credited(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """The panel as it looked later: the 22:02 order Success with UTR 667778178327 - nothing to send."""
    paid_live(fake_ai)
    cands = [{**LIVE_1107[0], "status": "Success", "utr": "667778178327"}, *LIVE_1107[1:]]
    _, outcome = await run(db, order_search, cands)
    assert outcome == "already_success" and fake_poster.media == []


# ---------------------------------------------------------------- the 5-minute window and the order's UPI
SHOT_UPI = "ci26102159-4@ypbiz"  # the payee UPI printed on the Amazon Pay screenshot of 2026-11-07


def test_the_upi_is_found_inside_the_creation_response_json():
    from app.admin.orders import upi_from_json

    creation = {"status": "ok", "data": {"orderId": "X1", "payee": {"name": "Cartzuno", "vpa": SHOT_UPI},
                "customer": {"email": "someone@gmail.com"}}}  # fmt: skip
    assert upi_from_json(creation) == SHOT_UPI
    assert upi_from_json({"data": {"intent": "pay to merchant", "upiId": "abc-1@ybl"}}) == "abc-1@ybl"
    assert upi_from_json({"note": "pay merchant-99@okaxis now", "value": "merchant-99@okaxis"}) == "merchant-99@okaxis"
    assert upi_from_json({"message": "ok", "email": "x@gmail.com"}) is None


def test_the_view_page_upi_lands_on_the_candidate():
    from app.admin.matcher import Candidate
    from app.admin.orders import apply_view_fields

    c = apply_view_fields(
        Candidate("1", A), {"gateway.upi": SHOT_UPI, "Status": "Pending"}, {"status": ["Status"]}, "Asia/Kolkata"
    )
    assert c.upi_id == SHOT_UPI and c.status == "Pending"


async def test_a_native_order_older_than_five_minutes_never_counts(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    paid(fake_ai, 6500.0)  # paid 19:32:12
    six_min = [{**NATIVE_ONE[0], "order_time": "2026-09-10 19:26:00"}]
    case_id, outcome = await run(db, order_search, six_min)
    assert outcome == "escalated" and fake_poster.media == []
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).gateway_choice is None
    four_min = [{**NATIVE_ONE[0], "order_time": "2026-09-10 19:28:00"}]
    case_id, outcome = await run(db, order_search, four_min, base=20)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A]


async def test_the_order_paid_to_the_screenshots_upi_is_the_one(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """Two Native orders created together; the panel's creation JSON gives each its UPI. The screenshot was paid to
    B's UPI: B alone goes out, although A was created closer to the payment."""
    paid(fake_ai, 6500.0)
    fake_ai.receiver_upi = SHOT_UPI
    cands = [
        {**NATIVE_ONE[0], "order_time": "2026-09-10 19:31:00", "upi_id": "merchant-77@okhdfc"},
        {**NATIVE_ONE[0], "betex_order_id": B, "illunise_order_id": "1003", "order_time": "2026-09-10 19:30:00",
         "upi_id": SHOT_UPI},
    ]  # fmt: skip
    case_id, outcome = await run(db, order_search, cands)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.native_order_ids == [B]
        rows = (await s.execute(select(AuditLog).where(AuditLog.action == "NATIVE_UPI_CHECK"))).scalars().all()
        assert len(rows) == 1 and rows[0].result == "matched" and rows[0].details["screenshot_upi"] == SHOT_UPI
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media == [("payment_screenshot", B)]
    assert fake_ai.ocr_calls == 1


async def test_a_masked_screenshot_upi_still_picks_the_order(db, fake_bot, fake_ai, order_search, no_download):
    paid(fake_ai, 6500.0)
    fake_ai.receiver_upi = "••••2159-4@ypbiz"
    cands = [
        {**NATIVE_ONE[0], "order_time": "2026-09-10 19:31:00", "upi_id": "merchant-77@okhdfc"},
        {**NATIVE_ONE[0], "betex_order_id": B, "illunise_order_id": "1003", "upi_id": SHOT_UPI},
    ]  # fmt: skip
    case_id, outcome = await run(db, order_search, cands)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [B]


async def test_no_native_order_paid_to_the_screenshots_upi_is_manual_review(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    paid(fake_ai, 6500.0)
    fake_ai.receiver_upi = SHOT_UPI
    cands = [{**NATIVE_ONE[0], "upi_id": "merchant-77@okhdfc"}]
    case_id, outcome = await run(db, order_search, cands)
    assert outcome == "escalated" and fake_poster.media == []
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW" in t][-1]
    assert "none was paid to the UPI on the screenshot" in alert and SHOT_UPI in alert and "merchant-77@okhdfc" in alert
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ESCALATED.value and c.gateway_choice is None


async def test_orders_without_a_upi_in_the_panel_go_by_time_and_no_ocr_runs(
    db, fake_bot, fake_ai, order_search, no_download
):
    paid(fake_ai, 6500.0)
    fake_ai.receiver_upi = SHOT_UPI
    case_id, outcome = await run(db, order_search, NATIVE_ONE)  # no upi_id on the order
    assert outcome == "ready" and fake_ai.ocr_calls == 0
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A]


async def test_a_screenshot_without_a_readable_upi_falls_back_to_the_time_rule(
    db, fake_bot, fake_ai, order_search, no_download
):
    paid(fake_ai, 6500.0)
    fake_ai.receiver_upi = None
    cands = [{**NATIVE_ONE[0], "upi_id": "merchant-77@okhdfc"}]
    case_id, outcome = await run(db, order_search, cands)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.native_order_ids == [A]
        rows = (await s.execute(select(AuditLog).where(AuditLog.action == "NATIVE_UPI_CHECK"))).scalars().all()
        assert rows[0].result == "no screenshot upi"


async def test_the_utr_on_the_order_beats_the_upi_check(db, fake_bot, fake_ai, order_search, no_download):
    paid_live(fake_ai)
    fake_ai.receiver_upi = SHOT_UPI
    cands = [
        {**LIVE_1107[0], "upi_id": "merchant-77@okhdfc", "utr": "667778178327"},
        {**LIVE_1107[1], "order_time": "2026-11-07 22:01:00", "upi_id": SHOT_UPI},
    ]
    case_id, outcome = await run(db, order_search, cands)
    assert outcome == "ready" and fake_ai.ocr_calls == 0
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [LIVE_1107[0]["betex_order_id"]]


# ---------------------------------------------------------------- the payee name (optional, Native only)
SHOT_NAME = "CARTZUNO INNOVATIONS PRIVA..."  # as the Amazon Pay screenshot of 2026-11-07 prints it (cut off)
ORDER_NAME = "Cartzuno Innovations Private Limited"


def named(fake_ai, name=SHOT_NAME):
    fake_ai.screenshot = {**fake_ai.screenshot, "receiver_name": {"value": name, "confidence": 0.9}}


def test_the_payee_name_is_found_in_the_creation_response_json():
    from app.admin.orders import payee_name_from_json, upi_from_json

    link = {"data": {"qr": "upi://pay?pa=ci26102159-4@ypbiz&pn=CARTZUNO%20INNOVATIONS&am=10000.00&cu=INR"}}
    assert payee_name_from_json(link) == "CARTZUNO INNOVATIONS" and upi_from_json(link) == SHOT_UPI
    obj = {"customer": {"name": "Rohit Tiwari", "mobile": "9876543210"}, "payee": {"name": ORDER_NAME, "vpa": SHOT_UPI}}
    assert payee_name_from_json(obj) == ORDER_NAME  # the name beside the UPI, never the customer's
    assert payee_name_from_json({"upiId": SHOT_UPI, "payeeName": ORDER_NAME, "name": "Rohit"}) == ORDER_NAME
    assert payee_name_from_json({"customerName": "Rohit Tiwari", "status": "created"}) is None


def test_names_match_when_cut_off_or_written_with_another_company_form():
    from app.telegram.upi_check import name_match

    assert name_match(SHOT_NAME, ORDER_NAME)[0]
    assert name_match("Cartzuno Innovations Pvt. Ltd.", ORDER_NAME)[0]
    assert name_match("CARTZUNO INNOVATIONS", "cartzuno innovations private limited")[0]
    assert not name_match("CARTZUNO INNOVATIONS", "Zentro Payments Private Limited")[0]
    assert not name_match("CAR", ORDER_NAME)[0]  # too little to go on
    assert not name_match(None, ORDER_NAME)[0] and not name_match(SHOT_NAME, "")[0]


def test_the_view_page_payee_name_lands_on_the_candidate():
    from app.admin.matcher import Candidate
    from app.admin.orders import apply_view_fields

    c = apply_view_fields(
        Candidate("1", A), {"gateway.upi": SHOT_UPI, "gateway.upi_name": ORDER_NAME}, {}, "Asia/Kolkata"
    )
    assert c.upi_id == SHOT_UPI and c.payee_name == ORDER_NAME


TWO_TOGETHER = [  # created together, no UPI shown by the panel: time alone cannot say which
    {**NATIVE_ONE[0], "order_time": "2026-09-10 19:31:00", "payee_name": "Zentro Payments Private Limited"},
    {**NATIVE_ONE[0], "betex_order_id": B, "illunise_order_id": "1003", "order_time": "2026-09-10 19:30:00",
     "payee_name": ORDER_NAME},
]  # fmt: skip


async def test_the_payee_name_picks_the_order_when_several_are_in_question(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    paid(fake_ai, 6500.0)
    named(fake_ai)
    case_id, outcome = await run(db, order_search, TWO_TOGETHER)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [B]  # paid to Cartzuno, although A is closer
        row = (await s.execute(select(AuditLog).where(AuditLog.action == "GATEWAY_DETECTED"))).scalars().one()
        assert row.details["name_check"] == "matched" and "the name on the screenshot" in row.details["why"]
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media == [("payment_screenshot", B)]


async def test_no_name_fits_decides_nothing(db, fake_bot, fake_ai, order_search, no_download):
    """Names are cut off and spelled freely: a name that fits no order throws no order out."""
    paid(fake_ai, 6500.0)
    named(fake_ai, "SOME OTHER SHOP")
    case_id, outcome = await run(db, order_search, TWO_TOGETHER)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A, B]  # the time rule, as without a name
        row = (await s.execute(select(AuditLog).where(AuditLog.action == "GATEWAY_DETECTED"))).scalars().one()
        assert row.details["name_check"] == "no match"


async def test_one_order_needs_no_name_check(db, fake_bot, fake_ai, order_search, no_download):
    paid(fake_ai, 6500.0)
    named(fake_ai, "SOME OTHER SHOP")
    case_id, outcome = await run(db, order_search, [TWO_TOGETHER[0]])
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A]
        row = (await s.execute(select(AuditLog).where(AuditLog.action == "GATEWAY_DETECTED"))).scalars().one()
        assert row.details["name_check"] == ""


async def test_the_upi_decides_before_the_name(db, fake_bot, fake_ai, order_search, no_download):
    paid(fake_ai, 6500.0)
    named(fake_ai)
    fake_ai.receiver_upi = SHOT_UPI
    cands = [{**TWO_TOGETHER[0], "upi_id": SHOT_UPI}, {**TWO_TOGETHER[1], "upi_id": "merchant-77@okhdfc"}]
    case_id, _ = await run(db, order_search, cands)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A]  # A's UPI is the screenshot's; B has the name


async def test_the_name_check_can_be_switched_off(db, fake_bot, fake_ai, order_search, no_download, monkeypatch):
    monkeypatch.setenv("NATIVE_NAME_CHECK", "false")
    reset_settings_cache()
    paid(fake_ai, 6500.0)
    named(fake_ai)
    case_id, _ = await run(db, order_search, TWO_TOGETHER)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A, B]


# ---------------------------------------------------------------- the screenshot's UPI is read by OCR
def test_up_to_three_characters_off_is_still_the_same_upi():
    from app.telegram.upi_check import upi_reading_slip

    assert upi_reading_slip("SK26101181@nsdlpbbpay", "SK26101181@nsdlpbpay")[0]  # live 2026-11-13: 1 letter doubled
    assert upi_reading_slip("OS26091170@nsdlpbbpay", "OS26091170@nsdlpbpay")[0]
    assert upi_reading_slip("ci26102159-4@yblz", "ci26102159-4@ypbiz")[0]  # live 2026-11-13: 2 in the handle
    assert upi_reading_slip("ci26l02l59-4@ypbiz", "ci26102159-4@ypbiz")[0]  # 1 read as l, twice: in the name
    assert upi_reading_slip("ci26l02l59-4@ypbz", "ci26102159-4@ypbiz")[0]  # 3 in all
    assert not upi_reading_slip("ci26l02l59-4@yblz", "ci26102159-4@ypbiz")[0]  # 4: no longer a slip
    assert not upi_reading_slip("ci26102159-4@okaxis", "ci26102159-4@ypbiz")[0]  # another bank altogether
    assert not upi_reading_slip("OS26091170@nsdlpbbpay", "ci26102159-4@ypbiz")[0]  # another UPI altogether
    assert not upi_reading_slip("a1@ibl", "a1@ybl")[0]  # too short a UPI to allow for slips
    assert not upi_reading_slip("••••2159-4@yblz", "ci26102159-4@ypbiz")[0]  # masked: the ending check's business
    assert not upi_reading_slip(None, "ci26102159-4@ypbiz")[0]
    assert not upi_reading_slip("ci26102159-4@ypbiz", "ci26102159-4@ypbiz")[0]  # identical is not a "slip"


def live_1113(fake_ai, amount, when, upi):
    fake_ai.screenshot = {
        **fake_ai.screenshot,
        "amount": {"value": amount, "confidence": 0.97},
        "payment_time": {"value": when, "confidence": 0.95},
    }
    fake_ai.receiver_upi = upi


async def test_live_1113_case_44_one_letter_misread_no_longer_stops_the_payment(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """CASE-20261113-000044: ₹5,000 paid 15:48, ONE order created 15:48 with UPI SK26101181@nsdlpbpay; the
    screenshot was read as SK26101181@nsdlpbbpay (a doubled b) and the case went to Manual Review."""
    live_1113(fake_ai, 5000.0, "2026-11-13 15:48:30", "SK26101181@nsdlpbbpay")
    order = {**NATIVE_ONE[0], "amount": 5000.0, "order_time": "2026-11-13 15:48:00", "upi_id": "SK26101181@nsdlpbpay"}
    case_id, outcome = await run(db, order_search, [order])
    assert outcome == "ready" and not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A]
        row = (await s.execute(select(AuditLog).where(AuditLog.action == "GATEWAY_DETECTED"))).scalars().one()
        assert "a reading slip" in row.details["why"]
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media == [("payment_screenshot", A)]


async def test_live_1113_case_47_the_misread_upi_still_picks_its_own_order(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    """CASE-20261113-000047: ₹2,500 paid 16:32; two orders - 16:32 with UPI ci26102159-4@ypbiz and 16:31 with
    OS26091170@nsdlpbpay. The screenshot reads OS26091170@nsdlpbbpay: the 16:31 order, alone - not the closer one."""
    live_1113(fake_ai, 2500.0, "2026-11-13 16:32:40", "OS26091170@nsdlpbbpay")
    cands = [
        {**NATIVE_ONE[0], "amount": 2500.0, "order_time": "2026-11-13 16:32:00", "upi_id": "ci26102159-4@ypbiz"},
        {**NATIVE_ONE[0], "betex_order_id": B, "illunise_order_id": "1003", "amount": 2500.0,
         "order_time": "2026-11-13 16:31:00", "upi_id": "OS26091170@nsdlpbpay"},
    ]  # fmt: skip
    case_id, outcome = await run(db, order_search, cands)
    assert outcome == "ready" and not any("MANUAL REVIEW" in t for _, t in fake_bot.sent)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [B]
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media == [("payment_screenshot", B)]


async def test_an_exact_upi_beats_one_that_is_a_character_off(db, fake_bot, fake_ai, order_search, no_download):
    """Two orders, UPIs one character apart; the screenshot shows B's exactly: B, not both."""
    paid(fake_ai, 6500.0)
    fake_ai.receiver_upi = SHOT_UPI
    cands = [
        {**NATIVE_ONE[0], "order_time": "2026-09-10 19:31:00", "upi_id": "ci26102159-9@ypbiz"},
        {**NATIVE_ONE[0], "betex_order_id": B, "illunise_order_id": "1003", "order_time": "2026-09-10 19:30:00",
         "upi_id": SHOT_UPI},
    ]  # fmt: skip
    case_id, _ = await run(db, order_search, cands)
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [B]


async def test_a_really_different_upi_is_still_manual_review(db, fake_bot, fake_ai, order_search, no_download):
    """Four characters off, or another UPI altogether: not a reading slip - nothing is sent."""
    paid(fake_ai, 6500.0)
    fake_ai.receiver_upi = "ci26l02l59-4@yblz"  # 4 characters from ci26102159-4@ypbiz
    _, outcome = await run(db, order_search, [{**NATIVE_ONE[0], "upi_id": SHOT_UPI}])
    assert outcome == "escalated"
    assert "none was paid to the UPI on the screenshot" in [t for _, t in fake_bot.sent if "MANUAL REVIEW" in t][-1]


async def test_the_tolerance_can_be_switched_off(db, fake_bot, fake_ai, order_search, no_download, monkeypatch):
    monkeypatch.setenv("UPI_OCR_MAX_EDITS", "0")
    reset_settings_cache()
    live_1113(fake_ai, 5000.0, "2026-11-13 15:48:30", "SK26101181@nsdlpbbpay")
    order = {**NATIVE_ONE[0], "amount": 5000.0, "order_time": "2026-11-13 15:48:00", "upi_id": "SK26101181@nsdlpbpay"}
    _, outcome = await run(db, order_search, [order])
    assert outcome == "escalated"
