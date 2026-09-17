from datetime import timedelta

from app.cases.correlation import correlate_betix_message
from app.db.models import BetixMessage, Case, CaseStatus
from app.utils.timeutil import utcnow


async def seed(s, n=1):
    cases = []
    for i in range(n):
        c = Case(
            case_id=f"CASE-20260910-00000{i + 1}",
            status=CaseStatus.WAITING_FOR_CONFIRMATION.value,
            source_chat_id=1,
            source_user_id=1,
            registration_number=f"REG00{i}",
            betex_pay_order_id=f"ILLUN-1786000000000{i}",
            utr=f"11112222333{i}",
            betix_root_message_id=100 + i,
            betix_posted_at=utcnow() - timedelta(minutes=2 + i * 30),
        )
        s.add(c)
        s.add(
            BetixMessage(
                case_id=c.case_id, chat_id=-1009999, message_id=100 + i, direction="out", kind="evidence_screenshot"
            )
        )
        cases.append(c)
    await s.flush()
    return cases


async def test_correlation_methods(db):
    async with db.session_scope() as s:
        await seed(s, 2)
        kw = dict(chat_id=-1009999, plat_order_nos=[], utrs=[], sender_authority="group_member", sent_at=utcnow())
        c = await correlate_betix_message(s, reply_to_message_id=101, order_ids=[], text="checking", **kw)
        assert (c.case_id, c.method) == ("CASE-20260910-000002", "reply_chain")
        c = await correlate_betix_message(
            s, reply_to_message_id=None, order_ids=["ILLUN-17860000000000"], text="", **kw
        )
        assert (c.case_id, c.method) == ("CASE-20260910-000001", "betex_order_id")
        c = await correlate_betix_message(s, reply_to_message_id=None, order_ids=[], text="REG001 done", **kw)
        assert (c.case_id, c.method) == ("CASE-20260910-000002", "registration")
        c = await correlate_betix_message(
            s, reply_to_message_id=None, order_ids=[], text="", **dict(kw, utrs=["111122223331"])
        )
        assert (c.case_id, c.method) == ("CASE-20260910-000002", "utr")
        # proximity: only case 1 was posted within the last 10 minutes -> low-confidence link
        c = await correlate_betix_message(s, reply_to_message_id=None, order_ids=[], text="Success", **kw)
        assert (c.case_id, c.method) == ("CASE-20260910-000001", "proximity") and c.confidence < 0.8
        # an unknown sender never gets a proximity link
        c = await correlate_betix_message(
            s, reply_to_message_id=None, order_ids=[], text="Success", **dict(kw, sender_authority="unknown")
        )
        assert c.case_id is None
