"""Збирачі дописів із каналів, чатів і груп для 📋 списків новинарні.

1. web      — публічні канали через вебпревʼю https://t.me/s/<username>. Без акаунта й без адміністрування,
              лише канали з увімкненим превʼю (більшість публічних каналів). Групи й приватні канали — ні.
2. mtproto  — службовий акаунт Telegram (Telethon). Читає все, на що акаунт підписаний: канали, групи, чати —
              у реальному часі; плюс публічні канали зі списків без підписки. Потрібні TELEGRAM_API_ID,
              TELEGRAM_API_HASH і TELEGRAM_SESSION (рядок сесії, створюється на сервері: python -m plugins.newsroom.make_session).
3. bot      — канали, де бот адміністратор, і групи, куди бот доданий (дописи приходять самі, див. on_channel_post).
"""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import re

import httpx

from .feeds import strip_html

log = logging.getLogger("orion.plugins.newsroom.collect")
UA = "Mozilla/5.0 (compatible; OrionAI-Newsroom/2.1)"


# ==================================================================== web (t.me/s)
POST_RE = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
FWD_RE = re.compile(r'tgme_widget_message_forwarded_from_name"[^>]*href="https://t\.me/([A-Za-z0-9_]+)')
PHOTO_RE = re.compile(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)")
TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
VIEWS_RE = re.compile(r'tgme_widget_message_views">([^<]+)<')
TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')


def count_num(s):
    """«1.2K» -> 1200, «3,4M» -> 3400000."""
    s = (s or "").strip().upper().replace(",", ".").replace(" ", "")
    mult = 1000 if s.endswith("K") else 1_000_000 if s.endswith("M") else 1
    try:
        return int(float(s.rstrip("KM") or 0) * mult)
    except ValueError:
        return 0


def tg_metrics(message):
    """Telethon Message -> перегляди, реакції, пересилання, коментарі."""
    likes = 0
    r = getattr(message, "reactions", None)
    for rc in (getattr(r, "results", None) or []):
        likes += int(getattr(rc, "count", 0) or 0)
    rep = getattr(message, "replies", None)
    return {"views": int(getattr(message, "views", 0) or 0), "likes": likes, "reposts": int(getattr(message, "forwards", 0) or 0),
            "comments": int(getattr(rep, "replies", 0) or 0) if rep else 0}


def parse_preview(page: str):
    """HTML t.me/s/… -> (назва каналу, [{id, text, photo, date}]) від старших до новіших."""
    title = html.unescape(TITLE_RE.search(page).group(1)) if TITLE_RE.search(page) else ""
    marks = list(POST_RE.finditer(page))
    posts = []
    for i, m in enumerate(marks):
        chunk = page[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(page)]
        t = TEXT_RE.search(chunk)
        ph = PHOTO_RE.search(chunk)
        tm = TIME_RE.search(chunk)
        fw = FWD_RE.search(chunk)
        vw = VIEWS_RE.search(chunk)
        posts.append({"id": int(m.group(2)), "text": strip_html(t.group(1)) if t else "",
                      "photo": ph.group(1) if ph else "", "date": tm.group(1) if tm else "", "fwd": fw.group(1).lower() if fw else "",
                      "metrics": {"views": count_num(vw.group(1)) if vw else 0}})
    uniq = {p["id"]: p for p in posts}
    return title, [uniq[k] for k in sorted(uniq)]


class WebCollector:
    def __init__(self):
        self.client = None

    async def _c(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}, trust_env=False)
        return self.client

    async def fetch(self, username: str, after_id: int = 0):
        c = await self._c()
        r = await c.get(f"https://t.me/s/{username}")
        if r.status_code != 200:
            raise RuntimeError(f"t.me/s: HTTP {r.status_code}")
        if "tgme_channel_info" not in r.text and "tgme_widget_message" not in r.text:
            raise RuntimeError("немає вебпревʼю: це група, приватний канал або превʼю вимкнено — потрібен службовий акаунт")
        title, posts = parse_preview(r.text)
        return title, [p for p in posts if p["id"] > after_id]

    async def photo_b64(self, url):
        if not url.startswith("https://"):
            return ""
        c = await self._c()
        r = await c.get(url)
        if r.status_code != 200 or len(r.content) > 8 * 1024 * 1024:
            return ""
        return base64.b64encode(r.content).decode()


# ==================================================================== MTProto (службовий акаунт)
def mtproto_configured() -> bool:
    return bool(os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_HASH") and os.getenv("TELEGRAM_SESSION"))


def chat_kind(entity) -> str:
    from telethon.tl.types import Channel, Chat, User
    if isinstance(entity, Channel):
        return "group" if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False) else "channel"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, User):
        return "chat"
    return "chat"


