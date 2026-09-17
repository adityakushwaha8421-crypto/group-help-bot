"""Alerts and confirmations go to EVERY chat in ADMIN_NOTIFY_CHAT_ID - each admin gets their own copy, still only
once per (case, kind), and one admin's failed delivery never costs the other theirs."""

from app.telegram.notifications import notify_admin


def two_admins(monkeypatch, value="111,222"):
    monkeypatch.setenv("ADMIN_NOTIFY_CHAT_ID", value)
    from app.config import get_settings, reset_settings_cache

    reset_settings_cache()
    return get_settings()


def test_the_setting_takes_a_list(monkeypatch):
    s = two_admins(monkeypatch, " 111 , 222 ,")
    assert s.notify_chats == [111, 222] and s.notify_chat == 111
    assert two_admins(monkeypatch, "").notify_chats == []


async def test_every_admin_gets_the_alert_once(db, fake_bot, monkeypatch):
    two_admins(monkeypatch)
    async with db.session_scope() as s:
        row = await notify_admin(s, kind="manual_review", text="⚠️ check", dedupe_suffix="x")
        assert row is not None and row.sent and row.chat_id == "111,222"
        assert await notify_admin(s, kind="manual_review", text="⚠️ check", dedupe_suffix="x") is None  # once
    assert fake_bot.sent == [(111, "⚠️ check"), (222, "⚠️ check")]


async def test_one_failed_delivery_does_not_stop_the_other(db, fake_bot, monkeypatch):
    two_admins(monkeypatch)
    real = fake_bot.send_message

    async def flaky(chat_id, text, **kw):
        if chat_id == 111:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        return await real(chat_id, text, **kw)

    fake_bot.send_message = flaky
    async with db.session_scope() as s:
        row = await notify_admin(s, kind="payment_confirmed", text="✅ done")
        assert row.sent and "111: Forbidden" in row.error
    assert fake_bot.sent == [(222, "✅ done")]
