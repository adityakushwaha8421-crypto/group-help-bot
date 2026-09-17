"""Original-customer preservation, forwarded evidence, threaded follow-ups, confirmation attribution.

Every case below runs the real correlation, manager, poster (recorded), follow-up sweeper and Betix monitor.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.cases import manager
from app.cases.correlation import ForwardOrigin, attach_message
from app.db.models import CaseStatus, Followup
from app.db.repository import get_case, list_evidence, list_followups
from app.followups.scheduler import sweep
from app.telegram.betix_monitor import handle_group_message, register_self, set_membership_checker
from app.telegram.notifications import customer_link, format_confirmed
from app.utils.timeutil import utcnow
from tests.conftest import make_group_msg, make_input
from tests.test_flow import GOOD, SYS_OK


@pytest.fixture(autouse=True)
def either_mode_no_allowlist(env, monkeypatch):
    """The production rule: system bot OR any group member confirms; no reviewer allowlist."""
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    monkeypatch.setenv("AUTHORIZED_BETIX_REVIEWER_IDS", "")
    monkeypatch.setenv("AUTHORIZED_BETIX_REVIEWER_USERNAMES", "")
    from app.config import reset_settings_cache

    reset_settings_cache()
    register_self(9000, "Chatexportfa_bot")
    yield
    from app.telegram import betix_monitor

    betix_monitor._self_ids.clear()
    betix_monitor._self_usernames.clear()


ADMIN = 111  # the person forwarding (the bot's allow-listed submitter)
CUSTOMER_A = ForwardOrigin(user_id=90001, username="rahul_fa", first_name="Rahul", last_name="Sharma")
CUSTOMER_B = ForwardOrigin(user_id=90002, username=None, first_name="Priya", last_name=None)
HIDDEN = ForwardOrigin(hidden_name="Vikas K")


def fwd(message_id, kind, origin, text=None, **kw):
    return make_input(message_id, kind, text, chat_id=ADMIN, user_id=ADMIN, forward=origin, **kw)


# ---------------------------------------------------------------- ownership
async def test_direct_message_from_customer_owns_the_case(db):
    async with db.session_scope() as s:
        r = await attach_message(
            s, make_input(1, "photo", chat_id=555, user_id=555, first_name="Direct", last_name="Customer")
        )
        assert r.created and r.case.original_user_id == 555 and r.case.original_username == "me"
        assert r.case.original_first_name == "Direct" and not r.case.evidence_forwarded


async def test_forwarded_message_belongs_to_original_sender_not_the_forwarder(db):
    async with db.session_scope() as s:
        r = await attach_message(s, fwd(1, "photo", CUSTOMER_A))
        c = r.case
        assert c.source_user_id == ADMIN  # who submitted
        assert c.original_user_id == 90001  # who the case is about
        assert (
            c.original_username == "rahul_fa" and c.original_first_name == "Rahul" and c.original_last_name == "Sharma"
        )
        assert c.evidence_forwarded is True
        # the admin then types the mobile number as a plain message: it joins the SAME case
        r2 = await attach_message(s, make_input(2, "text", "+91 98765 43210", chat_id=ADMIN, user_id=ADMIN))
        assert r2.case.case_id == c.case_id and not r2.created and r2.case.mobile == "9876543210"
        assert r2.case.original_user_id == 90001  # still the customer, not the admin


async def test_customer_with_and_without_username(db):
    async with db.session_scope() as s:
        a = (await attach_message(s, fwd(1, "photo", CUSTOMER_A))).case
        b = (await attach_message(s, fwd(2, "photo", CUSTOMER_B))).case
        assert a.case_id != b.case_id  # two customers, two cases
        assert customer_link(a) == "@rahul_fa"
        link_b = customer_link(b)
        assert (
            'href="tg://user?id=90002"' in link_b
            and "Priya" in link_b
            and "no username" in link_b
            and "90002" in link_b
        )
        h = (await attach_message(s, fwd(3, "photo", HIDDEN))).case
        assert h.original_first_name == "Vikas K" and h.original_user_id is None
        assert "no user id" in customer_link(h) and "Vikas K" in customer_link(h)


async def test_forwards_from_two_customers_do_not_merge(db):
    async with db.session_scope() as s:
        a1 = (await attach_message(s, fwd(1, "photo", CUSTOMER_A))).case
        b1 = (await attach_message(s, fwd(2, "photo", CUSTOMER_B))).case
        a2 = (await attach_message(s, fwd(3, "document", CUSTOMER_A, "Password:- x1"))).case
        assert a1.case_id == a2.case_id != b1.case_id
        ev_a = await list_evidence(s, a1.case_id)
        assert sorted(e.type for e in ev_a) == ["bank_statement", "payment_screenshot"]


async def test_duplicate_forwarded_screenshot_is_ignored(db):
    async with db.session_scope() as s:
        first = await attach_message(s, fwd(1, "photo", CUSTOMER_A, file_unique_id="SAME-FILE"))
        again = await attach_message(
            s, fwd(2, "photo", CUSTOMER_A, file_unique_id="SAME-FILE")
        )  # new message id, same file
        assert again.duplicate and again.case.case_id == first.case.case_id
        assert len(await list_evidence(s, first.case.case_id)) == 1


async def test_screenshot_forwarded_then_separate_reference_message(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    async with db.session_scope() as s:
        c = (await attach_message(s, fwd(1, "photo", CUSTOMER_A))).case
        await attach_message(s, make_input(2, "text", "9876543210", chat_id=ADMIN, user_id=ADMIN))
        await attach_message(s, fwd(3, "document", CUSTOMER_A))  # statement + video forwarded from the same customer
        await attach_message(s, fwd(4, "video", CUSTOMER_A))
        case_id = c.case_id
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        c = await get_case(s, case_id)
        assert c.betex_pay_order_id == "ILLUN-178621243657290" and c.original_username == "rahul_fa"


# ---------------------------------------------------------------- threaded follow-ups & confirmation
async def posted(db, order_search, fake_poster, origin=CUSTOMER_A):
    async with db.session_scope() as s:
        c = (await attach_message(s, fwd(1, "photo", origin))).case
        await attach_message(s, make_input(2, "text", "9876543210", chat_id=ADMIN, user_id=ADMIN))
        await attach_message(s, fwd(3, "document", origin))
        await attach_message(s, fwd(4, "video", origin))
        case_id = c.case_id
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert fake_poster.media[0] == ("payment_screenshot", "ILLUN-178621243657290")  # plain order id only
        assert c.betix_root_message_id == 501 and c.betix_chat_id == -1009999
        return case_id


async def fire(db, fake_poster, number):
    """Make follow-up row `number` due (even re-arming a cancelled row, to prove the sweeper still refuses)."""
    async with db.session_scope() as s:
        fu = (await s.execute(select(Followup).where(Followup.number == number))).scalar_one()
        fu.due_at = utcnow() - timedelta(seconds=1)
        fu.status = "scheduled"
    return await sweep(lambda: fake_poster)


async def test_followups_reply_to_the_original_screenshot(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id = await posted(db, order_search, fake_poster)
    assert await fire(db, fake_poster, 1) == {"sent": 1}
    assert await fire(db, fake_poster, 2) == {"sent": 1}
    followups = [(t, r) for t, r in fake_poster.texts if t == "Any update?"]
    assert len(followups) == 2
    assert all(reply_to == 501 for _, reply_to in followups)  # both are replies to the screenshot post
    assert followups[0][0] == "Any update?" and "http" not in followups[0][0]  # short, no details: it is a reply
    assert followups[1][0] == "Any update?"  # the second one is just as short
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.followup_1_message_id is not None and c.followup_2_message_id is not None
        assert c.followup_1_message_id != c.followup_2_message_id and c.status == CaseStatus.FOLLOWUP_2_SENT.value


async def _confirm(db, case_id, msg):
    async with db.session_scope() as s:
        r = await handle_group_message(s, msg)
        c = await get_case(s, case_id)
        return r, c, await list_followups(s, case_id)


async def test_confirmation_before_followup_1(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id = await posted(db, order_search, fake_poster)
    r, c, fus = await _confirm(
        db,
        case_id,
        make_group_msg(700, SYS_OK, sender_id=8445, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501),
    )
    assert r["action"] == "verified" and c.status == CaseStatus.VERIFIED.value
    assert all(f.status == "cancelled" for f in fus) and c.followup_1_message_id is None
    for n in (1, 2):
        assert await fire(db, fake_poster, n) == {"skipped": 1}
    assert not any(t == "Any update?" for t, _ in fake_poster.texts)


async def test_confirmation_after_followup_1(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch
):
    case_id = await posted(db, order_search, fake_poster)
    assert await fire(db, fake_poster, 1) == {"sent": 1}
    r, c, fus = await _confirm(
        db, case_id, make_group_msg(701, "success", sender_id=5001, sender_username="Wendy", reply_to=501)
    )
    assert r["action"] == "verified" and c.followup_1_message_id is not None and c.followup_2_message_id is None
    assert {f.number: f.status for f in fus} == {1: "sent", 2: "cancelled", 99: "cancelled"}
    assert await fire(db, fake_poster, 2) == {"skipped": 1}
    assert sum(1 for t, _ in fake_poster.texts if t == "Any update?") == 1


async def test_confirmation_after_followup_2(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id = await posted(db, order_search, fake_poster)
    await fire(db, fake_poster, 1)
    await fire(db, fake_poster, 2)
    r, c, fus = await _confirm(
        db, case_id, make_group_msg(702, "confirmed", sender_id=5001, sender_username="Wendy", reply_to=501)
    )
    assert r["action"] == "verified" and c.status == CaseStatus.VERIFIED.value
    assert {f.number: f.status for f in fus} == {1: "sent", 2: "sent", 99: "cancelled"}
    assert await fire(db, fake_poster, 99) == {"skipped": 1}  # no escalation after confirmation


async def test_confirmation_from_system_bot_is_attributed(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id = await posted(db, order_search, fake_poster)
    r, c, _ = await _confirm(
        db,
        case_id,
        make_group_msg(710, SYS_OK, sender_id=8445, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501),
    )
    assert (c.confirmation_type, c.confirmation_message_id, c.confirmation_user_id, c.confirmation_username) == (
        "system_bot",
        710,
        8445,
        "betixpay_cs_bot",
    )
    assert c.confirmation_at is not None
    text = [t for _, t in fake_bot.sent if "PAYMENT CONFIRMED" in t][0]
    assert "@rahul_fa" in text and "Betix System" in text and "ILLUN-178621243657290" in text


async def test_confirmation_from_any_group_member_is_attributed(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    async def everyone(chat_id, user_id):
        return chat_id == -1009999

    set_membership_checker(everyone)
    try:
        case_id = await posted(db, order_search, fake_poster, origin=CUSTOMER_B)
        r, c, _ = await _confirm(
            db, case_id, make_group_msg(720, "done", sender_id=77777, sender_username="new_betix_agent", reply_to=501)
        )
        assert r["authority"] == "group_member" and r["action"] == "verified"
        assert (c.confirmation_type, c.confirmation_user_id, c.confirmation_username) == (
            "group_member",
            77777,
            "new_betix_agent",
        )
        text = [t for _, t in fake_bot.sent if "PAYMENT CONFIRMED" in t][0]
        assert "✅ Confirmed by: @new_betix_agent" in text and text.count("new_betix_agent") == 1
        assert (
            'href="tg://user?id=90002"' in text and "Priya" in text and "no username" in text
        )  # customer without username
    finally:
        set_membership_checker(None)


async def test_unrelated_success_does_not_confirm(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id = await posted(db, order_search, fake_poster)
    r, c, fus = await _confirm(
        db,
        case_id,
        make_group_msg(
            730,
            "success",
            sender_id=5001,
            sender_username="Wendy",
            reply_to=999999,
            sent_at=utcnow() + timedelta(hours=2),
        ),
    )
    assert r["action"] == "unlinked" and c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value
    assert all(f.status == "scheduled" for f in fus)
    assert not any("Payment confirmed" in t for _, t in fake_bot.sent)


async def test_solved_notification_format(db):
    from app.db.models import Case

    c = Case(
        case_id="CASE-20260910-000042",
        source_chat_id=1,
        source_user_id=1,
        original_user_id=90001,
        original_username="customer_username",
        registration_number="9876543210",
        betex_pay_order_id="ILLUN-178903327221195",
        amount=1485.0,
        currency="INR",
        confirmation_type="group_member",
        confirmation_username="betix_staff",
        evidence_forwarded=True,
    )
    text = format_confirmed(c, [], confirmed_by="@betix_staff", confirmed_at=utcnow(), tz="Asia/Kolkata")
    assert "PAYMENT CONFIRMED" in text
    for needle in (
        "@customer_username",
        "🧾 Order: <code>ILLUN-178903327221195</code>",
        "₹1,485.00",
        "🕒 Time:",
        "✅ Confirmed by: @betix_staff",
    ):
        assert needle in text, needle
    assert "http" not in text and "PI" not in text.replace("PAYMENT", "")


# ---------------------------------------------------------------- the batch rule (live incident 2026-09-10 13:11)
async def test_forwarded_batch_with_mixed_origins_is_one_case(db):
    """Statement forwarded from 'Sunil', screenshot forwarded from the operator's own account, video forwarded
    from 'Shyamu', all within seconds: one case, owned by the first real customer."""
    sunil = ForwardOrigin(user_id=70001, first_name="Sunil")
    me = ForwardOrigin(user_id=ADMIN, username="fantasyAdda_support", first_name="Fantasy Adda")  # self-forward
    shyamu = ForwardOrigin(user_id=70002, first_name="Shyamu")
    async with db.session_scope() as s:
        a = (await attach_message(s, fwd(214, "document", sunil))).case
        b = (await attach_message(s, fwd(212, "photo", me))).case
        c = (await attach_message(s, fwd(213, "video", shyamu))).case
        d = (await attach_message(s, make_input(218, "text", "9876543210", chat_id=ADMIN, user_id=ADMIN))).case
        assert a.case_id == b.case_id == c.case_id == d.case_id
        assert a.original_user_id == 70001 and a.original_first_name == "Sunil"
        assert sorted(e.type for e in await list_evidence(s, a.case_id)) == [
            "bank_statement",
            "payment_screenshot",
            "payment_video",
        ]
        assert a.mobile == "9876543210"


async def test_self_forward_first_then_customer_forward_adopts_the_customer(db):
    me = ForwardOrigin(user_id=ADMIN, username="fantasyAdda_support", first_name="Fantasy Adda")
    async with db.session_scope() as s:
        a = (await attach_message(s, fwd(1, "photo", me))).case
        assert a.original_user_id == ADMIN  # for now the case is the operator's own
        b = (await attach_message(s, fwd(2, "document", CUSTOMER_A))).case
        assert b.case_id == a.case_id and a.original_user_id == 90001 and a.original_username == "rahul_fa"


async def test_batch_rule_does_not_merge_a_later_customer(db):
    async with db.session_scope() as s:
        a = (await attach_message(s, fwd(1, "document", CUSTOMER_A))).case
        a.last_input_at = utcnow() - timedelta(seconds=60)  # the batch is long over
        b = (await attach_message(s, fwd(2, "video", CUSTOMER_B))).case
        assert b.case_id != a.case_id
