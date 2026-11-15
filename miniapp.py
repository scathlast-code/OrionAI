"""🧩 Міст «чат ↔ Mini App»: застосунок показує ті самі екрани й виконує ті самі дії, що й чат.

Mini App шле POST /app/api з initData (підпис Telegram). Бот запускає звичайний route() з «заглушкою» Update,
перехоплює екран (текст і кнопки ще до згортання) і повертає JSON. Дані, черги й налаштування — спільні з чатом.
"""
from __future__ import annotations

import contextvars
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "miniapp" / "index.html"
CAPTURE = contextvars.ContextVar("miniapp_capture", default=None)  # dict, поки обробляється запит застосунку


class _Sink:
    """Збирає все, що бот «надіслав би» у відповідь: екран, підказки, повідомлення."""

    def __init__(self):
        self.screen = None
        self.toast = None
        self.notes = []


class _Query:
    def __init__(self, sink, data):
        self.sink, self.data, self.message = sink, data, None

    async def answer(self, text=None, show_alert=False, **kw):
        if text:
            self.sink.toast = text

    async def edit_message_text(self, text, **kw):
        self.sink.screen = self.sink.screen or (text, _rows(kw.get("reply_markup")))


def _rows(markup):
    if not markup or not getattr(markup, "inline_keyboard", None):
        return []
    out = []
    for r in markup.inline_keyboard:
        row = []
        for b in r:
            d = b.callback_data or (("url:" + b.url) if b.url else ("app:" + b.web_app.url) if b.web_app else "")
            row.append((b.text, d))
        out.append(row)
    return out


class _Chat:
    type = "private"

    def __init__(self, uid, sink, bot):
        self.id, self.sink, self.bot = uid, sink, bot

    async def send_message(self, text, **kw):
        if self.sink.screen is None and kw.get("reply_markup") is not None:
            self.sink.screen = (text, _rows(kw.get("reply_markup")))
        else:
            self.sink.notes.append(re.sub(r"<[^>]+>", "", text)[:2000])

    async def send_document(self, *a, **kw):  # файли (експорт, архіви) ідуть у чат — застосунок про це повідомляє
        await self.bot.send_document(self.id, *a, **kw)
        self.sink.notes.append("📎 Файл надіслано в чат із ботом")

    async def send_photo(self, *a, **kw):
        await self.bot.send_photo(self.id, *a, **kw)
        self.sink.notes.append("🖼 Зображення надіслано в чат")


class _User:
    is_bot = False

    def __init__(self, uid, name="", username=""):
        self.id, self.full_name, self.first_name, self.username = uid, name or str(uid), name or str(uid), username


class _Message:
    def __init__(self, uid, text, sink, chat, user):
        self.chat_id, self.text, self.caption, self.sink = uid, text, None, sink
        self.chat, self.from_user, self.message_id = chat, user, 0
        self.photo = self.document = self.voice = self.reply_to_message = self.forward_origin = None
        self.entities, self.caption_entities = [], []

    async def reply_text(self, text, **kw):
        if kw.get("reply_markup") is not None and getattr(kw["reply_markup"], "inline_keyboard", None) and self.sink.screen is None:
            self.sink.screen = (text, _rows(kw["reply_markup"]))
        else:
            self.sink.notes.append(re.sub(r"<[^>]+>", "", text)[:2000])
        return _Sent(self.sink.capbot, 0, text) if getattr(self.sink, "capbot", None) else None

    async def reply_document(self, *a, **kw):
        return await self.chat.send_document(*a, **kw)

    async def delete(self):
        return None


class _Update:
    def __init__(self, uid, sink, bot, data=None, text=None, user=None):
        u = _User(uid, *(user or ("", "")))
        self.effective_user = u
        self.effective_chat = _Chat(uid, sink, bot)
        self.callback_query = _Query(sink, data) if data is not None else None
        self.message = _Message(uid, text, sink, self.effective_chat, u) if text is not None else None
        self.effective_message = self.message or _Message(uid, "", sink, self.effective_chat, u)
        self.channel_post = None


class _Sent:
    def __init__(self, cap, mid, text):
        self.cap, self.message_id, self.text = cap, mid, text
        self.chat_id = cap.uid

    async def edit_text(self, text, **kw):
        self.cap.msgs[self.message_id] = text

    async def delete(self):
        self.cap.msgs.pop(self.message_id, None)


