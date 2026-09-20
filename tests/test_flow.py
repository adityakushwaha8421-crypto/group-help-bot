"""End-to-end flow with SQLite + fakes: four separate messages -> one case -> match -> post -> confirm."""

from datetime import timedelta

from sqlalchemy import select

from app.cases import manager
from app.cases.correlation import attach_message
from app.db.models import AuditLog, CaseStatus, Followup, Notification
from app.db.repository import get_case, list_case_betix_messages, list_evidence, list_followups
from app.followups.scheduler import sweep
from app.telegram.betix_monitor import handle_group_message
from app.utils.timeutil import utcnow
from tests.conftest import make_group_msg, make_input

MOBILE = "9876543210"
GOOD = [
    {
        "illunise_order_id": "1001",
        "betex_order_id": "ILLUN-178621243657290",
        "registration_number": MOBILE,
        "amount": 6499.92,
        "order_time": "2026-09-10 19:30:40",
        "status": "Pending",
        "gateway": "BetixPay",
    },
    {
        "illunise_order_id": "1002",
        "betex_order_id": "ILLUN-178621243657999",
        "registration_number": MOBILE,
        "amount": 6499.92,
        "order_time": "2026-09-05 09:00:00",
        "status": "Paid",
        "gateway": "BetixPay",
    },
]

SYS_OK = (
    "✅ STATUS: Successful\nPayment confirmed. Thank you!\n\nPlatNo: PI260823h7r4pvcjbp\n"
    "MerchantOrderNo: ILLUN-178621243657290"
)
SYS_FAIL = (
    "🛑 STATUS: Failed\nPayment unsuccessful. Please check your order.\n\nPlatNo: PI260804lpdsce7g71\n"
    "MerchantOrderNo: ILLUN-178621243657290"
)


async def submit_four_messages(db):
    async with db.session_scope() as s:
        r1 = await attach_message(s, make_input(1, "photo"))
        r2 = await attach_message(s, make_input(2, "text", MOBILE))
        r3 = await attach_message(s, make_input(3, "document", "Password:- lata2812"))
        r4 = await attach_message(s, make_input(4, "video"))
        assert r1.created and not (r2.created or r3.created or r4.created)
        assert {r.case.case_id for r in (r1, r2, r3, r4)} == {r1.case.case_id}
        assert r1.case.mobile == MOBILE and r1.case.registration_number is None
        assert r1.case.statement_password == "lata2812"
        dup = await attach_message(s, make_input(2, "text", MOBILE))
        assert dup.duplicate
        ev = await list_evidence(s, r1.case.case_id)
        assert sorted(e.type for e in ev) == ["bank_statement", "payment_screenshot", "payment_video"]
        return r1.case.case_id


async def test_grouping_new_mobile_starts_new_case(db):
    async with db.session_scope() as s:
        a = await attach_message(s, make_input(1, "text", "9111111111"))
        b = await attach_message(s, make_input(2, "photo"))
        c = await attach_message(s, make_input(3, "text", "9222222222"))
        assert a.case.case_id == b.case.case_id != c.case.case_id


async def run_until_ready(db, order_search, cands=GOOD):
    case_id = await submit_four_messages(db)
    order_search(cands)
    async with db.session_scope() as s:
        outcome = await manager.process_case(s, case_id, force=True)
    return case_id, outcome


