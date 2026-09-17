"""Bank statement / payment video in the Betix group, and the intake race.

BETIX_EXTRA_EVIDENCE_POLICY
  always     -> sent right after the screenshot, as REPLIES to it (default)
  on_request -> kept in the case until Betix asks for more evidence, then sent as replies
  never      -> never leave the case
They are never sent standalone and never twice. A human POSTING a file in the group is sharing
evidence, not asking for it. And a batch of forwards must never lose evidence to a case-id collision.
"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.cases import manager
from app.cases.correlation import attach_message
from app.config import get_settings
from app.db.models import Case
from app.db.repository import get_case, list_evidence
from app.telegram import input_bot
from app.telegram.betix_monitor import handle_group_message
from app.telegram.confirmation import classify_human_message
from tests.conftest import make_group_msg, make_input
from tests.test_authority import group_only  # noqa: F401  (fixture: membership directory, no allowlist)
from tests.test_flow import GOOD, MOBILE, run_until_ready

ROOT = 501  # message id of the screenshot post in FakePoster
BOILERPLATE = (
    "Edit, Sign and Share PDF files on the go. Download the Acrobat Reader app: https://adobeacrobat.app.link/x"
)


@pytest.fixture
def policy(monkeypatch):
    def set_(value):
        monkeypatch.setenv("BETIX_EXTRA_EVIDENCE_POLICY", value)
        from app.config import reset_settings_cache

        reset_settings_cache()

    return set_


@pytest.fixture
def poster_hook(fake_poster):
    """Make the monitor / worker use the recording poster."""
    manager.set_poster_factory(lambda: fake_poster)
    yield fake_poster
    manager.set_poster_factory(None)


async def posted(db, order_search, poster):
    case_id, _ = await run_until_ready(db, order_search)  # screenshot + mobile + statement + video
    async with db.session_scope() as s:
        assert await manager.post_case_to_betix(s, case_id, poster) == "posted"
    return case_id


async def posted_without_extras(db, order_search, poster, monkeypatch):
    """Two-item mode (REQUIRE_ALL_EVIDENCE=false): the only way a case reaches Betix before its statement."""
    monkeypatch.setenv("REQUIRE_ALL_EVIDENCE", "false")
    from app.config import reset_settings_cache

    reset_settings_cache()
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", MOBILE))
        case_id = r.case.case_id
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        assert await manager.post_case_to_betix(s, case_id, poster) == "posted"
    return case_id


def types(poster):
    return [m[0] for m in poster.media]


# ------------------------------------------------------------------ always (default)
async def test_default_policy_is_always():
    assert get_settings().betix_extra_evidence_policy == "always"


async def test_always_sends_statement_and_video_as_replies_to_the_screenshot(
    db, fake_bot, fake_ai, order_search, no_download, fake_poster
):
    await posted(db, order_search, fake_poster)
    assert types(fake_poster) == ["payment_screenshot", "bank_statement", "payment_video"]
    assert fake_poster.media_replies[0] == ("payment_screenshot", None)  # the anchor
    assert fake_poster.media_replies[1:] == [("bank_statement", ROOT), ("payment_video", ROOT)]
    assert fake_poster.media[1][1] == "Password:- lata2812"  # statement password travels with it
    assert fake_poster.texts == []  # still no details text


async def test_always_forwards_a_late_statement_as_a_reply(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook, monkeypatch
):
    case_id = await posted_without_extras(db, order_search, poster_hook, monkeypatch)
    assert types(poster_hook) == ["payment_screenshot"]
    r = await input_bot.ingest(make_input(3, "document", "Password:- 4321"))
    assert r["case"].case_id == case_id and r["late_post"] is True
    from app.workers.tasks import late_evidence_job

    assert await late_evidence_job({}, case_id) == 1
    assert poster_hook.media[-1] == ("bank_statement", "Password:- 4321")
    assert poster_hook.media_replies[-1] == ("bank_statement", ROOT)
    assert await late_evidence_job({}, case_id) == 0  # never twice


# ------------------------------------------------------------------ on_request
async def test_on_request_waits_until_betix_asks(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook, policy, group_only
):
    policy("on_request")
    await posted(db, order_search, poster_hook)
    assert types(poster_hook) == ["payment_screenshot"]
    async with db.session_scope() as s:
        r = await handle_group_message(
            s,
            make_group_msg(
                700, "please share bank statement and video", sender_id=5001, sender_username="Wendy", reply_to=ROOT
            ),
        )
    assert r["action"] == "need_more_evidence"
    assert types(poster_hook) == ["payment_screenshot", "bank_statement", "payment_video"]
    assert all(rt == ROOT for _, rt in poster_hook.media_replies[1:])
    note = [t for _, t in fake_bot.sent if "more evidence" in t][-1]
    assert "Sent 2 file(s)" in note
    async with db.session_scope() as s:  # asked again -> nothing re-sent
        await handle_group_message(
            s, make_group_msg(701, "send statement pls", sender_id=5001, sender_username="Wendy", reply_to=ROOT)
        )
    assert len(poster_hook.media) == 3


async def test_on_request_reports_missing_files_then_forwards_them_on_arrival(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook, policy, group_only, monkeypatch
):
    policy("on_request")
    case_id = await posted_without_extras(db, order_search, poster_hook, monkeypatch)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s,
            make_group_msg(
                700, "Please send the bank statement", sender_id=5001, sender_username="Wendy", reply_to=ROOT
            ),
        )
    assert r["action"] == "need_more_evidence" and len(poster_hook.media) == 1
    note = [t for _, t in fake_bot.sent if "more evidence" in t][-1]
    assert "Not on file: bank statement, payment video" in note
    # the admin now sends the statement to the bot -> forwarded as a reply to the screenshot
    r = await input_bot.ingest(make_input(3, "document", "Password:- 4321"))
    assert r["case"].case_id == case_id and r["late_post"] is True
    from app.workers.tasks import late_evidence_job

    assert await late_evidence_job({}, case_id) == 1
    assert poster_hook.media[-1] == ("bank_statement", "Password:- 4321")
    assert poster_hook.media_replies[-1] == ("bank_statement", ROOT)


# ------------------------------------------------------------------ never
async def test_never_keeps_files_internal(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook, policy, group_only
):
    policy("never")
    await posted(db, order_search, poster_hook)
    async with db.session_scope() as s:
        r = await handle_group_message(
            s,
            make_group_msg(700, "please share bank statement", sender_id=5001, sender_username="Wendy", reply_to=ROOT),
        )
    assert r["action"] == "need_more_evidence" and types(poster_hook) == ["payment_screenshot"]
    r = await input_bot.ingest(make_input(9, "video"))
    assert r["late_post"] is False
    from app.workers.tasks import late_evidence_job

    assert await late_evidence_job({}, r["case"].case_id) == 0


# ------------------------------------------------------------------ a human posting a file is not a request
def test_human_media_post_is_not_a_request_for_evidence(env):
    s = get_settings()
    kw = dict(betex_pattern=s.betex_order_id_pattern, plat_pattern=s.plat_order_pattern)
    assert classify_human_message(BOILERPLATE, has_media=True, **kw).outcome == "IRRELEVANT"
    assert classify_human_message(BOILERPLATE, has_media=False, **kw).outcome != "NEED_MORE_EVIDENCE"
    assert classify_human_message("Video from NV", has_media=True, **kw).outcome == "IRRELEVANT"
    assert classify_human_message("please share the bank statement", **kw).outcome == "NEED_MORE_EVIDENCE"
    assert (
        classify_human_message("Success", has_media=True, **kw).outcome == "SUCCESS"
    )  # captioned confirmation still counts


async def test_admin_file_posts_in_group_do_not_raise_evidence_alerts(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook, group_only
):
    await posted(db, order_search, poster_hook)
    async with db.session_scope() as s:
        r1 = await handle_group_message(
            s, make_group_msg(700, BOILERPLATE, sender_id=5004, sender_username="fantasyAdda_support", has_media=True)
        )
        r2 = await handle_group_message(
            s,
            make_group_msg(701, "Video from NV", sender_id=5004, sender_username="fantasyAdda_support", has_media=True),
        )
    assert r1["action"] in ("irrelevant", "unlinked") and r2["action"] in ("irrelevant", "unlinked")
    assert not any("more evidence" in t for _, t in fake_bot.sent)


# ------------------------------------------------------------------ the intake race
async def test_batch_of_forwards_creates_exactly_one_case(db):
    results = await asyncio.gather(
        input_bot.ingest(make_input(1, "photo")),
        input_bot.ingest(make_input(2, "text", MOBILE)),
        input_bot.ingest(make_input(3, "document", "Password:- 1")),
        input_bot.ingest(make_input(4, "video")),
    )
    ids = {r["case"].case_id for r in results}
    assert len(ids) == 1 and sum(1 for r in results if r["created"]) == 1
    async with db.session_scope() as s:
        assert len((await s.execute(select(Case))).scalars().all()) == 1
        ev = await list_evidence(s, ids.pop())
        assert sorted(e.type for e in ev) == ["bank_statement", "payment_screenshot", "payment_video"]


async def test_case_id_collision_is_retried(db, monkeypatch):
    real = input_bot.attach_message
    calls = {"n": 0}

    async def flaky(session, inp):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT INTO cases", {}, Exception("duplicate key value violates unique constraint"))
        return await real(session, inp)

    monkeypatch.setattr(input_bot, "attach_message", flaky)
    r = await input_bot.ingest(make_input(1, "photo"))
    assert r["created"] and calls["n"] == 2


# ------------------------------------------------------------------ the statement password travels with the PDF
async def test_password_in_caption_rides_on_the_pdf(db, fake_bot, fake_ai, order_search, no_download, fake_poster):
    await posted(db, order_search, fake_poster)  # submit_four_messages captions "Password:- lata2812"
    assert ("bank_statement", "Password:- lata2812") in fake_poster.media
    async with db.session_scope() as s:
        c = await get_case(s, (await s.execute(select(Case))).scalars().first().case_id)
        assert await manager.post_statement_password(s, c, fake_poster) is False  # already in the caption
    assert fake_poster.texts == []


async def lock_statement(db, case_id, tmp_path, password):
    """Point the case's statement at a really password-protected PDF (what the operator sent)."""
    from tests.test_file_detection import make_pdf

    locked = make_pdf(tmp_path / f"{case_id}.pdf", password)
    async with db.session_scope() as s:
        for e in await list_evidence(s, case_id):
            if e.type == "bank_statement":
                e.local_path = str(locked)


