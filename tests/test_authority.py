"""Who may confirm a payment: the Betix support GROUP is the authority, not a list of names.

- any human member of the configured Betix group saying "success" -> HUMAN confirmation
- betixpay_cs_bot -> SYSTEM confirmation
- our own bot/account -> never a confirmation
- a non-member, or a message outside the group -> ignored
- a "success" that cannot be tied to a case -> ignored
"""

import pytest

from app.cases import manager
from app.db.models import CaseStatus
from app.db.repository import get_case, list_verification_events
from app.telegram import betix_monitor
from app.telegram.betix_monitor import handle_group_message, register_self, set_membership_checker
from app.telegram.confirmation import authority_for_sender
from tests.conftest import make_group_msg
from tests.test_flow import SYS_OK, run_until_ready

GROUP = -1009999
OTHER_CHAT = -1005555

MEMBERS = {5001: "Wendy", 5002: "Queenie", 5003: "Anne", 5004: "some_new_agent", 5005: None}
NON_MEMBERS = {7001: "outsider"}
OUR_BOT = (9000, "Chatexportfa_bot")


@pytest.fixture
def group_only(env, monkeypatch):
    """No reviewer allowlist; membership answered from a fake member directory."""
    monkeypatch.setenv("AUTHORIZED_BETIX_REVIEWER_IDS", "")
    monkeypatch.setenv("AUTHORIZED_BETIX_REVIEWER_USERNAMES", "")
    monkeypatch.setenv("BETIX_SYSTEM_BOT_USERNAMES", "betixpay_cs_bot")
    from app.config import reset_settings_cache

    reset_settings_cache()

    async def directory(chat_id: int, user_id: int):
        if chat_id != GROUP:
            return False
        if user_id in MEMBERS:
            return True
        if user_id in NON_MEMBERS:
            return False
        return None  # unknown to the directory: fall back to message origin

    set_membership_checker(directory)
    register_self(*OUR_BOT)
    yield
    set_membership_checker(None)
    betix_monitor._self_ids.clear()
    betix_monitor._self_usernames.clear()


# ---------------------------------------------------------------- pure authority resolution
def test_authority_resolution():
    kw = dict(
        system_bot_ids=set(),
        system_bot_usernames={"betixpay_cs_bot"},
        our_ids={9000},
        our_usernames={"chatexportfa_bot"},
    )
    for name in ("Wendy", "Queenie", "Anne", "anyone_else"):
        assert (
            authority_for_sender(sender_id=1, username=name, is_bot=False, in_betix_chat=True, is_member=True, **kw)
            == "group_member"
        )
    # membership could not be checked but the message came from the group -> still a member
    assert (
        authority_for_sender(sender_id=1, username=None, is_bot=False, in_betix_chat=True, is_member=None, **kw)
        == "group_member"
    )
    assert (
        authority_for_sender(sender_id=1, username="x", is_bot=False, in_betix_chat=True, is_member=False, **kw)
        == "unknown"
    )
    assert (
        authority_for_sender(sender_id=1, username="Wendy", is_bot=False, in_betix_chat=False, is_member=True, **kw)
        == "unknown"
    )
    assert (
        authority_for_sender(
            sender_id=2, username="betixpay_cs_bot", is_bot=True, in_betix_chat=True, is_member=True, **kw
        )
        == "system_bot"
    )
    assert (
        authority_for_sender(
            sender_id=9000, username="Chatexportfa_bot", is_bot=True, in_betix_chat=True, is_member=True, **kw
        )
        == "self"
    )
    assert (
        authority_for_sender(sender_id=3, username="other_bot", is_bot=True, in_betix_chat=True, is_member=True, **kw)
        == "unknown"
    )
    # an OPTIONAL allowlist narrows the group down; empty (default) means everyone
    assert (
        authority_for_sender(
            sender_id=1,
            username="Wendy",
            is_bot=False,
            in_betix_chat=True,
            is_member=True,
            reviewer_usernames={"queenie"},
            **kw,
        )
        == "unknown"
    )
    assert (
        authority_for_sender(
            sender_id=1,
            username="Queenie",
            is_bot=False,
            in_betix_chat=True,
            is_member=True,
            reviewer_usernames={"queenie"},
            **kw,
        )
        == "group_member"
    )


