"""🔔 Події для застосунку: усе, що бот надсилає людині сам (результати фонових завдань, новинарня, скринька,
нагадування, рятувальні копії…), дублюється в стрічку застосунку. Відповіді на власні дії людини в чаті сюди
не потрапляють — їх вона й так бачить. Зберігається останні 100 подій на людину."""
from __future__ import annotations

import contextvars
import json
import sqlite3
import time
from contextlib import closing

UPDATE_CHAT = contextvars.ContextVar("update_chat", default=None)  # чат, чию дію бот зараз обробляє
SENT = contextvars.ContextVar("sent_ids", default=None)  # список, куди складати id надісланих повідомлень (зв'язок із розмовою)


def _note_sent(m):
    lst = SENT.get()
    if lst is not None and getattr(m, "message_id", None):
        lst.append(m.message_id)
KEEP = 100


class Feed:
    def __init__(self, path):
        self.path = str(path)
        with closing(self.db()) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS feed (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, ts REAL, html TEXT, "
                        "rows TEXT, file TEXT, seen INTEGER DEFAULT 0)")
            con.execute("CREATE INDEX IF NOT EXISTS feed_chat ON feed(chat, id)")

    def db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def add(self, chat, html, rows=None, file=""):
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO feed(chat, ts, html, rows, file) VALUES (?,?,?,?,?)",
                        (chat, time.time(), (html or "")[:6000], json.dumps(rows or [], ensure_ascii=False), file or ""))
            con.execute("DELETE FROM feed WHERE chat=? AND id NOT IN (SELECT id FROM feed WHERE chat=? ORDER BY id DESC LIMIT ?)",
                        (chat, chat, KEEP))

    def items(self, chat, limit=50):
        with closing(self.db()) as con:
            rs = [dict(r) for r in con.execute("SELECT * FROM feed WHERE chat=? ORDER BY id DESC LIMIT ?", (chat, limit))]
            con.execute("UPDATE feed SET seen=1 WHERE chat=?", (chat,))
            con.commit()
        for r in rs:
            r["rows"] = json.loads(r["rows"] or "[]")
        return rs

    def unseen(self, chat):
        with closing(self.db()) as con:
            return con.execute("SELECT COUNT(*) FROM feed WHERE chat=? AND seen=0", (chat,)).fetchone()[0]

    def clear(self, chat):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM feed WHERE chat=?", (chat,))


def markup_rows(markup):
    """InlineKeyboardMarkup → [[(текст, callback або url:адреса)]] для застосунку."""
    out = []
    for row in getattr(markup, "inline_keyboard", None) or []:
        r = []
        for b in row:
            if b.callback_data:
                r.append([b.text, b.callback_data])
            elif b.url:
                r.append([b.text, "url:" + b.url])
        if r:
            out.append(r)
    return out


def feed_bot_class(base, feed_getter, is_private_id):
    """Підклас бота, який дублює «самостійні» повідомлення в стрічку застосунку."""
    import notify

    class FeedBot(base):
        def _quiet(self, chat_id, k):
            """🔔 Режим сповіщень людини: самостійні повідомлення можуть приходити тихо."""
            try:
                notify.apply(chat_id, k, UPDATE_CHAT.get() != int(chat_id))
            except Exception:  # noqa: BLE001
                pass
            return k

        def _feed(self, chat_id, html, markup, file=""):
            try:
                cid = int(chat_id)
            except (TypeError, ValueError):
                return
            if cid <= 0 or UPDATE_CHAT.get() == cid or not is_private_id(cid):
                return
            try:
                feed_getter().add(cid, html, markup_rows(markup), file)
            except Exception:  # noqa: BLE001 — стрічка не повинна заважати надсиланню
                pass

        async def send_message(self, chat_id, text, *a, **k):
            self._quiet(chat_id, k)
            m = await super().send_message(chat_id, text, *a, **k)
            _note_sent(m)
            self._feed(chat_id, text if k.get("parse_mode") else _esc(text), k.get("reply_markup"))
            return m

        async def send_document(self, chat_id, document, *a, **k):
            self._quiet(chat_id, k)
            m = await super().send_document(chat_id, document, *a, **k)
            _note_sent(m)
            name = getattr(getattr(m, "document", None), "file_name", "") or "файл"
            self._feed(chat_id, k.get("caption") or "", k.get("reply_markup"), "📎 " + name)
            return m

        async def send_photo(self, chat_id, photo, *a, **k):
            self._quiet(chat_id, k)
            m = await super().send_photo(chat_id, photo, *a, **k)
            self._feed(chat_id, k.get("caption") or "", k.get("reply_markup"), "🖼 фото")
            return m

        async def copy_message(self, chat_id, from_chat_id, message_id, *a, **k):
            self._quiet(chat_id, k)
            m = await super().copy_message(chat_id, from_chat_id, message_id, *a, **k)
            self._feed(chat_id, "", k.get("reply_markup"), "📨 повідомлення — повністю в чаті з ботом")
            return m

        async def send_video(self, chat_id, video, *a, **k):
            return await super().send_video(chat_id, video, *a, **self._quiet(chat_id, k))

        async def send_audio(self, chat_id, audio, *a, **k):
            return await super().send_audio(chat_id, audio, *a, **self._quiet(chat_id, k))

        async def send_animation(self, chat_id, animation, *a, **k):
            return await super().send_animation(chat_id, animation, *a, **self._quiet(chat_id, k))

        async def send_media_group(self, chat_id, media, *a, **k):
            return await super().send_media_group(chat_id, media, *a, **self._quiet(chat_id, k))

    return FeedBot


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
