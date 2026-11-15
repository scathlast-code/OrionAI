"""Міст: той самий код ботів (ШІ-бот і головний бот) працює у WhatsApp та Instagram через офіційні API Meta.

Як це влаштовано:
- бот, як і раніше, «говорить» з Telegram Bot API, але запити перехоплює міст і перекладає їх для Meta:
  повідомлення, кнопки, фото, музика, відео, голосові;
- вхідні вебхуки Meta міст перетворює на звичайні оновлення Telegram (текст, натискання кнопок, голосові, фото).

Різниця між месенджерами (чесно):
- кнопки під повідомленням: WhatsApp — до 3 кнопок або список до 10 пунктів; Instagram — до 3 кнопок або до 13 швидких відповідей.
  Більше пунктів — міст сам розбиває на сторінки з «➡️ Ще»;
- повідомлення не редагуються, тож кожне меню приходить новим повідомленням;
- меню внизу екрана немає: напишіть «меню» або натисніть «☰ Меню» у списку (в Instagram ще є постійне меню ≡);
- бот може писати вам лише протягом 24 годин після вашого останнього повідомлення. Що не вмістилося —
  міст тримає в черзі й надсилає, щойно ви напишете (WhatsApp ще вміє нагадати шаблоном — це платно);
- ботам не можна видалити ваше повідомлення: ключі й токени видаляйте з чату самі.
"""

import base64
import hashlib
import hmac
import html
import itertools
import json
import logging
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import time

import httpx
from telegram import Update
from telegram.request import HTTPXRequest

log = logging.getLogger("bridge")


def env(name, default=""):
    return os.environ.get(name, default).strip()


GRAPH = env("GRAPH_API_URL", "https://graph.facebook.com")
IG_GRAPH = env("IG_GRAPH_API_URL", "https://graph.instagram.com")
GRAPH_VERSION = env("GRAPH_VERSION", "v25.0")
# У WhatsApp та Instagram бот не може видаляти ваші повідомлення — виправляємо обіцянки в текстах ботів.
TEXT_FIXES = [
    ("Я одразу видалю ваше повідомлення з ключем із чату.", "Після цього видаліть повідомлення з ключем із чату самі."),
    ("Повідомлення з токеном я одразу видалю.", "Після цього видаліть повідомлення з токеном самі."),
    ("(повідомлення з ключем я видалю)", "(потім видаліть повідомлення з ключем самі)"),
    ("Ключ отримано й видалено з чату.", "Ключ отримано. Видаліть повідомлення з ключем із чату."),
    ("Бота додано. Повідомлення з токеном видалено з чату.", "Бота додано. Видаліть повідомлення з токеном із чату."),
    ("Токен замінено, повідомлення з ним видалено.", "Токен замінено. Видаліть повідомлення з ним із чату."),
    ("я перевірю його й видалю повідомлення.", "я перевірю його, а повідомлення потім видаліть самі."),
    ("Токен я перевіряю й одразу видаляю ваше повідомлення з ним із чату.", "Токен я перевіряю; повідомлення з ним видаліть із чату самі."),
]


def fix_text(t):
    for a, b in TEXT_FIXES:
        t = t.replace(a, b)
    return t


MENU_WORDS = {"меню", "menu", "☰", "☰ меню", "/menu", "start", "/start", "старт", "почати"}


# ---------------------------------------------------------------- форматування


def html_to_wa(text):
    """HTML Telegram -> розмітка WhatsApp (*жирний*, _курсив_, `код`)."""
    t = text or ""
    t = re.sub(r"<pre[^>]*>(.*?)</pre>", lambda m: "```" + m.group(1) + "```", t, flags=re.S)
    t = re.sub(r"<code[^>]*>(.*?)</code>", lambda m: "`" + m.group(1) + "`" if "\n" not in m.group(1) else "```" + m.group(1) + "```", t, flags=re.S)
    t = re.sub(r"</?(b|strong)>", "*", t)
    t = re.sub(r"</?(i|em)>", "_", t)
    t = re.sub(r"</?(s|strike|del)>", "~", t)
    t = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", t, flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\*(\s*)\*", r"\1", t)  # порожні *…*
    return html.unescape(t).strip()


def html_to_plain(text):
    t = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", text or "", flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def split_text(text, limit):
    out, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            out.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out or [""]


def flat_buttons(markup):
    """reply_markup Telegram -> ([(підпис, дані)], посилання [(підпис, url)], тексти для копіювання)."""
    btns, links, copies = [], [], []
    for row in (markup or {}).get("inline_keyboard", []):
        for b in row:
            if b.get("url"):
                links.append((b["text"], b["url"]))
            elif b.get("copy_text"):
                copies.append(b["copy_text"]["text"])
            elif b.get("callback_data") and b["callback_data"] != "m:noop":
                btns.append((b["text"], b["callback_data"]))
    return btns, links, copies


# ---------------------------------------------------------------- спільна частина мосту


