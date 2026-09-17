"""/search: screenshot + identifier -> Illunise order id. Read-only, no case, nothing posted."""

import pytest
from sqlalchemy import select

from app.db.models import Case
from app.telegram import order_search as osx
from tests.conftest import make_input
from tests.test_flow import GOOD, MOBILE

CHAT, USER = 111, 111


@pytest.fixture(autouse=True)
def clean_sessions(tmp_path):
    osx._sessions.clear()

    async def fake_download(file_id):
        p = tmp_path / f"{file_id}.jpg"
        p.write_bytes(b"\xff\xd8fake")
        return p

    osx.set_downloader(fake_download)
    yield
    osx.set_downloader(None)
    osx._sessions.clear()


def test_session_lifecycle_and_identifier_parsing():
    assert osx.active(CHAT) is None
    sess = osx.start(CHAT, USER)
    assert osx.active(CHAT) is sess and osx.missing(sess) == ["payment screenshot", "mobile number"]
    assert osx.set_identifier(sess, "+91 77339 31348") and sess.identifier == "7733931348" and sess.is_mobile
    sess2 = osx.start(CHAT, USER, "REG123456")  # /search REG123456
    assert sess2.identifier == "REG123456" and not sess2.is_mobile
    assert not osx.set_identifier(sess2, "please check")  # chatter is not an identifier
    assert not osx.set_identifier(sess2, "/status")
    assert osx.cancel(CHAT) and osx.active(CHAT) is None and not osx.cancel(CHAT)


def test_status_lines():
    sess = osx.start(CHAT, USER)
    assert [l.split("  ·  ")[0] for l in osx.status_lines(sess)] == [
        "⏳ Payment screenshot — please send it",
        "⏳ Mobile number — please send it (a registration number or UTR works too)",
    ]
    osx.take(sess, make_input(1, "photo"))
    osx.take(sess, make_input(2, "text", MOBILE))
    assert osx.status_lines(sess) == ["✅ Payment screenshot", f"✅ Mobile number · <code>{MOBILE}</code>"]
    assert osx.missing(sess) == []


def test_take_ignores_non_screenshot_files_and_keeps_first_identifier():
    sess = osx.start(CHAT, USER, MOBILE)
    osx.take(sess, make_input(3, "document"))  # a PDF is not a screenshot
    assert sess.screenshot_file_id is None
    osx.take(sess, make_input(4, "text", "9111111111"))  # identifier already set: kept
    assert sess.identifier == MOBILE
    osx.take(sess, make_input(5, "photo", "UTR 611532946151"))
    assert sess.screenshot_file_id == "photo5" and sess.text_extraction.utr.value == "611532946151"


async def test_search_by_mobile_returns_the_order_id(db, fake_ai, order_search):
    order_search(GOOD)
    sess = osx.start(CHAT, USER)
    osx.take(sess, make_input(1, "photo"))
    osx.take(sess, make_input(2, "text", MOBILE))
    reply = await osx.run(sess)
    assert reply.startswith("🔎 <b>Order search result</b>")
    assert f"👤 Mobile: <code>{MOBILE}</code>" in reply and "💰 Amount: ₹6,499.92" in reply
    assert "🧾 <b>Order ID</b>\n✅ <code>ILLUN-178621243657290</code> — match <b>" in reply
    assert "🎯 <b>Match found</b>" in reply
    assert fake_ai.calls == 1
    async with db.session_scope() as s:  # read-only: no case was created
        assert (await s.execute(select(Case))).scalars().all() == []


async def test_search_by_registration_number_still_finds_the_order(db, fake_ai, order_search):
    holder = order_search(GOOD)
    sess = osx.start(CHAT, USER, "REG123456")
    osx.take(sess, make_input(1, "photo"))
    reply = await osx.run(sess)
    assert holder["query"] == "REG123456"  # the panel is searched with the identifier as typed
    assert "ILLUN-178621243657290" in reply


async def test_search_with_no_orders(db, fake_ai, order_search):
    order_search([])
    sess = osx.start(CHAT, USER, MOBILE)
    osx.take(sess, make_input(1, "photo"))
    reply = await osx.run(sess)
    assert f"❌ No orders found in Illunise for <code>{MOBILE}</code>" in reply


async def test_search_reports_no_match_with_closest_candidate(db, fake_ai, order_search):
    order_search([{**GOOD[0], "amount": 100.0}])
    sess = osx.start(CHAT, USER, MOBILE)
    osx.take(sess, make_input(1, "photo"))
    reply = await osx.run(sess)
    assert "<b>No order matched</b>" in reply and "<i>Closest:</i>\n▫️ <code>ILLUN-178621243657290</code>" in reply


async def test_search_ambiguous_lists_candidates(db, fake_ai, order_search):
    a = dict(GOOD[0])
    b = {
        **GOOD[0],
        "illunise_order_id": "1002",
        "betex_order_id": "ILLUN-178621243657291",
        "order_time": "2026-09-10 19:31:30",
        "utr": None,
    }
    a["utr"] = None
    order_search([a, b])
    sess = osx.start(CHAT, USER, MOBILE)
    osx.take(sess, make_input(1, "photo"))
    reply = await osx.run(sess)
    assert (
        "⚠️ <b>More than one order fits</b>" in reply
        and "ILLUN-178621243657290" in reply
        and "ILLUN-178621243657291" in reply
    )


async def test_search_admin_error_is_reported_not_raised(db, fake_ai):
    from app.admin.browser import LoginFailed
    from app.cases import manager

    async def boom(query, amount=None, when=None):
        raise LoginFailed("bad credentials")

    manager.set_order_search(boom)
    try:
        sess = osx.start(CHAT, USER, MOBILE)
        osx.take(sess, make_input(1, "photo"))
        assert "❌ Admin login failed" in await osx.run(sess)
    finally:
        manager.set_order_search(None)