class _CaptureBot:
    """Справжній бот, але все, що йде в чат цього користувача (відповіді ШІ, прогрес), збирається для застосунку.
    Файли, фото, голос і повідомлення іншим людям проходять у Telegram як зазвичай."""

    def __init__(self, real, uid, sink):
        self.real, self.uid, self.sink, self.msgs, self.n = real, uid, sink, {}, 10 ** 9

    def __getattr__(self, name):
        return getattr(self.real, name)

    async def send_message(self, chat_id, text, **kw):
        if int(chat_id) != self.uid:
            return await self.real.send_message(chat_id, text, **kw)
        self.n += 1
        self.msgs[self.n] = text
        if kw.get("reply_markup") is not None and getattr(kw["reply_markup"], "inline_keyboard", None) and self.sink.screen is None:
            self.sink.screen = (text, _rows(kw["reply_markup"]))
        return _Sent(self, self.n, text)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        if chat_id is not None and int(chat_id) == self.uid and message_id in self.msgs:
            self.msgs[message_id] = text
            return True
        return await self.real.edit_message_text(text, chat_id=chat_id, message_id=message_id, **kw)

    async def delete_message(self, chat_id, message_id, **kw):
        if int(chat_id) == self.uid and message_id in self.msgs:
            self.msgs.pop(message_id, None)
            return True
        return await self.real.delete_message(chat_id, message_id, **kw)

    async def send_chat_action(self, *a, **kw):
        return True


class _Ctx:
    def __init__(self, bot):
        self.bot, self.args = bot, []


class _File:
    def __init__(self, data):
        self.data = data
        self.file_size = len(data)

    async def download_as_bytearray(self):
        return bytearray(self.data)


class _Doc:
    def __init__(self, name, data, mime=""):
        self.file_name, self.file_size, self.mime_type, self._data = name, len(data), mime, data
        self.file_unique_id = str(hash(data))[:12]

    async def get_file(self):
        return _File(self._data)


class _Photo(_Doc):
    width = height = 1280


def clean_rows(rows):
    """Прибирає чат-навігацію (◀️ 🏠 ▶️) і 💡 — у застосунку свої."""
    out = []
    for r in rows or []:
        rr = [(t, d) for t, d in r if d and not d.startswith(("m:nav", "imp:", "ui:", "copy:")) and d not in ("m:home",)]
        if rr:
            out.append(rr)
    return out


def history(B, uid, limit=40):
    """Поточна розмова з ШІ — та сама, що в чаті."""
    did = B.current_dialog(uid)
    with B.closing(B.db()) as con:
        rows = con.execute("SELECT role, text, provider, ts FROM messages WHERE dialog_id=? AND pending=0 ORDER BY id DESC LIMIT ?",
                           (did, limit)).fetchall()
    title = B.dialog_name(did) if did else "Нова розмова"
    return {"dialog": title, "messages": [{"role": r["role"], "text": r["text"][:6000], "provider": r["provider"] or "", "ts": r["ts"]} for r in rows[::-1]]}


async def run(bot_module, uid, op, data=None, text=None, user=None, file=None):
    """op: screen | text | say | file | history. -> {"text", "rows", "toast", "notes", "awaiting", "replies"}"""
    B = bot_module
    if op == "history":
        return history(B, uid)
    sink = _Sink()
    token = CAPTURE.set(sink)
    tbot = B.tg_app.bot if B.tg_app else None
    capbot = _CaptureBot(tbot, uid, sink) if tbot else None
    sink.capbot = capbot
    try:
        B.CHAT.set(uid)
        if op in ("say", "file"):
            # Повідомлення з застосунку проходить той самий шлях, що й повідомлення в чаті: ньюзрум, стікери, ШІ…
            upd = _Update(uid, sink, capbot, text=text or "", user=user)
            ctx = _Ctx(capbot)
            if op == "file":
                name, raw, mime = file
                if mime.startswith("image/") and not name.lower().endswith((".webp", ".svg")):
                    upd.message.photo = [_Photo(name, raw, mime)]
                    upd.message.caption, upd.message.text = text or None, None
                    await B.on_message(upd, ctx)
                else:
                    upd.message.document = _Doc(name, raw, mime)
                    upd.message.caption, upd.message.text = text or None, None
                    await B.on_file_document(upd, ctx)
            else:
                await B.on_message(upd, ctx)
        elif op == "text":
            upd = _Update(uid, sink, tbot, text=text, user=user)
            if uid in B.AWAIT:
                await B.handle_awaited(upd, _Ctx(tbot), text.strip())
            else:
                sink.notes.append("Тут текст не очікувався. Відкрийте розділ і натисніть потрібну дію.")
        else:
            upd = _Update(uid, sink, tbot, data=data, user=user)
            await B.route(upd, _Ctx(tbot), data or "m:home", edit=True, record=False)
    finally:
        CAPTURE.reset(token)
    text_out, rows = sink.screen or ("", [])
    replies = [t for t in (capbot.msgs.values() if capbot else []) if t and not t.startswith(("⏳", "🕒", "💭"))]
    return {"text": text_out, "rows": clean_rows(rows), "toast": sink.toast, "notes": sink.notes[-5:], "awaiting": uid in B.AWAIT,
            "replies": [re.sub(r"<[^>]+>", "", t)[:6000] for t in replies[-5:]]}


def page():
    try:
        return PAGE.read_text("utf-8")
    except OSError:
        return None