class Bridge:
    platform = "base"
    max_text = 4096

    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(os.path.join(data_dir, "bridge_media"), exist_ok=True)
        self.ids = itertools.count(int(time.time()) % 100000 * 1000)
        self.upd = itertools.count(1)
        self.app = None
        self.menus = {}       # chat -> підписи меню внизу (з ReplyKeyboardMarkup)
        self.pages = {}       # chat -> (кнопки, текст) для «➡️ Ще»
        self.last_seen = {}   # chat -> коли людина писала востаннє (вікно 24 год)
        self.outbox = {}      # chat -> повідомлення, що не вмістилися у вікно
        self.files = {}       # file_id -> {"kind", "ref"} для getFile/скачування
        self.toasts = {}      # chat -> коротка підказка, що піде разом із наступним повідомленням
        self.community = None  # 🏘 Спільноти (community.Community): групи WhatsApp, коментарі Instagram
        self.bot_user = {"id": int(env("BRIDGE_BOT_ID", "1000")), "is_bot": True, "first_name": env("BRIDGE_BOT_NAME", "Бот"),
                         "username": env("BRIDGE_BOT_USERNAME", "bot")}

    # ---- збереження файлів, щоб їх можна було надіслати повторно (бібліотека медіа)
    def store(self, data, filename):
        fid = f"{self.platform}f_{secrets.token_hex(8)}"
        path = os.path.join(self.data_dir, "bridge_media", fid)
        with open(path, "wb") as f:
            f.write(data)
        with open(path + ".name", "w") as f:
            f.write(filename or "file")
        return fid

    def load(self, fid):
        path = os.path.join(self.data_dir, "bridge_media", fid)
        if not os.path.exists(path):
            return None, None
        with open(path, "rb") as f:
            data = f.read()
        name = open(path + ".name").read() if os.path.exists(path + ".name") else "file"
        return data, name

    # ---- встановлення в PTB
    def install(self):
        bridge = self

        async def do_request(self_, url, method, request_data=None, **kw):
            if "/file/bot" in url:
                path = url.split("/file/bot", 1)[1].split("/", 1)[1]
                return 200, await bridge.download(path)
            api = url.rsplit("/", 1)[-1]
            params = dict(request_data.parameters) if request_data else {}
            files = dict(request_data.multipart_data) if request_data else {}
            try:
                res = await bridge.call(api, params, files)
                return 200, json.dumps({"ok": True, "result": res}).encode()
            except BridgeError as e:
                return 400, json.dumps({"ok": False, "error_code": 400, "description": f"Bad Request: {e}"}).encode()

        HTTPXRequest.do_request = do_request
        return self

    def dispatch(self, payload):
        """Ставить вебхук у фонову чергу. Повторні доставки того самого повідомлення відкидаються."""
        import asyncio

        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        seen = getattr(self, "_seen", None)
        if seen is None:
            seen = self._seen = {}
        now = time.time()
        for k in [k for k, t in seen.items() if now - t > 3600]:
            del seen[k]
        if key in seen:
            return
        seen[key] = now
        prev = getattr(self, "_chain", None)

        async def run():
            if prev is not None:
                try:
                    await prev
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self.webhook(payload)
            except Exception:  # noqa: BLE001
                log.exception("помилка обробки вебхука")

        self._chain = asyncio.ensure_future(run())
        return self._chain

    def _msg(self, chat, extra=None):
        m = {"message_id": next(self.ids), "date": int(time.time()), "chat": {"id": int(chat), "type": "private"}, "from": self.bot_user}
        m.update(extra or {})
        return m

    async def call(self, api, p, files):
        chat = p.get("chat_id")
        if api == "getMe":
            return self.bot_user
        if api in ("sendMessage", "editMessageText"):
            text = fix_text(p.get("text", ""))
            toast = self.toasts.pop(int(chat), None) if chat else None
            if toast:
                text = f"<i>· {html.escape(toast)}</i>\n\n{text}"
            await self.send_text(chat, text, p.get("reply_markup"))
            return self._msg(chat, {"text": p.get("text", "")})
        if api in ("sendPhoto", "sendAudio", "sendVideo", "sendDocument", "sendVoice"):
            kind = api[4:].lower()
            src = p.get(kind)
            if kind in files:
                name, data = files[kind][0], files[kind][1]
                data = data if isinstance(data, bytes) else data.read()
                fid = self.store(data, name)
            elif isinstance(src, str) and src.startswith(("http://", "https://")):
                fid, data, name = None, None, src
            else:
                fid = src
                data, name = self.load(src)
                if data is None:
                    raise BridgeError("файл не знайдено")
            await self.send_media(chat, kind, data, name, html_to_plain(p.get("caption", "")))
            if p.get("reply_markup"):
                await self.send_text(chat, "⬆️", p["reply_markup"])
            obj = {"file_id": fid or name, "file_unique_id": (fid or name)[-12:], "width": 1, "height": 1, "duration": 1}
            if kind == "document":
                obj["file_name"] = name
            return self._msg(chat, {kind: [obj] if kind == "photo" else obj, **({"caption": p["caption"]} if p.get("caption") else {})})
        if api == "getFile":
            return {"file_id": p["file_id"], "file_unique_id": p["file_id"][-12:], "file_path": p["file_id"]}
        if api == "answerCallbackQuery":
            # коротка підказка («Збережено», «Переміщено») не йде окремим повідомленням —
            # вона додається на початок наступного, щоб не витрачати ліміт повідомлень
            if p.get("text") and self.last_cb_chat:
                self.toasts[int(self.last_cb_chat)] = p["text"]
            return True
        if api == "sendChatAction":
            await self.typing(chat)
            return True
        if api == "deleteMessage":
            key = int(chat) if chat else 0
            return True  # видалити не можна; тексти ботів уже просять зробити це самостійно (TEXT_FIXES)
        if api in ("setMyCommands", "deleteMyCommands", "setChatMenuButton", "editMessageReplyMarkup", "setMyName",
                   "setMyDescription", "setMyShortDescription", "deleteWebhook", "getUpdates", "logOut", "close"):
            return True if api != "getUpdates" else []
        if api == "getMyCommands":
            return []
        log.info("міст: метод %s не підтримується — пропускаю", api)
        return True

    last_cb_chat = None
    deleted_notice = {}
    user_msgs = set()  # id повідомлень, які надіслала людина (лише їх бот «хотів би» видалити)

    # ---- текст і кнопки (перевизначають адаптери)
    async def send_text(self, chat, text, markup):
        raise NotImplementedError

    async def send_media(self, chat, kind, data, name, caption):
        raise NotImplementedError

    async def typing(self, chat):
        return None

    async def download(self, path):
        raise NotImplementedError

    # ---- вікно 24 години
    def in_window(self, chat):
        seen = self.last_seen.get(int(chat))
        return seen is None or time.time() - seen < 23.5 * 3600  # невідомо — пробуємо, Meta сама скаже

    def queue(self, chat, item):
        q = self.outbox.setdefault(int(chat), [])
        if len(q) < 30:
            q.append(item)
        self.save_state()

    def state_path(self):
        return os.path.join(self.data_dir, f"bridge_{self.platform}_state.json")

    def save_state(self):
        try:
            with open(self.state_path(), "w") as f:
                json.dump({"last_seen": self.last_seen, "outbox": self.outbox, "menus": self.menus,
                           "usage": getattr(self, "usage", None)}, f, ensure_ascii=False)
        except OSError:
            pass

    def load_state(self):
        try:
            with open(self.state_path()) as f:
                st = json.load(f)
            self.last_seen = {int(k): v for k, v in st.get("last_seen", {}).items()}
            self.outbox = {int(k): v for k, v in st.get("outbox", {}).items()}
            self.menus = {int(k): v for k, v in st.get("menus", {}).items()}
            if st.get("usage") and hasattr(self, "usage"):
                self.usage = st["usage"]
        except (OSError, ValueError):
            pass

    async def flush(self, chat):
        q = self.outbox.pop(int(chat), [])
        if q:
            await self.send_text(chat, f"📬 Поки вікно для повідомлень було закрите, накопичилося {len(q)}:", None)
            for kind, args in q:
                await (self.send_text(*args) if kind == "text" else self.send_media(*args))

    # ---- вхідні -> Update Telegram
    def user(self, uid, name):
        return {"id": int(uid), "is_bot": False, "first_name": name or str(uid), "username": None}

    async def feed(self, upd):
        await self.app.process_update(Update.de_json(upd, self.app.bot))

    async def incoming_text(self, uid, name, text, forwarded=False, raw=False):
        self.last_seen[int(uid)] = time.time()
        self.save_state()
        await self.flush(uid)
        low = text.strip().lower()
        if not raw and low in MENU_WORDS and not low.startswith("/"):
            if self.menus.get(int(uid)):  # у бота є меню внизу (головний бот) — показую його списком
                return await self.send_menu(uid)
            text = "/menu"  # меню внизу немає (ШІ-бот) — слово «меню» відкриває кнопкове меню, а не йде до ШІ
        if not raw and low.startswith("➡️ ще") and int(uid) in self.pages:
            return await self.more(uid)
        msg = {"message_id": next(self.ids), "date": int(time.time()), "chat": {"id": int(uid), "type": "private"}, "from": self.user(uid, name), "text": text}
        self.user_msgs.add(msg["message_id"])
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if forwarded:
            msg["forward_origin"] = {"type": "hidden_user", "date": int(time.time()), "sender_user_name": "переслано"}
        await self.feed({"update_id": next(self.upd), "message": msg})

    async def incoming_button(self, uid, name, data):
        self.last_seen[int(uid)] = time.time()
        self.save_state()
        await self.flush(uid)
        if data == "__more":
            return await self.more(uid)
        if data.startswith("__menu:"):  # пункт меню внизу — як натискання кнопки меню в Telegram
            return await self.incoming_text(uid, name, data[7:], raw=True)
        self.last_cb_chat = int(uid)
        msg = {"message_id": next(self.ids), "date": int(time.time()), "chat": {"id": int(uid), "type": "private"}, "from": self.bot_user, "text": "меню"}
        await self.feed({"update_id": next(self.upd), "callback_query": {"id": str(next(self.ids)), "from": self.user(uid, name), "chat_instance": "b", "data": data, "message": msg}})

    async def incoming_media(self, uid, name, kind, file_id, caption="", forwarded=False, mime=None):
        self.last_seen[int(uid)] = time.time()
        self.save_state()
        await self.flush(uid)
        msg = {"message_id": next(self.ids), "date": int(time.time()), "chat": {"id": int(uid), "type": "private"}, "from": self.user(uid, name)}
        obj = {"file_id": file_id, "file_unique_id": file_id[-12:], "duration": 1, "width": 1, "height": 1}
        if mime:
            obj["mime_type"] = mime
        if kind == "photo":
            msg["photo"] = [obj]
        elif kind == "document":
            obj["file_name"] = caption or "file"
            msg["document"] = obj
        else:
            msg[kind] = obj
        if caption and kind != "document":
            msg["caption"] = caption
        if forwarded:
            msg["forward_origin"] = {"type": "hidden_user", "date": int(time.time()), "sender_user_name": "переслано"}
        await self.feed({"update_id": next(self.upd), "message": msg})

    # ---- меню й сторінки
    def remember_menu(self, chat, markup):
        if markup and "keyboard" in markup:
            labels = [b["text"] if isinstance(b, dict) else b for row in markup["keyboard"] for b in row]
            self.menus[int(chat)] = labels
            return True
        return False

    async def send_menu(self, chat):
        labels = self.menus.get(int(chat)) or ["☰ Меню"]
        await self.send_choices(chat, "☰ *Меню*" if self.platform == "wa" else "☰ Меню", [(l, "__menu:" + l) for l in labels])

    async def more(self, chat):
        btns, text = self.pages.pop(int(chat), ([], ""))
        if btns:
            await self.send_choices(chat, text or "Ще:", btns)

    async def send_choices(self, chat, text, btns):
        raise NotImplementedError


