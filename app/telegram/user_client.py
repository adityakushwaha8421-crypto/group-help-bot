"""Optional Telethon user-session client. Needed only when BETIX_POST_MODE=user or BETIX_MONITOR_MODE=user
(e.g. the group forbids bots, or the bot cannot read historical/other members' messages).
Session string is generated once with scripts/create_telegram_session.py and stored in TELEGRAM_SESSION."""

from __future__ import annotations

from app.config import get_settings

_client = None


def user_mode_configured() -> bool:
    s = get_settings()
    return bool(s.telegram_api_id and s.telegram_api_hash and s.telegram_session)


async def get_user_client():
    global _client
    if _client is not None:
        return _client
    s = get_settings()
    if not user_mode_configured():
        raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION must be set for user mode")
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    _client = TelegramClient(StringSession(s.telegram_session), s.telegram_api_id, s.telegram_api_hash)
    await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError("TELEGRAM_SESSION is not authorized; regenerate it with scripts/create_telegram_session.py")
    return _client