async def test_password_sent_later_is_forwarded_once_as_a_reply(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook, tmp_path
):
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", MOBILE))
        await attach_message(s, make_input(3, "document"))  # no caption, password unknown
        await attach_message(s, make_input(4, "video"))
        case_id = r.case.case_id
    order_search(GOOD)
    async with db.session_scope() as s:
        assert await manager.process_case(s, case_id, force=True) == "ready"
        assert await manager.post_case_to_betix(s, case_id, poster_hook) == "posted"
    assert ("bank_statement", None) in poster_hook.media
    await lock_statement(db, case_id, tmp_path, "lata2812")
    r = await input_bot.ingest(make_input(5, "text", "lata2812"))  # the bare password, later
    assert r["case"].case_id == case_id and r["case"].statement_password == "lata2812" and r["late_post"] is True
    from app.workers.tasks import late_evidence_job

    assert await late_evidence_job({}, case_id) == 1
    assert poster_hook.texts == [("Password:- lata2812", ROOT)]  # a reply to the screenshot
    assert await late_evidence_job({}, case_id) == 0  # once


async def test_never_policy_keeps_the_password_too(
    db, fake_bot, fake_ai, order_search, no_download, poster_hook, policy, tmp_path
):
    policy("never")
    case_id = await posted(db, order_search, poster_hook)
    async with db.session_scope() as s:
        (await get_case(s, case_id)).statement_password = None  # pretend it was never known
    await lock_statement(db, case_id, tmp_path, "4321")
    r = await input_bot.ingest(make_input(9, "text", "Password:- 4321"))
    assert r["late_post"] is False
    from app.workers.tasks import late_evidence_job

    assert await late_evidence_job({}, r["case"].case_id) == 0 and poster_hook.texts == []
