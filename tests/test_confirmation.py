"""Uses verbatim message shapes from the Betix support group export."""

import re

from app.telegram.confirmation import (
    authority_for_sender,
    classify_human_message,
    classify_system_message,
    is_confirmed,
)

BX = re.compile(r"\bILLUN-\d{8,20}\b", re.I)
PL = re.compile(r"\bPI[0-9a-z]{10,24}\b")

SUCCESS_STATUS = (
    "✅ STATUS: Successful\nPayment confirmed. Thank you!\n\nPlatNo: PI2607293hgertk2jua\n"
    "MerchantOrderNo: ILLUN-178531583978389"
)
PENDING_STATUS = (
    "📌 STATUS: Still Pending\nPayment has not been confirmed. Please share a bank transaction statement "
    "or a payment video recording so that we can look into it further. (Kindly ignore this if it has "
    "been shared.)\n\nPlatNo: PI2608024f1am69vdqj\nMerchantOrderNo: ILLUN-178568624981711"
)
FAILED_STATUS = (
    "🛑 STATUS: Failed\nPayment unsuccessful. Please check your order.\n\nPlatNo: PI260804lpdsce7g71\n"
    "MerchantOrderNo: ILLUN-178583510770864"
)
PI_PAID = (
    "Order's UPI: 7896709133@ptsbi\n💵OrderAmount: 2000\n 💰PaidAmount: 2000\n"
    "📄OrderStatus: Paid | 🔁CallbackStatus: Success\n🧾UTR: 611532946151\n"
    "📌PlatOrderNo: PI2608032qdauslocv8 ( 79 )\n📌MerchantOrderNo: ILLUN-178577603155718\n"
    "🕒CreatedTime: 2026-08-03 22:23:52 +05:30"
)
PI_PENDING = (
    "Order's UPI: niyas74@ptyes\n💵OrderAmount: 1690\n 💰PaidAmount: 0\n"
    "📄OrderStatus: Pending | 🔁CallbackStatus: Init\n🧾UTR: 059407375233\n📌PlatOrderNo: PI2608085oq4ui12qjs"
)
ACK = (
    "1.Your order issue has been recorded on our platform.\n We will conduct a preliminary verification and "
    "reply to you."
)
RECOG = (
    "🔍Recognition results\n 🧾UTR: <code>166278480667</code> \n 🏦 UPI: <code>x@naviaxis</code>\n"
    "✅ UPI Belongs to us--35,7\nThis UTR was not found at the moment !"
)
NOTICE = "📢 BetixPay Payout Notice 📢\n\n🚀 Payout channel are running smoothly"
ALREADY = (
    "Already matched \n📄[311713502606(22)]\n💵OrderAmount: 6894.00\n💰PaidAmount: 6894.00\n"
    "📌MerchantOrderNo: ILLUN-178577407764304\n📌PlatOrderNo: PI2608036v1gfqv627q"
)
ILLUN_CARD = (
    "💳 ILLUN-17856761389326\n──────\nUser    │ 9999 (N/A)\nType    │ Add Fund\n"
    "Status  │ ✅ Confirmed\nAmount  │ ₹5,000"
)


def test_system_templates():
    c = classify_system_message(SUCCESS_STATUS, BX, PL)
    assert c.outcome == "SUCCESS"
    assert c.order_ids == ["ILLUN-178531583978389"] and c.plat_order_nos == ["PI2607293hgertk2jua"]
    assert classify_system_message(PENDING_STATUS, BX, PL).outcome == "PENDING"
    assert classify_system_message(FAILED_STATUS, BX, PL).outcome == "FAILED"
    c = classify_system_message(PI_PAID, BX, PL)
    assert c.outcome == "SUCCESS" and c.utrs == ["611532946151"]
    assert classify_system_message(PI_PENDING, BX, PL).outcome == "PENDING"
    assert classify_system_message(ACK, BX, PL).outcome == "ACK"
    assert classify_system_message(RECOG, BX, PL).outcome == "PENDING"  # UTR not found yet
    assert classify_system_message(NOTICE, BX, PL).outcome == "IRRELEVANT"
    assert classify_system_message("Order not found", BX, PL).outcome == "NOT_FOUND"
    assert classify_system_message(ALREADY, BX, PL).outcome == "SUCCESS"
    assert classify_system_message(ILLUN_CARD, BX, PL).outcome == "SUCCESS"


def test_human_phrases():
    for t in [
        "Success",
        "success",
        "Successful",
        "Done ✔️",
        "Both done ✔️",
        "ok done",
        "payment confirmed",
        "Confirmed sir",
        "received",
    ]:
        assert classify_human_message(t, BX, PL).outcome == "SUCCESS", t
    for t in ["checking", "checking team", "let us check", "pls wait", "Checking，sir"]:
        assert classify_human_message(t, BX, PL).outcome == "CHECKING", t
    assert (
        classify_human_message(
            "Dear sir, could you please provide a PDF and video transaction record so that we can verify them again?",
            BX,
            PL,
        ).outcome
        == "NEED_MORE_EVIDENCE"
    )
    assert classify_human_message("not ours", BX, PL).outcome == "FAILED"
    assert classify_human_message("Reversed  BXWD-29532-56856", BX, PL).outcome == "FAILED"
    assert classify_human_message("/pi ILLUN-178617416481031", BX, PL).outcome == "IRRELEVANT"
    assert classify_human_message("Any update", BX, PL).outcome == "UNKNOWN"
    assert classify_human_message("we have not received the payment, done checking", BX, PL).outcome != "SUCCESS"


def test_authority_and_rule():
    kw = dict(system_bot_ids=set(), system_bot_usernames={"betixpay_cs_bot"}, in_betix_chat=True, is_member=True)
    assert authority_for_sender(sender_id=1, username="betixpay_cs_bot", is_bot=True, **kw) == "system_bot"
    assert authority_for_sender(sender_id=5001, username=None, is_bot=False, **kw) == "group_member"
    assert authority_for_sender(sender_id=2, username="Wendy", is_bot=False, **kw) == "group_member"
    assert authority_for_sender(sender_id=3, username="random", is_bot=False, **dict(kw, is_member=False)) == "unknown"
    assert (
        authority_for_sender(sender_id=3, username="random", is_bot=False, **dict(kw, in_betix_chat=False)) == "unknown"
    )
    assert is_confirmed(mode="strict", system_success=True, reviewer_success=False) is False
    assert is_confirmed(mode="strict", system_success=True, reviewer_success=True) is True
    assert is_confirmed(mode="monitor", system_success=True, reviewer_success=False) is True
    assert is_confirmed(mode="monitor", system_success=False, reviewer_success=False) is False