async def test_full_flow_strict_confirmation(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, outcome = await run_until_ready(db, order_search)
    assert outcome == "ready"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.READY_FOR_BETIX.value
        assert c.betex_pay_order_id == "ILLUN-178621243657290" and c.illunise_order_id == "1001"
        assert c.amount == 6499.92 and c.utr == "611532946151"
        assert c.match_confidence >= 0.9
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "posted"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value
        assert c.betix_root_message_id == 501
        # POLICY: the screenshot with the plain ILLUN id goes first (the anchor); statement and video follow
        # as REPLIES to it (BETIX_EXTRA_EVIDENCE_POLICY=always). No text message, no details.
        assert fake_poster.media == [
            ("payment_screenshot", "ILLUN-178621243657290"),
            ("bank_statement", "Password:- lata2812"),
            ("payment_video", None),
        ]
        assert fake_poster.media_replies[1:] == [("bank_statement", 501), ("payment_video", 501)]
        assert fake_poster.texts == []
        fus = await list_followups(s, case_id)
        assert [(f.number, f.status) for f in fus] == [(1, "scheduled"), (2, "scheduled"), (99, "scheduled")]
        assert fus[0].due_at - c.betix_posted_at == timedelta(minutes=15)
        assert fus[1].due_at - fus[0].due_at == timedelta(minutes=30)
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "already"  # idempotent
        assert len(fake_poster.media) == 3

    # random user says success -> ignored
    async with db.session_scope() as s:
        r = await handle_group_message(s, make_group_msg(600, "Success", sender_username="stranger", reply_to=501))
        assert r["action"] == "ignored_unknown_sender"
    # system bot replies SUCCESS to our screenshot -> strict mode: partial
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(601, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r["action"] == "partial" and r["correlation"] == "reply_chain"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value and c.system_confirmed_at
        assert c.betix_plat_order_no == "PI260823h7r4pvcjbp"
    # authorized reviewer says Success, not as a reply but mentioning the order id -> verified
    async with db.session_scope() as s:
        r = await handle_group_message(s, make_group_msg(602, "Success ILLUN-178621243657290", sender_username="Wendy"))
        assert r["action"] == "verified" and r["correlation"] == "betex_order_id"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.VERIFIED.value and c.followup_cancelled
        assert all(f.status == "cancelled" for f in await list_followups(s, case_id))
        again = await handle_group_message(
            s, make_group_msg(602, "Success ILLUN-178621243657290", sender_username="Wendy")
        )
        assert again["action"] == "duplicate"
    confirmed = [t for _, t in fake_bot.sent if "PAYMENT CONFIRMED" in t]
    assert len(confirmed) == 1 and "ILLUN-178621243657290" in confirmed[0] and "@Wendy" in confirmed[0]
    # even a stale schedule can never send a follow-up after verification
    async with db.session_scope() as s:
        for f in (await s.execute(select(Followup))).scalars():
            f.status, f.due_at = "scheduled", utcnow() - timedelta(minutes=1)
    assert await sweep(lambda: fake_poster) == {"skipped": 3}
    assert not any(t == "Any update?" for t, _ in fake_poster.texts)
    async with db.session_scope() as s:
        actions = [a.action for a in (await s.execute(select(AuditLog).where(AuditLog.case_id == case_id))).scalars()]
    for a in [
        "CASE_CREATED",
        "ORDER_SEARCH_STARTED",
        "ORDER_CANDIDATE_FOUND",
        "ORDER_MATCH_SELECTED",
        "BETIX_EVIDENCE_POSTED",
        "BETIX_CONFIRMATION_DETECTED",
        "CASE_VERIFIED",
        "FOLLOWUPS_CANCELLED",
    ]:
        assert a in actions, a


async def test_monitor_mode_single_signal(db, fake_bot, fake_ai, order_search, no_download, fake_poster, monkeypatch):
    monkeypatch.setenv("CONFIRMATION_MODE", "monitor")
    from app.config import reset_settings_cache

    reset_settings_cache()
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(s, make_group_msg(700, "Success", sender_id=5001, reply_to=501))
        assert r["action"] == "verified"


async def test_followups_and_escalation(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
    assert await sweep(lambda: fake_poster) == {}  # nothing due yet
    for n, expect_status in ((1, "FOLLOWUP_1_SENT"), (2, "FOLLOWUP_2_SENT")):
        async with db.session_scope() as s:
            fu = (await s.execute(select(Followup).where(Followup.number == n))).scalar_one()
            fu.due_at = utcnow() - timedelta(seconds=1)
        assert await sweep(lambda: fake_poster) == {"sent": 1}
        assert await sweep(lambda: fake_poster) == {}  # never sent twice
        async with db.session_scope() as s:
            assert (await get_case(s, case_id)).status == expect_status
    texts = [t for t, _ in fake_poster.texts]
    assert any(t == "Any update?" for t in texts)
    assert any(t == "Any update?" for t in texts)
    assert not any(t.startswith("/pi") for t in texts)  # no extra status queries in the group
    assert all(r == 501 for t, r in fake_poster.texts if t == "Any update?")  # replies to the screenshot
    async with db.session_scope() as s:
        fu = (await s.execute(select(Followup).where(Followup.number == 99))).scalar_one()
        fu.due_at = utcnow() - timedelta(seconds=1)
    assert await sweep(lambda: fake_poster) == {"escalated": 1}
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ESCALATED.value and c.followups_sent == 2
    alerts = [t for _, t in fake_bot.sent if "MANUAL REVIEW NEEDED" in t]
    assert len(alerts) == 1 and "No confirmation after 2 follow-up(s)" in alerts[0]
    # a late confirmation after escalation still verifies (monitoring keeps listening)
    async with db.session_scope() as s:
        await handle_group_message(s, make_group_msg(801, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True))
        r = await handle_group_message(s, make_group_msg(802, "done", sender_username="queenie", reply_to=801))
        assert r["action"] == "verified" and r["correlation"] == "reply_chain"


async def test_ambiguous_match_stops_and_alerts(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    twins = [dict(GOOD[0]), dict(GOOD[0])]
    twins[1]["betex_order_id"] = "ILLUN-178621243657291"
    twins[1]["illunise_order_id"] = "1003"
    case_id, outcome = await run_until_ready(db, order_search, twins)
    assert outcome == "ambiguous"
    async with db.session_scope() as s:
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ORDER_MATCH_AMBIGUOUS.value and c.betex_pay_order_id is None
        assert await manager.post_case_to_betix(s, case_id, fake_poster) == "already"
    assert any("Multiple orders match" in t for _, t in fake_bot.sent)
    assert fake_poster.media == []


async def test_no_candidates_escalates(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    case_id, outcome = await run_until_ready(db, order_search, [])
    assert outcome == "escalated"
    async with db.session_scope() as s:
        assert (await get_case(s, case_id)).status == CaseStatus.ESCALATED.value
    assert any(
        "was created before the payment" in t and "searched by the mobile number and by the amount" in t
        for _, t in fake_bot.sent
    )


async def test_login_failure_escalates(db, fake_bot, fake_ai, no_download, fake_poster):
    from app.admin.browser import ManualAuthRequired

    async def boom(q, amount=None, when=None):
        raise ManualAuthRequired("otp")

    manager.set_order_search(boom)
    try:
        case_id = await submit_four_messages(db)
        async with db.session_scope() as s:
            assert await manager.process_case(s, case_id, force=True) == "escalated"
    finally:
        manager.set_order_search(None)
    assert any("manual authentication" in t for _, t in fake_bot.sent)


async def test_betix_failed_status_escalates_not_verifies(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(900, SYS_FAIL, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r["action"] == "escalated_failed"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.ESCALATED.value and c.verified_at is None
    assert not any("Payment confirmed" in t for _, t in fake_bot.sent)


async def test_notification_idempotent(db, fake_bot):
    from app.db.models import Case
    from app.telegram.notifications import notify_admin

    async with db.session_scope() as s:
        c = Case(case_id="CASE-20260910-000009", source_chat_id=1, source_user_id=1)
        s.add(c)
        await s.flush()
        assert await notify_admin(s, kind="x", text="hello", case=c) is not None
        assert await notify_admin(s, kind="x", text="hello", case=c) is None
        n = (await s.execute(select(Notification))).scalars().all()
        assert len(n) == 1 and n[0].sent
    assert len(fake_bot.sent) == 1


async def test_recovery_reschedules_followups(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    from app.workers.tasks import recover

    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
        for f in await list_followups(s, case_id):
            await s.delete(f)  # simulate rows lost in a crash
    counts = await recover()
    assert counts.get("monitoring") == 1
    async with db.session_scope() as s:
        assert len(await list_followups(s, case_id)) == 3
        bm = await list_case_betix_messages(s, case_id)
        out = [m for m in bm if m.direction == "out"]
        assert [m.kind for m in out] == ["evidence_screenshot", "bank_statement", "payment_video"]
        assert out[0].reply_to_message_id is None and all(m.reply_to_message_id == 501 for m in out[1:])
