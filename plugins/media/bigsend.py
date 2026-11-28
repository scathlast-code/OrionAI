"""🚀 Великі файли до 2 ГБ: надсилання й отримання через MTProto від імені того самого бота (Telethon).

Звичайний Bot API дає боту надіслати лише 50 МБ і отримати 20 МБ. Інші боти, що віддають по 1–2 ГБ, працюють
через MTProto — так само робимо й ми: тим самим токеном бота, але через пряме з'єднання з Telegram.
Потрібні TELEGRAM_API_ID і TELEGRAM_API_HASH (my.telegram.org → API development tools) — ті самі, що й для новинарні.
Сесія бота зберігається в теці плагіна, щоб не входити щоразу заново.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

BIG_LIMIT = 2000 * 1024 * 1024  # межа Telegram для ботів через MTProto


class Big:
    def __init__(self, folder: Path, token: str):
        self.session_path = Path(folder) / "mtproto.session.txt"
        self.token = token
        self._client = None
        self._lock = asyncio.Lock()
        self.error = ""

    def ready(self) -> bool:
        if not (self.token and os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_HASH")):
            return False
        try:
            import telethon  # noqa: F401
        except ImportError:
            return False
        return True

    async def client(self):
        async with self._lock:
            if self._client and self._client.is_connected():
                return self._client
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            saved = self.session_path.read_text().strip() if self.session_path.is_file() else ""
            c = TelegramClient(StringSession(saved), int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"],
                               receive_updates=False, device_model="OrionAI Media", flood_sleep_threshold=60)
            await c.start(bot_token=self.token)
            s = c.session.save()
            if s != saved:
                self.session_path.write_text(s)
                self.session_path.chmod(0o600)
            self._client = c
            return c

    async def resolve(self, username: str):
        """@нікнейм -> (ID, ім'я) через Telegram (бот може знайти будь-кого з публічним нікнеймом) або None."""
        try:
            from telethon.tl.functions.contacts import ResolveUsernameRequest
            c = await self.client()
            r = await c(ResolveUsernameRequest(username.lstrip("@")))
            u = next((x for x in r.users if getattr(x, "id", None)), None)
            if not u or getattr(u, "bot", False):
                return None
            return u.id, " ".join(filter(None, [getattr(u, "first_name", ""), getattr(u, "last_name", "")])) or username
        except Exception:  # noqa: BLE001 — немає такого нікнейму чи MTProto недоступний
            return None

    async def send(self, chat: int, path: Path, caption: str = "", progress=None, video=None, html=False, thumb=None):
        """Надсилає файл (до 2 ГБ): документом або, якщо video (dict з width/height/duration), — відео з плеєром.
        progress(done, total) — для показу відсотків."""
        c = await self.client()
        peer = await c.get_input_entity(chat)  # бот може звернутися до людини, яка йому писала
        attrs = None
        if video is not None:
            from telethon.tl.types import DocumentAttributeVideo
            attrs = [DocumentAttributeVideo(duration=int(video.get("duration") or 0), w=int(video.get("width") or 0),
                                            h=int(video.get("height") or 0), supports_streaming=True)]
        await c.send_file(peer, str(path), caption=caption[:4000 if html else 1000], force_document=video is None,
                          supports_streaming=video is not None, part_size_kb=512, progress_callback=progress,
                          attributes=attrs, parse_mode="html" if html else None, thumb=str(thumb) if thumb else None)

    async def download(self, chat: int, message_id: int, dest: Path, progress=None) -> Path:
        """Завантажує вкладення з повідомлення людини в чаті з ботом (більші за 20 МБ)."""
        c = await self.client()
        peer = await c.get_input_entity(chat)
        msg = await c.get_messages(peer, ids=message_id)
        if not msg or not msg.media:
            raise ValueError("повідомлення з файлом не знайдено")
        out = await c.download_media(msg, file=str(dest), progress_callback=progress)
        return Path(out)


class Progress:
    """Одне повідомлення з відсотками, що оновлюється не частіше ніж раз на 6 с."""

    def __init__(self, bot, chat, title):
        self.bot, self.chat, self.title = bot, chat, title
        self.msg = None
        self.last = 0.0

    async def start(self):
        self.msg = await self.bot.send_message(self.chat, f"{self.title}… 0%")

    async def __call__(self, done, total):
        if not self.msg or time.monotonic() - self.last < 6 or not total:
            return
        self.last = time.monotonic()
        try:
            await self.bot.edit_message_text(f"{self.title}… {done * 100 // total}% ({done / 1048576:.0f} з {total / 1048576:.0f} МБ)",
                                             chat_id=self.chat, message_id=self.msg.message_id)
        except Exception:  # noqa: BLE001
            pass

    async def done(self):
        if self.msg:
            try:
                await self.bot.delete_message(self.chat, self.msg.message_id)
            except Exception:  # noqa: BLE001
                pass
