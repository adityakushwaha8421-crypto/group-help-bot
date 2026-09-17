"""
Telegram Session Generator
--------------------------
Generates a Telethon StringSession using your Telegram API ID and API Hash.

Install:
    pip install telethon

Run:
    python telegram_session_generator.py

IMPORTANT:
- Use your own Telegram account.
- Never share your API hash, phone number, OTP, 2FA password, or generated session string.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def main():
    print("=" * 60)
    print("Telegram Session String Generator")
    print("=" * 60)

    api_id = input("Enter Telegram API ID: ").strip()
    api_hash = input("Enter Telegram API Hash: ").strip()

    if not api_id.isdigit():
        raise ValueError("API ID must be a number.")

    if not api_hash:
        raise ValueError("API Hash cannot be empty.")

    phone = input("Enter your Telegram phone number (e.g. +919876543210): ").strip()

    print("\nStarting Telegram login...")
    print("Telegram will send an OTP to your account.")
    print("If 2-Step Verification is enabled, you will also be asked for your password.\n")

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        client.start(phone=phone)

        session_string = client.session.save()

        print("\n" + "=" * 60)
        print("SUCCESS — SESSION STRING GENERATED")
        print("=" * 60)
        print("\nYour session string:\n")
        print(session_string)
        print("\n" + "=" * 60)
        print("KEEP THIS SECRET.")
        print("Anyone with this session string may be able to access")
        print("your Telegram account. Do NOT send it to anyone.")
        print("=" * 60)

        # Optional local backup
        save = input("\nSave it to telegram_session.txt? (y/n): ").strip().lower()
        if save == "y":
            with open("telegram_session.txt", "w", encoding="utf-8") as f:
                f.write(session_string)
            print("Saved as telegram_session.txt")


if __name__ == "__main__":
    main()
