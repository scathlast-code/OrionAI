"""Створення рядка сесії службового акаунта Telegram для ньюзруму.

Запускайте ЛИШЕ на своєму сервері в терміналі (не в чаті з ботом):
    docker compose run --rm -it ai python -m plugins.newsroom.make_session
Потрібні TELEGRAM_API_ID і TELEGRAM_API_HASH з https://my.telegram.org → API development tools.
Отриманий рядок впишіть у .env як TELEGRAM_SESSION=… і перезапустіть: docker compose up -d ai
Рядок сесії = повний доступ до акаунта. Нікому не пересилайте, не комітьте в Git. Краще окремий акаунт із 2FA.
"""
import asyncio
import os


async def main():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id = int(os.getenv("TELEGRAM_API_ID") or input("API ID: "))
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("API hash: ")
    async with TelegramClient(StringSession(), api_id, api_hash, device_model="OrionAI Newsroom") as client:
        me = await client.get_me()
        print(f"\nУвійшли як {me.first_name} (id {me.id}). Рядок сесії — у .env як TELEGRAM_SESSION:\n")
        print(client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
