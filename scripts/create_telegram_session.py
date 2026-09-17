"""Generate a Telethon StringSession for user-mode posting/monitoring.
You enter your phone/code yourself; the script prints the session string for TELEGRAM_SESSION.
Never commit the string. Run:  python scripts/create_telegram_session.py
"""

import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(os.environ.get("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: "))
    api_hash = os.environ.get("TELEGRAM_API_HASH") or input("TELEGRAM_API_HASH: ")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nTELEGRAM_SESSION=" + client.session.save())
        me = await client.get_me()
        print(f"Authorized as {me.first_name} (@{me.username})")


if __name__ == "__main__":
    asyncio.run(main())