class BridgeError(Exception):
    pass


def verify_signature(secret, body, header):
    if not secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header[7:])


# ---------------------------------------------------------------- WhatsApp


class WhatsApp(Bridge):
    platform = "wa"
    max_text = 4096

    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.token = env("WA_TOKEN")
        self.phone_id = env("WA_PHONE_ID")
        self.app_secret = env("META_APP_SECRET")
        self.verify_token = env("WA_VERIFY_TOKEN")
        self.reengage = env("WA_REENGAGE_TEMPLATE")  # необов'язково: назва затвердженого шаблону для нагадувань поза вікном (платно)
        # З 1.10.2026 Meta дає 1000 безкоштовних повідомлень від бота на місяць на номер, далі — платно.
        # Міст рахує їх і зупиняється раніше, щоб ви нічого не платили (WA_MONTHLY_STOP=0 — не зупинятися).
        self.free_limit = int(env("WA_FREE_PER_MONTH", "1000") or 1000)
        self.stop_at = int(env("WA_MONTHLY_STOP", "990") or 0)
        self.usage = {"month": time.strftime("%Y-%m"), "count": 0, "warned": []}

    def count_sent(self):
        month = time.strftime("%Y-%m")
        if self.usage.get("month") != month:
            self.usage = {"month": month, "count": 0, "warned": []}
        self.usage["count"] += 1
        self.save_state()
        return self.usage["count"]

    def usage_line(self):
        return f"{self.usage.get('count', 0)} з {self.free_limit} безкоштовних повідомлень цього місяця"

    async def api(self, method, path, **kw):
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.request(method, f"{GRAPH}/{GRAPH_VERSION}/{path}", headers={"Authorization": f"Bearer {self.token}"}, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            err = data.get("error", {})
            raise WAError(err.get("code"), err.get("message", r.text[:200]))
        return data

    async def post(self, chat, body):
        if not self.in_window(chat):
            first = not self.outbox.get(int(chat))
            self.queue(chat, ("raw", body))
            if first and self.reengage:  # платне нагадування шаблоном, щоб ви відкрили чат
                await self.reengage_template(chat)
            return
        month = time.strftime("%Y-%m")
        if self.usage.get("month") != month:
            self.usage = {"month": month, "count": 0, "warned": []}
        used = self.usage["count"]
        if self.stop_at and used >= self.stop_at:
            log.warning("WhatsApp: досягнуто межі %s повідомлень за місяць — не надсилаю, щоб не платити", self.stop_at)
            if "stop" not in self.usage["warned"]:  # одне пояснення — у межах безкоштовних (межа нижча за 1000)
                self.usage["warned"].append("stop")
                self.save_state()
                try:
                    await self.api("POST", f"{self.phone_id}/messages", json={"messaging_product": "whatsapp", "to": str(chat), "type": "text", "text": {"body":
                        f"⛔️ Безкоштовні повідомлення WhatsApp на цей місяць закінчилися ({self.usage_line()}). "
                        "Я замовкаю до 1-го числа, щоб Meta нічого не нарахувала. Telegram-версія бота працює як завжди.\n"
                        "Хочете продовжити платно — задайте WA_MONTHLY_STOP=0 і додайте картку в Meta."}})
                    self.count_sent()
                except WAError:
                    pass
            return
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": str(chat), **body}
        try:
            await self.api("POST", f"{self.phone_id}/messages", json=body)
            used = self.count_sent()
            for share, note in ((0.8, "80%"), (0.95, "95%")):
                mark = int(self.free_limit * share)
                if used >= mark and note not in self.usage["warned"] and (not self.stop_at or used < self.stop_at):
                    self.usage["warned"].append(note)
                    self.save_state()
                    stop = (f"На {self.stop_at}-му я зупинюся до 1-го числа, щоб нічого не платити." if self.stop_at else
                            "Далі Meta братиме оплату за кожне повідомлення.")
                    await self.api("POST", f"{self.phone_id}/messages", json={"messaging_product": "whatsapp", "to": str(chat), "type": "text",
                                   "text": {"body": f"⚠️ Використано {note} безкоштовних повідомлень WhatsApp цього місяця ({self.usage_line()}). {stop}\n"
                                                    "Порада: менше перемикань меню, а довгі розмови — у Telegram-версії бота."}})
                    self.count_sent()
        except WAError as e:
            if e.code in (131047, 131026):  # поза вікном 24 год / неможливо доставити
                self.last_seen[int(chat)] = 0
                self.queue(chat, ("raw", body))
            else:
                log.warning("WhatsApp: %s", e)

    async def flush(self, chat):
        q = self.outbox.pop(int(chat), [])
        if q:
            await self.post(chat, {"type": "text", "text": {"body": f"📬 Поки вікно для повідомлень було закрите, накопичилося {len(q)}:"}})
            for _, body in q:
                body.pop("to", None)
                await self.post(chat, {k: v for k, v in body.items() if k not in ("messaging_product", "recipient_type")})

    async def reengage_template(self, chat):
        try:
            await self.api("POST", f"{self.phone_id}/messages", json={"messaging_product": "whatsapp", "to": str(chat), "type": "template",
                                                                     "template": {"name": self.reengage, "language": {"code": env("WA_TEMPLATE_LANG", "uk")}}})
        except WAError as e:
            log.warning("шаблон нагадування не надіслано: %s", e)

    async def send_text(self, chat, text, markup):
        if self.remember_menu(chat, markup):
            markup = None
            text = text + "\n\n_Меню — напишіть «меню» в будь-який момент._" if text.strip() not in ("⌨️",) else ""
            if not text:
                return await self.send_menu(chat)
        body = html_to_wa(text)
        btns, links, copies = flat_buttons(markup)
        if links:
            body += "\n\n" + "\n".join(f"🔗 {t}: {u}" for t, u in links)
        if btns:
            await self.send_choices(chat, body, btns)
        else:
            for part in split_text(body, self.max_text):
                await self.post(chat, {"type": "text", "text": {"body": part, "preview_url": False}})
        for c in copies:  # «скопіювати одним дотиком» — окремим повідомленням, його легко скопіювати довгим натисканням
            await self.post(chat, {"type": "text", "text": {"body": c}})

    async def send_choices(self, chat, text, btns):
        text = text or "Оберіть:"
        if len(text) > 1024:  # тіло інтерактивного повідомлення — до 1024 символів
            for part in split_text(text, self.max_text):
                await self.post(chat, {"type": "text", "text": {"body": part}})
            text = "Оберіть:"
        if len(btns) <= 3 and all(len(t) <= 20 for t, _ in btns):
            await self.post(chat, {"type": "interactive", "interactive": {"type": "button", "body": {"text": text},
                                   "action": {"buttons": [{"type": "reply", "reply": {"id": d[:256], "title": t}} for t, d in btns]}}})
            return
        page, rest = (btns, []) if len(btns) <= 10 else (btns[:9], btns[9:])
        rows = []
        for t, d in page:
            title, desc = (t, "") if len(t) <= 24 else (t[:23] + "…", t[:72])
            rows.append({"id": d[:200], "title": title, **({"description": desc} if desc else {})})
        if rest:
            self.pages[int(chat)] = (rest, "Ще варіанти:")
            rows.append({"id": "__more", "title": "➡️ Ще", "description": f"ще {len(rest)}"})
        await self.post(chat, {"type": "interactive", "interactive": {"type": "list", "body": {"text": text},
                               "action": {"button": "Обрати", "sections": [{"title": "Дії", "rows": rows}]}}})

    async def upload(self, data, name):
        mime = mimetypes.guess_type(name or "")[0] or "application/octet-stream"
        if (name or "").endswith(".ogg"):
            mime = "audio/ogg"
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{GRAPH}/{GRAPH_VERSION}/{self.phone_id}/media", headers={"Authorization": f"Bearer {self.token}"},
                             data={"messaging_product": "whatsapp", "type": mime}, files={"file": (name or "file", data, mime)})
        if r.status_code >= 400:
            raise BridgeError(f"WhatsApp не прийняв файл: {r.text[:150]}")
        return r.json()["id"]

    async def send_media(self, chat, kind, data, name, caption):
        wkind = {"photo": "image", "audio": "audio", "voice": "audio", "video": "video", "document": "document"}[kind]
        if data is None:
            obj = {"link": name}
        else:
            obj = {"id": await self.upload(data, name)}
        if caption and wkind in ("image", "video", "document"):
            obj["caption"] = caption[:1024]
        if wkind == "document":
            obj["filename"] = os.path.basename(name or "file")
        await self.post(chat, {"type": wkind, wkind: obj})
        if caption and wkind == "audio":
            await self.post(chat, {"type": "text", "text": {"body": caption[:4096]}})

    async def typing(self, chat):
        mid = getattr(self, "_last_in", {}).get(int(chat))
        if mid:
            try:
                await self.api("POST", f"{self.phone_id}/messages", json={"messaging_product": "whatsapp", "status": "read", "message_id": mid, "typing_indicator": {"type": "text"}})
            except WAError:
                pass

    async def download(self, path):
        media_id = path.split(":", 1)[1] if path.startswith("wa:") else path
        if not path.startswith("wa:"):
            data, _ = self.load(path)
            return data or b""
        meta = await self.api("GET", media_id)
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.get(meta["url"], headers={"Authorization": f"Bearer {self.token}"})
        return r.content

    async def register_webhook(self, url):
        """Окрема адреса вебхука для цього номера: так ШІ-бот і головний бот (два номери в одному застосунку Meta)
        отримують кожен свої повідомлення. Meta перевіряє адресу запитом із WA_VERIFY_TOKEN, тож сервер уже має працювати."""
        try:
            await self.api("POST", self.phone_id, json={"webhook_configuration": {"override_callback_uri": url, "verify_token": self.verify_token}})
            log.info("WhatsApp: вебхук номера %s -> %s", self.phone_id, url)
            return True
        except WAError as e:
            log.warning("WhatsApp: не вдалося задати адресу вебхука для номера (%s). Задайте її в кабінеті Meta вручну.", e)
            return False

    async def webhook(self, payload):
        """Обробляє вебхук WhatsApp Cloud API."""
        for entry in payload.get("entry", []):
            for ch in entry.get("changes", []):
                v = ch.get("value", {})
                pid = (v.get("metadata") or {}).get("phone_number_id")
                if pid and self.phone_id and str(pid) != str(self.phone_id):
                    continue  # повідомлення для іншого номера (іншого бота) — не наше
                names = {c["wa_id"]: c.get("profile", {}).get("name") for c in v.get("contacts", [])}
                for m in v.get("messages", []):
                    uid = m["from"]
                    gid = m.get("group_id") or (m.get("group") or {}).get("id") or v.get("group_id")
                    if gid:  # повідомлення в групі WhatsApp — це не розмова з ботом, а спільнота
                        if self.community and m.get("type") == "text":
                            await self.community.wa_group_message(gid, uid, names.get(uid), m["text"]["body"])
                        continue
                    self._last_in = getattr(self, "_last_in", {})
                    self._last_in[int(uid)] = m["id"]
                    name = names.get(uid)
                    fwd = bool(m.get("context", {}).get("forwarded"))
                    t = m.get("type")
                    if t == "text":
                        await self.incoming_text(uid, name, m["text"]["body"], fwd)
                    elif t == "interactive":
                        it = m["interactive"]
                        data = (it.get("button_reply") or it.get("list_reply") or {}).get("id", "")
                        await self.incoming_button(uid, name, data)
                    elif t == "button":
                        await self.incoming_text(uid, name, m["button"].get("text", ""))
                    elif t in ("audio", "voice"):
                        a = m[t]
                        await self.incoming_media(uid, name, "voice" if a.get("voice") or not fwd else "audio", "wa:" + a["id"], forwarded=fwd, mime=a.get("mime_type"))
                    elif t == "image":
                        await self.incoming_media(uid, name, "photo", "wa:" + m["image"]["id"], m["image"].get("caption", ""), fwd)
                    elif t == "video":
                        await self.incoming_media(uid, name, "video", "wa:" + m["video"]["id"], m["video"].get("caption", ""), fwd)
                    elif t == "document":
                        d = m["document"]
                        await self.incoming_media(uid, name, "document", "wa:" + d["id"], d.get("filename", "file"), fwd, d.get("mime_type"))
                    else:
                        await self.post(uid, {"type": "text", "text": {"body": "Цей тип повідомлення бот поки не розуміє."}})


