#!/usr/bin/env python3
"""Interactively create a Telethon StringSession without writing credentials to disk."""

from __future__ import annotations

import getpass

from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main() -> None:
    print("Telegram MTProto StringSession generator")
    api_id = int(input("api_id: ").strip())
    api_hash = getpass.getpass("api_hash: ").strip()
    phone = input("phone number (international format): ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        client.start(phone=phone)
        session = client.session.save()
    print("\nStringSession (store it as a secret; it grants access to your Telegram account):")
    print(session)


if __name__ == "__main__":
    main()