def fwd_of(message) -> str:
    """Звідки переслано (id каналу у форматі -100…), або ''."""
    f = getattr(message, "fwd_from", None)
    pid = getattr(f, "from_id", None) if f else None
    cid = getattr(pid, "channel_id", None)
    return f"-100{cid}" if cid else ""


class MTProto:
    """Обгортка над Telethon. Усі методи безпечні, якщо Telethon не встановлено або сесію не задано."""

    def __init__(self, on_message=None):
        self.client = None
        self.me = None
        self.error = ""
        self.on_message = on_message      # async fn(chat_id:str, username, title, chat_type, msg_id, text, photo_b64)

    @property
    def ready(self):
        return self.client is not None and self.client.is_connected()

    async def start(self):
        if not mtproto_configured():
            self.error = "не налаштовано (TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION)"
            return False
        try:
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession
        except ImportError:
            self.error = "не встановлено telethon (pip install telethon)"
            return False
        try:
            self.client = TelegramClient(StringSession(os.getenv("TELEGRAM_SESSION")), int(os.getenv("TELEGRAM_API_ID")),
                                         os.getenv("TELEGRAM_API_HASH"), device_model="OrionAI Newsroom", flood_sleep_threshold=60)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                self.error = "сесія недійсна — створіть нову: python -m plugins.newsroom.make_session"
                await self.client.disconnect()
                self.client = None
                return False
            self.me = await self.client.get_me()
            if self.on_message:
                self.client.add_event_handler(self._event, events.NewMessage(incoming=True))
            self.error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self.client = None
            return False

    async def _event(self, event):
        try:
            if event.is_private and not os.getenv("NEWSROOM_READ_PRIVATE"):
                return  # особисті листування за замовчуванням не читаємо
            chat = await event.get_chat()
            text = event.raw_text or ""
            photo = ""
            if not text.strip() and event.photo:
                photo = await self._photo(event.message)
            await self.on_message(str(event.chat_id), getattr(chat, "username", None) or "",
                                  getattr(chat, "title", None) or getattr(chat, "first_name", "") or "",
                                  chat_kind(chat), event.id, text, photo, fwd_of(event.message))
        except Exception:  # noqa: BLE001
            log.exception("mtproto event")

    async def _photo(self, message):
        try:
            data = await self.client.download_media(message, file=bytes)
            if data and len(data) <= 8 * 1024 * 1024:
                return base64.b64encode(data).decode()
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def dialogs(self, limit=1000):
        """Канали, групи (і за бажанням особисті чати) акаунта -> [{chat_id, username, title, type}]"""
        out = []
        async for d in self.client.iter_dialogs(limit=limit):
            kind = chat_kind(d.entity)
            if kind == "chat" and not os.getenv("NEWSROOM_READ_PRIVATE"):
                continue
            if getattr(d.entity, "bot", False):
                continue
            out.append({"chat_id": str(d.id), "username": (getattr(d.entity, "username", None) or "").lower(),
                        "title": d.name or "", "type": kind})
        return out

    async def fetch(self, entry, after_id=0, limit=20):
        """Нові повідомлення джерела. -> (назва, chat_id, тип, [{id, text, photo}])"""
        ref = entry["chat_id"] or entry["ref"]
        if entry["kind"] == "invite" and not entry["chat_id"]:
            from telethon.tl.functions.messages import CheckChatInviteRequest
            from telethon.tl.types import ChatInviteAlready, ChatInvitePeek
            inv = await self.client(CheckChatInviteRequest(entry["ref"].lstrip("+")))
            if not isinstance(inv, (ChatInviteAlready, ChatInvitePeek)):
                raise RuntimeError("акаунт не учасник цього чату за запрошенням — вступіть у нього вручну")
            entity = inv.chat
        else:
            entity = await self.client.get_entity(int(ref) if str(ref).lstrip("-").isdigit() else ref)
        msgs = await self.client.get_messages(entity, limit=limit, min_id=after_id)
        out = []
        for m in reversed(msgs):
            text = m.raw_text or ""
            photo = await self._photo(m) if (not text.strip() and m.photo) else ""
            out.append({"id": m.id, "text": text, "photo": photo, "fwd": fwd_of(m), "metrics": tg_metrics(m),
                        "at": m.date.timestamp() if getattr(m, "date", None) else 0})
        from telethon import utils
        return (getattr(entity, "title", "") or getattr(entity, "first_name", "") or "", str(utils.get_peer_id(entity)),
                chat_kind(entity), out)

    async def stop(self):
        if self.client:
            await self.client.disconnect()