class WAError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


# ---------------------------------------------------------------- Instagram


class Instagram(Bridge):
    platform = "ig"
    max_text = 1000

    def __init__(self, data_dir, public_url):
        super().__init__(data_dir)
        self.token = env("IG_TOKEN")
        self.app_secret = env("IG_APP_SECRET", env("META_APP_SECRET"))
        self.verify_token = env("IG_VERIFY_TOKEN")
        self.public_url = public_url.rstrip("/")
        self.urls = {}  # file_id -> зовнішній URL вкладення (вхідні)
        self.token_info = {"token": None, "refreshed": 0}

    def load_state(self):
        super().load_state()
        try:
            with open(self.state_path()) as f:
                ti = json.load(f).get("ig_token")
            if ti and ti.get("token") and ti.get("from_env") == self.token[-12:]:
                self.token_info = ti
                self.token = ti["token"]  # новіший, продовжений токен
        except (OSError, ValueError):
            pass

    def save_state(self):
        super().save_state()
        try:
            with open(self.state_path()) as f:
                st = json.load(f)
            st["ig_token"] = self.token_info
            with open(self.state_path(), "w") as f:
                json.dump(st, f, ensure_ascii=False)
        except (OSError, ValueError):
            pass

    async def refresh_token(self, force=False):
        """Токен Instagram живе 60 днів; продовжуємо раз на ~20 днів, тож бот не замовкає."""
        if not force and time.time() - (self.token_info.get("refreshed") or 0) < 20 * 86400:
            return False
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(f"{IG_GRAPH}/refresh_access_token", params={"grant_type": "ig_refresh_token", "access_token": self.token})
            data = r.json()
            if r.status_code == 200 and data.get("access_token"):
                env_tail = (self.token_info.get("from_env") or env("IG_TOKEN")[-12:])
                self.token = data["access_token"]
                self.token_info = {"token": self.token, "refreshed": time.time(), "from_env": env_tail, "expires_in": data.get("expires_in")}
                self.save_state()
                log.info("токен Instagram продовжено")
                return True
            log.warning("не вдалося продовжити токен Instagram: %s", r.text[:200])
        except (httpx.HTTPError, ValueError) as e:
            log.warning("не вдалося продовжити токен Instagram: %s", e)
        self.token_info["refreshed"] = time.time() - 19 * 86400  # наступна спроба — через добу
        return False

    async def api(self, method, path, **kw):
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.request(method, f"{IG_GRAPH}/{GRAPH_VERSION}/{path}", headers={"Authorization": f"Bearer {self.token}"}, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            err = data.get("error", {})
            raise WAError(err.get("error_subcode") or err.get("code"), err.get("message", r.text[:200]))
        return data

    async def post(self, chat, message):
        if not self.in_window(chat):
            self.queue(chat, ("raw", message))
            return
        try:
            await self.api("POST", "me/messages", json={"recipient": {"id": str(chat)}, "message": message})
        except WAError as e:
            if e.code in (2018278, 2534022, 10):  # поза вікном 24 год
                self.last_seen[int(chat)] = 0
                self.queue(chat, ("raw", message))
            else:
                log.warning("Instagram: %s", e)

    async def flush(self, chat):
        q = self.outbox.pop(int(chat), [])
        if q:
            await self.post(chat, {"text": f"📬 Поки вікно для повідомлень було закрите, накопичилося {len(q)}:"})
            for _, m in q:
                await self.post(chat, m)

    async def send_text(self, chat, text, markup):
        if self.remember_menu(chat, markup):
            markup = None
            if text.strip() == "⌨️":
                return await self.send_menu(chat)
            text += "\n\nМеню — напишіть «меню» або відкрийте ≡."
        body = html_to_plain(text)
        btns, links, copies = flat_buttons(markup)
        if btns or links:
            await self.send_choices(chat, body, btns, links)
        else:
            for part in split_text(body, self.max_text):
                await self.post(chat, {"text": part})
        for c in copies:
            await self.post(chat, {"text": c[:1000]})

    async def send_choices(self, chat, text, btns, links=None):
        links = links or []
        text = text or "Оберіть:"
        if len(btns) + len(links) <= 3 and all(len(t) <= 20 for t, _ in btns + links) and len(text) <= 640:
            buttons = [{"type": "postback", "title": t, "payload": d[:1000]} for t, d in btns]
            buttons += [{"type": "web_url", "title": t, "url": u} for t, u in links]
            return await self.post(chat, {"attachment": {"type": "template", "payload": {"template_type": "button", "text": text, "buttons": buttons}}})
        if links:
            text += "\n\n" + "\n".join(f"🔗 {t}: {u}" for t, u in links)
        parts = split_text(text, self.max_text)
        for part in parts[:-1]:
            await self.post(chat, {"text": part})
        page, rest = (btns, []) if len(btns) <= 13 else (btns[:12], btns[12:])
        qr = [{"content_type": "text", "title": t[:20], "payload": d[:1000]} for t, d in page]
        if rest:
            self.pages[int(chat)] = (rest, "Ще варіанти:")
            qr.append({"content_type": "text", "title": "➡️ Ще", "payload": "__more"})
        msg = {"text": parts[-1]}
        if qr:
            msg["quick_replies"] = qr
        await self.post(chat, msg)

    def media_url(self, fid):
        return f"{self.public_url}/media/{fid}"

    async def send_media(self, chat, kind, data, name, caption):
        ikind = {"photo": "image", "audio": "audio", "voice": "audio", "video": "video", "document": "file"}[kind]
        if data is None:
            url = name
        else:
            if ikind == "audio" and not (name or "").endswith((".m4a", ".mp4", ".wav", ".aac")):
                data, name = to_m4a(data), "audio.m4a"  # Instagram приймає aac/m4a/wav/mp4
            url = self.media_url(self.store(data, name))
        await self.post(chat, {"attachment": {"type": ikind, "payload": {"url": url}}})
        if caption:
            await self.post(chat, {"text": caption[:1000]})

    async def typing(self, chat):
        try:
            await self.api("POST", "me/messages", json={"recipient": {"id": str(chat)}, "sender_action": "typing_on"})
        except WAError:
            pass

    async def download(self, path):
        if path.startswith("ig:"):
            url = self.urls.get(path)
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
                return (await c.get(url)).content if url else b""
        data, _ = self.load(path)
        return data or b""

    async def setup_menu(self, labels):
        """Постійне меню ≡ в Instagram (до 20 пунктів) — з розділів меню внизу."""
        try:
            await self.api("POST", "me/messenger_profile", json={"platform": "instagram", "persistent_menu": [
                {"locale": "default", "composer_input_disabled": False,
                 "call_to_actions": [{"type": "postback", "title": l[:30], "payload": "__menu:" + l} for l in labels[:20]]}]})
        except WAError as e:
            log.warning("постійне меню Instagram не встановлено: %s", e)

    async def webhook(self, payload):
        await self.refresh_token()
        me = None
        for entry in payload.get("entry", []):
            me = str(entry.get("id"))
            for ch in entry.get("changes", []):  # коментарі під дописами — у 🏘 Спільноти
                if ch.get("field") in ("comments", "live_comments") and self.community:
                    await self.community.ig_comment(me, ch.get("value") or {})
            for ev in entry.get("messaging", []):
                uid = ev.get("sender", {}).get("id")
                if not uid or uid == me:
                    continue
                if "postback" in ev:
                    await self.incoming_button(uid, None, ev["postback"].get("payload", ""))
                    continue
                m = ev.get("message") or {}
                if m.get("is_echo"):
                    continue
                if m.get("quick_reply"):
                    await self.incoming_button(uid, None, m["quick_reply"].get("payload", ""))
                    continue
                atts = m.get("attachments") or []
                if atts:
                    a = atts[0]
                    url = a.get("payload", {}).get("url")
                    fid = f"ig:{secrets.token_hex(6)}"
                    self.urls[fid] = url
                    kind = {"audio": "voice", "image": "photo", "video": "video", "file": "document"}.get(a.get("type"), "document")
                    await self.incoming_media(uid, None, kind, fid, m.get("text", ""), forwarded=bool(m.get("is_forwarded")))
                elif m.get("text"):
                    await self.incoming_text(uid, None, m["text"], forwarded=bool(m.get("is_forwarded")))


def to_m4a(data):
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in"), os.path.join(d, "out.m4a")
        open(src, "wb").write(data)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c:a", "aac", "-b:a", "96k", dst], check=True)
        return open(dst, "rb").read()


# ---------------------------------------------------------------- маршрути вебхуків (Starlette)


def routes(bridge):
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse, Response
    from starlette.routing import Route

    path = "/wa/webhook" if bridge.platform == "wa" else "/ig/webhook"

    async def hook(request: Request):
        if request.method == "GET":  # підтвердження вебхука в кабінеті Meta
            q = request.query_params
            if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == bridge.verify_token:
                return PlainTextResponse(q.get("hub.challenge", ""))
            return PlainTextResponse("forbidden", status_code=403)
        body = await request.body()
        if not verify_signature(bridge.app_secret, body, request.headers.get("X-Hub-Signature-256")):
            return PlainTextResponse("bad signature", status_code=403)
        try:
            payload = json.loads(body)
        except ValueError:
            return PlainTextResponse("bad json", status_code=400)
        # Meta чекає відповіді за кілька секунд, а ШІ може думати довше — обробляємо у фоні,
        # по черзі для кожної людини (щоб відповіді не перемішувалися), без повторів.
        bridge.dispatch(payload)
        return JSONResponse({"ok": True})

    async def media(request: Request):
        data, name = bridge.load(request.path_params["fid"])
        if data is None:
            return PlainTextResponse("not found", status_code=404)
        return Response(data, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream")

    return [Route(path, hook, methods=["GET", "POST"]), Route("/media/{fid}", media)]


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode()
