"""BETIX or NATIVE. Once the screenshot and the mobile are in, the case card asks which gateway the customer paid
through and NOTHING is processed until the operator taps. Betix: the flow as before. Native: nothing else is
needed, the order amount is a round figure, and the screenshot goes to the Native group captioned with EVERY
order id created under the mobile at the payment time (one or several, closest first)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.cases import manager
from app.cases.gateway import gateway_pending
from app.config import get_settings, reset_settings_cache
from app.db.models import GATEWAY_BETIX, GATEWAY_NATIVE, AuditLog, CaseStatus, Followup
from app.db.repository import get_case, list_evidence
from app.telegram import input_bot
from app.telegram.betix_monitor import handle_group_message, is_betix_chat, set_membership_checker
from app.telegram.progress import card_buttons, progress_card
from app.workers import tasks
from tests.conftest import make_group_msg, make_input
from tests.test_flow import GOOD, MOBILE, submit_four_messages

NATIVE_CHAT = -1004444
A, B, C = "ILLUN-178621243657290", "ILLUN-178621243657291", "ILLUN-178621243657292"
# paid ₹6,499.92 at 19:32 (the fake AI's screenshot). Native orders are ROUND figures: ₹6,500.
NATIVE_ONE = [{**GOOD[0], "amount": 6500.0, "gateway": "Native", "order_time": "2026-09-10 19:30:00"}]
NATIVE_TWO = [
    {**GOOD[0], "amount": 6500.0, "gateway": "Native", "order_time": "2026-09-10 19:28:00"},
    {**GOOD[0], "illunise_order_id": "1003", "betex_order_id": B, "amount": 6500.0, "gateway": "Native",
     "order_time": "2026-09-10 19:31:00"},
]  # fmt: skip


@pytest.fixture
def asking(monkeypatch):
    monkeypatch.setenv("ASK_GATEWAY", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


class Chat:
    def __init__(self, sink, inp):
        self.chat = SimpleNamespace(id=inp.chat_id, type="private")
        self.message_id = inp.message_id
        self._sink, self._n = sink, 900

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self._n += 1
        self._sink.append((text, reply_markup))
        return SimpleNamespace(message_id=self._n)


class Tap:
    """A CallbackQuery from the operator on a case card."""

    def __init__(self, choice, case_id, chat_id=111, user_id=111, message_id=901):
        self.data = f"gw:{choice}:{case_id}"
        self.from_user = SimpleNamespace(id=user_id, username="me")
        self.message = Chat([], SimpleNamespace(chat_id=chat_id, message_id=message_id))
        self.answers: list[str] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


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


def buttons_of(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row] if markup else []


# ---------------------------------------------------------------- the setting
def test_the_native_group_id_is_accepted_with_or_without_its_sign(monkeypatch):
    for given, want in (("-1003515687583", -1003515687583), ("1003515687583", -1003515687583),
                        ("3515687583", -1003515687583), ("", None), ("x", None)):  # fmt: skip
        monkeypatch.setenv("NATIVE_GROUP_CHAT_ID", given)
        reset_settings_cache()
        assert get_settings().native_chat == want, given
    reset_settings_cache()


def test_the_native_group_is_monitored_like_a_betix_group():
    assert is_betix_chat(NATIVE_CHAT) and is_betix_chat(-1009999) and not is_betix_chat(-1005555)


# ---------------------------------------------------------------- the question
async def test_the_card_asks_betix_or_native_and_nothing_starts(db, fake_bot, asking, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    assert jobs == []  # no processing, no FORCE SEND wait
    text, markup = sink[-1]
    assert "Which gateway" in text and "Gateway — waiting for your choice" in text
    assert buttons_of(markup) == [f"gw:betix:{case_id}", f"gw:native:{case_id}"]
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.gateway_choice is None and c.status == CaseStatus.WAITING_FOR_INPUT.value
        assert await manager.process_case(s, case_id, force=True) == "gateway_pending"


async def test_more_evidence_keeps_the_question_open(db, fake_bot, asking, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    await send(sink, make_input(3, "document"), make_input(4, "video"))
    assert jobs == []  # all four in, still nothing until the tap
    _, _, text = fake_bot.edits[-1]
    assert "Which gateway" in text and "✅ Bank Statement" in text and "✅ Payment Video" in text
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).gateway_choice is None


async def test_the_question_is_not_asked_before_the_screenshot_and_mobile(db, fake_bot, asking, jobs):
    sink = []
    await send(sink, make_input(1, "photo"))
    text, markup = sink[-1]
    assert "Which gateway" not in text and markup is None
    assert "Waiting for the mobile number" in text


async def test_a_withdrawal_is_never_asked(db, fake_bot, asking, jobs):
    sink = []
    await send(sink, make_input(1, "text", "WD-84425-67115"), make_input(2, "document"))
    text, markup = sink[-1]
    assert "Which gateway" not in text and markup is None


async def test_with_the_question_off_the_case_starts_as_before(db, fake_bot, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    assert jobs == [("process_case_job", (case_id, 1, True, True), 30)]
    assert "Which gateway" not in sink[-1][0] and sink[-1][1] is None


# ---------------------------------------------------------------- the tap
async def test_betix_with_only_the_screenshot_starts_the_force_send_wait(db, fake_bot, asking, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    tap = Tap(GATEWAY_BETIX, case_id)
    await input_bot.on_gateway_button(tap)
    assert tap.answers == ["✅ Betix"]
    assert jobs == [("process_case_job", (case_id, 1, True, True), 30)]  # the 30 s wait, as before
    _, _, text = fake_bot.edits[-1]
    assert "Gateway: Betix" in text and "Waiting 30s for the bank statement and payment video" in text
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.gateway_choice == GATEWAY_BETIX
        rows = (await s.execute(select(AuditLog).where(AuditLog.action == "GATEWAY_CHOSEN"))).scalars().all()
        assert len(rows) == 1 and rows[0].result == "betix" and rows[0].actor == "@me"


async def test_betix_with_everything_in_is_processed_at_once(db, fake_bot, asking, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE), make_input(3, "document"),
                         make_input(4, "video"))  # fmt: skip
    await input_bot.on_gateway_button(Tap(GATEWAY_BETIX, case_id))
    assert jobs == [("process_case_job", (case_id, 1, True), 0)]


async def test_native_starts_at_once_and_needs_nothing_else(db, fake_bot, asking, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    tap = Tap(GATEWAY_NATIVE, case_id)
    await input_bot.on_gateway_button(tap)
    assert tap.answers == ["✅ Native"]
    assert jobs == [("process_case_job", (case_id, 1, True), 0)]  # no wait for a statement or video
    _, _, text = fake_bot.edits[-1]
    assert "Gateway: Native" in text and "no statement or video needed" in text
    assert "Bank Statement" not in text and "Payment Video" not in text
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        from app.cases.correlation import missing_items

        assert c.gateway_choice == GATEWAY_NATIVE
        assert missing_items(c, await list_evidence(s, case_id)) == []
        assert not gateway_pending(c, [])


async def test_a_second_tap_changes_nothing(db, fake_bot, asking, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    await input_bot.on_gateway_button(Tap(GATEWAY_NATIVE, case_id))
    tap = Tap(GATEWAY_BETIX, case_id)
    await input_bot.on_gateway_button(tap)
    assert tap.answers == ["Already chosen: Native"] and len(jobs) == 1
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).gateway_choice == GATEWAY_NATIVE


async def test_only_an_admin_can_tap(db, fake_bot, asking, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    tap = Tap(GATEWAY_NATIVE, case_id, user_id=999)
    await input_bot.on_gateway_button(tap)
    assert tap.answers == ["⛔ Not authorized"] and jobs == []


async def test_the_live_card_keeps_the_buttons_while_the_question_is_open(db, fake_bot, asking, jobs):
    sink = []
    case_id = await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    await input_bot.refresh_progress(case_id)  # any status push while waiting re-sends the question
    _, _, text = fake_bot.edits[-1]
    assert "Which gateway" in text
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        from app.cases.correlation import missing_items

        missing = missing_items(c, await list_evidence(s, case_id))
        assert buttons_of(card_buttons(c, missing)) == [f"gw:betix:{case_id}", f"gw:native:{case_id}"]
        assert card_buttons(c) is None  # without the evidence picture no button is guessed


# ---------------------------------------------------------------- the Native flow
async def native_case(db) -> str:
    """Screenshot + mobile in, the operator tapped Native."""
    from app.cases.correlation import attach_message

    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", MOBILE))
        r.case.gateway_choice = GATEWAY_NATIVE
        r.case.progress_chat_id, r.case.progress_message_id = 111, 4242
        return r.case.case_id


async def run_native(db, order_search, cands):
    case_id = await native_case(db)
    order_search(cands)
    async with db.session_scope() as s:
        outcome = await manager.process_case(s, case_id, force=True)
    return case_id, outcome


async def test_one_native_order_goes_to_the_native_group_with_the_screenshot(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id, outcome = await run_native(db, order_search, NATIVE_ONE)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.native_order_ids == [A] and c.betex_pay_order_id == A and c.order_amount == 6500.0
        assert "ORDER MATCHED" in progress_card(c) and "Native group" in progress_card(c)
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value and c.betix_chat_id == NATIVE_CHAT
        assert (await s.execute(select(Followup))).scalars().all() == []  # no "Any update?" in the Native group
        assert "SENT TO NATIVE GROUP" in progress_card(c)
    assert fake_poster.chats == [NATIVE_CHAT]
    assert fake_poster.media == [("payment_screenshot", A)]  # only the screenshot, captioned with the order id
    assert fake_poster.texts == []  # no amount note, no statement, no video


async def test_several_orders_at_the_payment_time_all_go_in_the_caption_closest_first(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id, outcome = await run_native(db, order_search, NATIVE_TWO)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.native_order_ids == [B, A]  # B was created 1 min before the payment, A 4 min
        assert "ORDERS FOUND" in progress_card(c) and B in progress_card(c) and A in progress_card(c)
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
    assert fake_poster.media == [("payment_screenshot", f"{B}\n{A}")]
    assert len([m for m in fake_poster.media]) == 1  # ONE post for the payment


async def test_the_native_amount_is_the_rounded_figure_and_betix_orders_never_count(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    cands = [
        {**NATIVE_TWO[0]},  # ₹6,500 Native, 4 min before: fits
        {**NATIVE_TWO[1], "amount": 6000.0},  # another amount: no
        {**GOOD[0], "betex_order_id": C, "illunise_order_id": "1004", "amount": 6500.0, "gateway": "BetixPay",
         "order_time": "2026-09-10 19:31:00"},  # a Betix order, right amount and time: never a Native one
        {**NATIVE_ONE[0], "betex_order_id": "ILLUN-178621243657293", "illunise_order_id": "1005",
         "order_time": "2026-09-10 19:40:00"},  # created AFTER the payment: no
        {**NATIVE_ONE[0], "betex_order_id": "ILLUN-178621243657294", "illunise_order_id": "1006",
         "registration_number": "9000000001"},  # another customer's: no
    ]  # fmt: skip
    case_id, outcome = await run_native(db, order_search, cands)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A]


async def test_a_round_payment_matches_the_round_order(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    fake_ai.screenshot = {**fake_ai.screenshot, "amount": {"value": 6500.0, "confidence": 0.97}}
    case_id, outcome = await run_native(db, order_search, NATIVE_ONE)
    assert outcome == "ready"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).native_order_ids == [A]


async def test_no_native_order_is_manual_review_and_nothing_is_posted(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id, outcome = await run_native(db, order_search, [{**NATIVE_ONE[0], "amount": 6000.0}])
    assert outcome == "escalated"
    alert = [t for _, t in fake_bot.sent if "MANUAL REVIEW" in t][-1]
    assert "Native: no order of ₹6,500" in alert and A in alert
    assert fake_poster.media == [] and fake_poster.texts == []
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.ESCALATED.value


async def test_a_native_order_is_never_asked_with_pi_nor_searched_by_amount(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    holder = order_search([{**NATIVE_ONE[0], "amount": 6000.0}])
    await run_native(db, order_search, [{**NATIVE_ONE[0], "amount": 6000.0}])
    assert holder["query"] == MOBILE  # the mobile only: no UTR / padded-amount fallback search
    assert not any(t.startswith("/pi") for t, _ in fake_poster.texts)


async def test_a_members_done_in_the_native_group_confirms_the_case(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    reset_settings_cache()

    async def everyone(chat_id, user_id):
        return True

    set_membership_checker(everyone)
    try:
        case_id, _ = await run_native(db, order_search, NATIVE_ONE)
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


async def test_a_betix_case_still_goes_to_the_betix_group(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id = await submit_four_messages(db)
    async with db.session_scope() as s:
        (await get_case(s, case_id)).gateway_choice = GATEWAY_BETIX
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.betix_chat_id == -1009999 and c.native_order_ids is None
        assert len((await s.execute(select(Followup))).scalars().all()) > 0


async def test_recovery_leaves_an_unanswered_case_alone(db, fake_bot, asking, jobs):
    sink = []
    await send(sink, make_input(1, "photo"), make_input(2, "text", MOBILE))
    counts = await tasks.recover()
    assert counts.get("gateway_pending") == 1 and jobs == []


async def test_add_on_a_native_case_needs_nothing(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    from tests.test_add_evidence import Msg

    case_id, _ = await run_native(db, order_search, NATIVE_ONE)
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
    sink = []
    from app.telegram import add_evidence

    add_evidence._sessions.clear()
    await input_bot.cmd_add(Msg(make_input(100, "text", chat_id=111, user_id=111), sink), SimpleNamespace(args=MOBILE))
    for t in input_bot._add_timers.values():
        t.cancel()
    input_bot._add_timers.clear()
    add_evidence._sessions.clear()
    assert "already with the Native group" in sink[-1] and "already has everything" in sink[-1]
