"""Підробний сервер Telegram для тестів: перехоплює всі виклики Bot API і повертає правдоподібні відповіді.

Використання:
    import faketg; tg = faketg.install()      # до імпорту бота
    import bot; await tg.start(bot.tg_app)
    await tg.text(111, "привіт"); await tg.press(111, "m:dl")
    tg.last_text(111), tg.last_buttons(111)
"""

import itertools
import json
import time

from telegram import Update
from telegram.request import HTTPXRequest

BOT_USER = {"id": 999, "is_bot": True, "first_name": "Test", "username": "test_bot"}


class FakeTG:
    def __init__(self):
        self.calls = []
        self.msg_id = itertools.count(100)
        self.upd_id = itertools.count(1)
        self.messages = {}  # (chat, id) -> dict
        self.sent = []      # [(chat, method, payload)]
        self.app = None
        self.handlers = {}  # method -> fn(payload) -> result (для власних відповідей)

    # ---------------------------------------------------------------- відповіді на виклики бота
    def _msg(self, chat, payload, extra=None):
        mid = next(self.msg_id)
        m = {"message_id": mid, "date": int(time.time()), "chat": {"id": chat, "type": "private"}, "from": BOT_USER}
        if "text" in payload:
            m["text"] = payload["text"]
        if payload.get("caption"):
            m["caption"] = payload["caption"]
        rm = payload.get("reply_markup")
        if rm and "inline_keyboard" in rm:
            m["reply_markup"] = rm
        m.update(extra or {})
        self.messages[(chat, mid)] = m
        return m

    def handle(self, method, p):
        self.calls.append((method, p))
        if method in self.handlers:
            return self.handlers[method](p)
        chat = p.get("chat_id")
        try:
            chat = int(chat)
        except (TypeError, ValueError):
            pass
        if method == "getMe":
            return BOT_USER
        if method == "sendMessage":
            self.sent.append((chat, method, p))
            return self._msg(chat, p)
        if method in ("sendPhoto", "sendAudio", "sendVideo", "sendDocument", "sendVoice"):
            self.sent.append((chat, method, p))
            key = {"sendPhoto": "photo", "sendAudio": "audio", "sendVideo": "video", "sendDocument": "document", "sendVoice": "voice"}[method]
            f = {"file_id": f"F{next(self.msg_id)}", "file_unique_id": "u", "width": 1, "height": 1, "duration": 1}
            return self._msg(chat, p, {key: [f] if key == "photo" else f})
        if method == "editMessageText":
            self.sent.append((chat, method, p))
            mid = int(p.get("message_id") or 0)
            m = self.messages.get((chat, mid), {"message_id": mid, "chat": {"id": chat, "type": "private"}, "date": 0})
            m["text"] = p["text"]
            if p.get("reply_markup"):
                m["reply_markup"] = p["reply_markup"]
            return m
        if method == "getFile":
            return {"file_id": p["file_id"], "file_unique_id": "u", "file_path": f"files/{p['file_id']}"}
        if method == "deleteMessage":
            self.sent.append((chat, method, p))
            return True
        return True  # answerCallbackQuery, sendChatAction, setMyCommands …

    # ---------------------------------------------------------------- події від користувача
    async def start(self, app):
        self.app = app
        await app.initialize()

    async def feed(self, upd):
        await self.app.process_update(Update.de_json(upd, self.app.bot))

    def _user(self, uid, name="Owner", username=None):
        return {"id": uid, "is_bot": False, "first_name": name, "username": username or f"u{uid}"}

    async def text(self, uid, text, **extra):
        mid = next(self.msg_id)
        msg = {"message_id": mid, "date": int(time.time()), "chat": {"id": uid, "type": "private"}, "from": self._user(uid), "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        msg.update(extra)
        await self.feed({"update_id": next(self.upd_id), "message": msg})

    async def press(self, uid, data, mid=None):
        mid = mid or self.last_mid(uid) or next(self.msg_id)
        msg = self.messages.get((uid, mid)) or {"message_id": mid, "date": 0, "chat": {"id": uid, "type": "private"}}
        await self.feed({"update_id": next(self.upd_id), "callback_query": {"id": str(next(self.msg_id)), "from": self._user(uid), "chat_instance": "c", "data": data, "message": msg}})

    # ---------------------------------------------------------------- перевірки
    def out(self, uid):
        return [p for c, m, p in self.sent if c == uid and m in ("sendMessage", "editMessageText")]

    def last_mid(self, uid):
        for c, m, p in reversed(self.sent):
            if c == uid and m in ("sendMessage", "editMessageText") and "inline_keyboard" in json.dumps(p.get("reply_markup") or {}):
                if m == "editMessageText" and "message_id" in p:
                    return int(p["message_id"])
                for (cc, mid), mm in reversed(list(self.messages.items())):
                    if cc == uid and mm.get("text") == p["text"]:
                        return mid
        return None

    def last_text(self, uid):
        o = self.out(uid)
        return o[-1]["text"] if o else ""

    def last_buttons(self, uid):
        for p in reversed(self.out(uid)):
            rm = p.get("reply_markup")
            if isinstance(rm, str):
                rm = json.loads(rm)
            if rm and "inline_keyboard" in rm:
                return [[(b["text"], b.get("callback_data") or b.get("url") or "copy") for b in row] for row in rm["inline_keyboard"]]
        return []

    def reply_kb(self, uid):
        for p in reversed(self.out(uid)):
            rm = p.get("reply_markup")
            if isinstance(rm, str):
                rm = json.loads(rm)
            if rm and "keyboard" in rm:
                return [[b["text"] if isinstance(b, dict) else b for b in row] for row in rm["keyboard"]]
        return []

    def find(self, uid, label):
        for row in self.last_buttons(uid):
            for t, d in row:
                if label in t:
                    return d
        raise AssertionError(f"немає кнопки «{label}» серед {self.last_buttons(uid)}")


def _decode(request_data):
    if request_data is None:
        return {}
    out = {}
    for k, v in request_data.parameters.items():
        if isinstance(v, str):
            try:
                v = json.loads(v) if v[:1] in "[{" else v
            except ValueError:
                pass
        out[k] = v
    return out


def install():
    tg = FakeTG()

    async def do_request(self, url, method, request_data=None, **kw):
        if "/file/bot" in url:
            return 200, b"FAKEFILE"
        api = url.rsplit("/", 1)[-1]
        res = tg.handle(api, _decode(request_data))
        return 200, json.dumps({"ok": True, "result": res}).encode()

    HTTPXRequest.do_request = do_request
    return tg