# ---------------------------------------------------------------- through the monitor
async def posted_case(db, order_search, fake_poster):
    case_id, _ = await run_until_ready(db, order_search)
    async with db.session_scope() as s:
        await manager.post_case_to_betix(s, case_id, fake_poster)
    return case_id


@pytest.mark.parametrize(
    "uid,name", [(5001, "Wendy"), (5002, "Queenie"), (5003, "Anne"), (5004, "some_new_agent"), (5005, None)]
)
async def test_any_group_member_success_is_human_confirmation(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, group_only, uid, name
):
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(700, "success", sender_id=uid, sender_username=name, reply_to=501)
        )
        assert r["authority"] == "group_member" and r["action"] == "partial"  # strict: system bot still needed
        c = await get_case(s, case_id)
        assert c.reviewer_confirmed_at is not None and c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value
        events = await list_verification_events(s, case_id)
        assert events[-1].event_type == "HUMAN_SUCCESS"
        # then the system bot confirms -> VERIFIED
        r2 = await handle_group_message(
            s, make_group_msg(701, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r2["authority"] == "system_bot" and r2["action"] == "verified"


async def test_confirmation_phrases(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, group_only, monkeypatch
):
    monkeypatch.setenv("CONFIRMATION_MODE", "monitor")
    from app.config import reset_settings_cache

    reset_settings_cache()
    from app.config import get_settings
    from app.telegram.confirmation import classify_human_message

    st = get_settings()
    for phrase in [
        "success",
        "successful",
        "confirmed",
        "confirm",
        "done",
        "Done ✔️",
        "payment confirmed",
        "Confirmed ✅",
    ]:
        assert classify_human_message(phrase, st.betex_order_id_pattern, st.plat_order_pattern).outcome == "SUCCESS", (
            phrase
        )
    for phrase in ["checking", "any update", "not ours", "pls wait"]:
        assert classify_human_message(phrase, st.betex_order_id_pattern, st.plat_order_pattern).outcome != "SUCCESS", (
            phrase
        )
    # end to end: a bare "confirm" from any group member verifies in monitor mode
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        root = (await get_case(s, case_id)).betix_root_message_id
        r = await handle_group_message(
            s, make_group_msg(800, "confirm", sender_id=5004, sender_username="agent_x", reply_to=root)
        )
        assert r["action"] == "verified" and r["authority"] == "group_member"


async def test_non_member_success_is_ignored(db, fake_bot, fake_ai, order_search, no_download, fake_poster, group_only):
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        # in the group chat but the membership directory says NOT a member
        r = await handle_group_message(
            s, make_group_msg(710, "success", sender_id=7001, sender_username="outsider", reply_to=501)
        )
        assert r["authority"] == "unknown" and r["action"] == "ignored_unknown_sender"
        # a "success" posted in some OTHER chat, even mentioning the order id
        r2 = await handle_group_message(
            s,
            make_group_msg(
                711, "success ILLUN-178621243657290", sender_id=5001, sender_username="Wendy", chat_id=OTHER_CHAT
            ),
        )
        assert r2["authority"] == "unknown"
        c = await get_case(s, case_id)
        assert c.reviewer_confirmed_at is None and c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value
    assert any("not a Betix group member" in t for _, t in fake_bot.sent)


async def test_system_bot_is_system_confirmation(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, group_only
):
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(720, SYS_OK, sender_id=8445, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r["authority"] == "system_bot" and r["action"] == "partial"
        c = await get_case(s, case_id)
        assert c.system_confirmed_at is not None and c.reviewer_confirmed_at is None
        events = await list_verification_events(s, case_id)
        assert events[-1].event_type == "SYSTEM_SUCCESS"


async def test_our_own_bot_never_confirms(db, fake_bot, fake_ai, order_search, no_download, fake_poster, group_only):
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s,
            make_group_msg(
                730, "success", sender_id=9000, sender_username="Chatexportfa_bot", is_bot=True, reply_to=501
            ),
        )
        assert r["authority"] == "self" and r["action"] == "ignored_self"
        c = await get_case(s, case_id)
        assert c.reviewer_confirmed_at is None and c.system_confirmed_at is None
        events = await list_verification_events(s, case_id)
        assert events[-1].event_type == "SELF_SUCCESS"


async def test_unrelated_success_is_ignored(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, group_only, monkeypatch
):
    monkeypatch.setenv("CONFIRMATION_MODE", "monitor")
    from app.config import reset_settings_cache

    reset_settings_cache()
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        # a member says "success" replying to something that is not ours, no order id, long after our post
        from datetime import timedelta

        from app.utils.timeutil import utcnow

        r = await handle_group_message(
            s,
            make_group_msg(
                740,
                "success",
                sender_id=5001,
                sender_username="Wendy",
                reply_to=123456,
                sent_at=utcnow() + timedelta(hours=3),
            ),
        )
        assert r["action"] == "unlinked"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value and c.reviewer_confirmed_at is None
        # tied to the case by replying to our post -> counts
        r2 = await handle_group_message(
            s, make_group_msg(741, "success", sender_id=5001, sender_username="Wendy", reply_to=501)
        )
        assert r2["action"] == "verified"


# ---------------------------------------------------------------- the OR rule (default mode)
@pytest.fixture
def either_mode(group_only, monkeypatch):
    monkeypatch.setenv("CONFIRMATION_MODE", "either")
    from app.config import reset_settings_cache

    reset_settings_cache()


async def test_either_system_bot_alone_verifies(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, either_mode
):
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(900, SYS_OK, sender_id=8445, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r["authority"] == "system_bot" and r["action"] == "verified"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.VERIFIED.value and c.followup_cancelled
    assert sum(1 for _, t in fake_bot.sent if "PAYMENT CONFIRMED" in t) == 1


async def test_either_any_member_alone_verifies(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, either_mode
):
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s, make_group_msg(910, "done", sender_id=5004, sender_username="whoever", reply_to=501)
        )
        assert r["authority"] == "group_member" and r["action"] == "verified"
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.VERIFIED.value and c.followup_cancelled and "@whoever" in (c.confirmed_by or "")
        # a later system confirmation does not notify twice
        r2 = await handle_group_message(
            s, make_group_msg(911, SYS_OK, sender_username="betixpay_cs_bot", is_bot=True, reply_to=501)
        )
        assert r2["action"] == "terminal"
    assert sum(1 for _, t in fake_bot.sent if "PAYMENT CONFIRMED" in t) == 1


async def test_either_unrelated_or_outsider_does_not_verify(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster, either_mode
):
    case_id = await posted_case(db, order_search, fake_poster)
    async with db.session_scope() as s:
        from datetime import timedelta

        from app.utils.timeutil import utcnow

        r1 = await handle_group_message(
            s, make_group_msg(920, "success", sender_id=7001, sender_username="outsider", reply_to=501)
        )
        r2 = await handle_group_message(
            s,
            make_group_msg(
                921,
                "success",
                sender_id=5001,
                sender_username="Wendy",
                reply_to=424242,
                sent_at=utcnow() + timedelta(hours=2),
            ),
        )
        r3 = await handle_group_message(
            s,
            make_group_msg(
                922, "success", sender_id=9000, sender_username="Chatexportfa_bot", is_bot=True, reply_to=501
            ),
        )
        assert (r1["action"], r2["action"], r3["action"]) == ("ignored_unknown_sender", "unlinked", "ignored_self")
        c = await get_case(s, case_id)
        assert c.status == CaseStatus.WAITING_FOR_CONFIRMATION.value
    assert not any("Payment confirmed" in t for _, t in fake_bot.sent)
