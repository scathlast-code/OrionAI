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
        self.media = []  # 🖼 фото, відео, аудіо, файли, які бот надіслав у чат під час цієї дії — застосунок показує їх і в себе


MEDIA_KEEPER = None  # fn(bytes, name) -> {"name","url","size","kind"} — задає бот (тимчасове посилання для застосунку)
KIND_EXT = {"photo": ".jpg", "video": ".mp4", "audio": ".mp3", "voice": ".ogg", "animation": ".mp4", "document": ""}


def _capture_media(sink, kind, payload, kw):
    """Копія надісланого в чат — для застосунку: байти/файл → тимчасове посилання; https-адреса — як є."""
    if sink is None:
        return payload
    name = kw.get("filename") or getattr(payload, "name", "") or ("файл" + KIND_EXT.get(kind, ""))
    name = str(name).rsplit("/", 1)[-1] or "файл"
    try:
        if hasattr(payload, "read"):
            payload = payload.read()
        if isinstance(payload, (bytes, bytearray)) and MEDIA_KEEPER:
            if not Path(name).suffix and KIND_EXT.get(kind):
                name += KIND_EXT[kind]
            item = MEDIA_KEEPER(bytes(payload), name)
            if item:
                item["caption"] = re.sub(r"<[^>]+>", "", kw.get("caption") or "")[:600]
                sink.media.append(item)
        elif isinstance(payload, str) and payload.startswith("http"):
            k = "image" if kind == "photo" else "video" if kind in ("video", "animation") else "audio" if kind in ("audio", "voice") else "file"
            sink.media.append({"name": name, "url": payload, "size": 0, "kind": k,
                               "caption": re.sub(r"<[^>]+>", "", kw.get("caption") or "")[:600]})
    except Exception:  # noqa: BLE001 — копія для застосунку не повинна ламати надсилання в чат
        pass
    return payload


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

    async def send_document(self, doc, *a, **kw):  # файли йдуть у чат, а застосунок показує їх і в себе
        doc = _capture_media(self.sink, "document", doc, kw)
        return await self.bot.send_document(self.id, doc, *a, **kw)

    async def send_photo(self, photo, *a, **kw):
        photo = _capture_media(self.sink, "photo", photo, kw)
        return await self.bot.send_photo(self.id, photo, *a, **kw)


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
        rows = _rows(kw["reply_markup"]) if kw.get("reply_markup") is not None and getattr(kw["reply_markup"], "inline_keyboard", None) else []
        service = all(d.startswith(("m:stop:", "view:", "stop:")) for r in rows for _, d in r)  # «Думаю…»/«Зупинити», «Чистий текст» — не окремий екран
        if rows and not service and self.sink.screen is None:
            self.sink.screen = (text, rows)
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
        rows = _rows(kw["reply_markup"]) if kw.get("reply_markup") is not None and getattr(kw["reply_markup"], "inline_keyboard", None) else []
        service = all(d.startswith(("m:stop:", "view:", "stop:")) for r in rows for _, d in r)  # «Думаю…»/«Зупинити», «Чистий текст» — не окремий екран
        if rows and not service and self.sink.screen is None:
            self.sink.screen = (text, rows)
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

    async def _send_media(self, method, kind, chat_id, payload, *a, **kw):
        if int(chat_id) == self.uid:  # у чат цієї людини — і копія для застосунку
            payload = _capture_media(self.sink, kind, payload, kw)
        return await getattr(self.real, method)(chat_id, payload, *a, **kw)

    async def send_photo(self, chat_id, photo, *a, **kw):
        return await self._send_media("send_photo", "photo", chat_id, photo, *a, **kw)

    async def send_document(self, chat_id, document, *a, **kw):
        return await self._send_media("send_document", "document", chat_id, document, *a, **kw)

    async def send_video(self, chat_id, video, *a, **kw):
        return await self._send_media("send_video", "video", chat_id, video, *a, **kw)

    async def send_audio(self, chat_id, audio, *a, **kw):
        return await self._send_media("send_audio", "audio", chat_id, audio, *a, **kw)

    async def send_voice(self, chat_id, voice, *a, **kw):
        return await self._send_media("send_voice", "voice", chat_id, voice, *a, **kw)

    async def send_animation(self, chat_id, animation, *a, **kw):
        return await self._send_media("send_animation", "animation", chat_id, animation, *a, **kw)


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
        rr = [(t, d) for t, d in r if d and not d.startswith(("m:nav", "imp:", "ui:", "copy:", "app:")) and d not in ("m:home",)]
        if rr:
            out.append(rr)
    return out


def history(B, uid, limit=40):
    """Поточна розмова з ШІ — та сама, що в чаті."""
    did = B.current_dialog(uid)
    with B.closing(B.db()) as con:
        rows = con.execute("SELECT id, role, text, provider, ts FROM messages WHERE dialog_id=? AND pending=0 ORDER BY id DESC LIMIT ?",
                           (did, limit)).fetchall()
    title = B.dialog_name(did) if did else "Нова розмова"
    d = B.dialog_get(did) or {}
    f = B.folder_get(d["folder_id"]) if d.get("folder_id") else None
    ch = B.get_choice(uid)
    models = [{"id": "auto", "title": "🔁 Авто", "on": ch == "auto"}] + [
        {"id": k, "title": f"{B.BADGE.get(k, '🤖')} {v['title']}", "on": ch == k} for k, v in B.PROVIDERS.items()]
    return {"dialog": title, "limits": B.limits_brief(short=True), "models": models, "avatars": B.avatars(uid), "did": did, "folder": f["name"] if f else "",
            "reply": (B.APP_REPLY.get(uid) or "")[:300],
            "messages": [{"id": r["id"], "role": r["role"], "text": r["text"][:6000], "provider": r["provider"] or "", "ts": r["ts"]} for r in rows[::-1]]}


def chats(B, uid):
    """Усі розмови й теки — для перемикача в застосунку."""
    cur = B.current_dialog(uid)
    return {"current": cur, "folders": [{"id": f["id"], "name": f["name"]} for f in B.folders_list(uid)],
            "dialogs": [{"id": d["id"], "title": d["title"], "folder": d["folder_id"], "n": d["n"], "updated": d["updated"], "pinned": bool(d.get("pinned_at"))}
                        for d in B.dialogs_list(uid, limit=200)]}


def chat_action(B, uid, body):
    """Дії з розмовами в застосунку: нова, перейти, у теку, перейменувати, нова тека, гілка, відповідь на повідомлення."""
    act = str(body.get("act") or "")
    did = int(body.get("did") or 0) or None
    own = lambda x: (B.dialog_get(x) or {}).get("chat_id") == uid  # noqa: E731
    name = str(body.get("name") or "").strip()[:60]
    fid = body.get("folder")
    fid = int(fid) if str(fid or "").isdigit() and (B.folder_get(int(fid)) or {}).get("chat_id") == uid else None
    if act == "new":
        B.dialog_create(uid, name or "Нова розмова", fid)
    elif act == "switch" and did and own(did):
        B.set_pref(uid, "dialog", did)
    elif act == "move" and did and own(did):
        B.dialog_update(did, folder_id=fid)
    elif act == "rename" and did and own(did) and name:
        B.dialog_update(did, title=name)
    elif act == "folder" and name:
        new = B.folder_create(uid, name)
        if did and own(did):
            B.dialog_update(did, folder_id=new)
    elif act in ("branch", "reply"):
        mid = int(body.get("mid") or 0)
        with B.closing(B.db()) as con:
            m = con.execute("SELECT dialog_id, text FROM messages WHERE id=? AND chat_id=?", (mid, uid)).fetchone()
        if not m:
            return {"error": "Повідомлення не знайдено"}
        if act == "branch":  # 🌿 нова розмова з історією до цього повідомлення
            B.dialog_branch(uid, m["dialog_id"], mid)
        else:  # ↩️ відповісти: у цій розмові або в іншій (target)
            target = int(body.get("target") or 0) or m["dialog_id"]
            if own(target):
                B.set_pref(uid, "dialog", target)
            B.APP_REPLY[uid] = m["text"][:1500]
    elif act == "noreply":
        B.APP_REPLY.pop(uid, None)
    elif act == "del":  # 🗑 видалити повідомлення і з розмови, і з чату з ботом
        import asyncio
        mid = int(body.get("mid") or 0)
        bot = B.tg_app.bot if B.tg_app else None
        if mid and bot:
            asyncio.get_running_loop().create_task(B.delete_everywhere(bot, uid, db_ids=[mid]))
        elif mid:
            with B.closing(B.db()) as con, con:
                con.execute("DELETE FROM messages WHERE id=? AND chat_id=?", (mid, uid))
    return chats(B, uid)


def menu(B, uid):
    """Меню для застосунку: усі розділи одразу розгорнуті (без ▼/▲ і зайвих запитів), з тими ж назвами й порядком, що в чаті."""
    import explain
    from uilayout import PLACE_HOME
    L = B.L
    B._migrate_compact_menu(uid)

    def items(place, path):
        own, subs = [], []
        for i in L.in_place(uid, place):
            if i.startswith("g:"):
                subs += items(i, path + [L.label(uid, i)])
            elif i not in ("menu", "app"):
                o = (B.prefs(uid).get("hint_over") or {}).get(__import__("button_hints").norm_label(L.label(uid, i))) or {}
                own.append({"key": i, "label": L.label(uid, i), "data": L.target(i), "hint": o.get("t", explain.short(i)), "hs": o.get("s", 0),
                            "why": f"m:why:{i}" if i in explain.E else ""})
        return ([{"title": " › ".join(path), "items": own}] if own else []) + subs

    sections = items(PLACE_HOME, [])
    if sections and not sections[0]["title"]:
        sections[0]["title"] = "Швидкий доступ"
    g = B.home_counts(uid)  # те саме привітання й лічильник справ, що й на головному екрані бота
    greet = re.sub(r"<[^>]+>", "", B.home_greeting_html(g))
    unseen = B.FEED.unseen(uid) if hasattr(B, "FEED") else 0
    info = [("📓", "Завдань", str(g["total"]), "m:hub:zap"),
            ("🌅", "На сьогодні", str(g["today"]), "m:today")]
    if g["red"]:
        info.append(("🗞", "Редакційні", str(g["red"]), "m:pl:red"))
    info.append(("🔔", "Події", f"{unseen} нових" if unseen else "немає нових", "feed"))
    return {"menu": True, "greet": greet, "info": [{"icon": a, "label": b, "value": c, "data": e} for a, b, c, e in info], "sections": sections,
            "tabs": tabs(B, uid), "tabs_max": TABS_MAX}


TABS_MAX = 4  # + «☰ Меню», яке лишається завжди
TABS_DEFAULT = ["chat", "today", "zap", "nr"]
TABS_RENAMED = {"news": "nr", "stk": "cr", "med": "cr", "tl": "zap", "nt": "zap", "utm": "nr", "col": "nr", "red": "nr"}  # старі кнопки, що тепер усередині нових розділів


def tabs(B, uid):
    """Панель унизу застосунку: вибрані кнопки (налаштовує сам користувач)."""
    keys = list(dict.fromkeys(TABS_RENAMED.get(k, k) for k in (B.prefs(uid).get("app_tabs") or TABS_DEFAULT)))
    out = []
    for k in keys:
        if k == "chat":
            out.append({"key": "chat", "label": "💬 Розмова", "data": "chat"})
        elif k == "feed":
            out.append({"key": "feed", "label": "🔔 Події", "data": "feed"})
        elif k in getattr(B.L, "admin_items", set()) and not B.is_admin_id(uid):
            continue  # розділи лише для власника (📰 Ньюзрум тощо) учаснику не показуємо
        elif k in B.ITEMS and k not in ("menu", "app") and B.ITEMS[k][1].startswith(("m:", "help:")):
            out.append({"key": k, "label": B.L.label(uid, k), "data": B.ITEMS[k][1]})
    return out[:TABS_MAX]


def save_tabs(B, uid, keys):
    keys = [str(k) for k in (keys or []) if k in ("chat", "feed") or k in B.ITEMS][:TABS_MAX]
    B.set_pref(uid, "app_tabs", list(dict.fromkeys(keys)))
    return menu(B, uid)


def deeplink(B, data):
    """«m:start:<параметр>» — посилання t.me/…?start=… з тексту відкриваємо прямо в застосунку, а не в чаті."""
    if not (data or "").startswith("m:start:"):
        return data
    arg = data[8:]
    news = B.PLUGINS.get("news")
    m = re.fullmatch(r"obb_([a-z]+)_(\d{8})", arg)
    if m and news and hasattr(news, "jump_target"):
        import datetime as _dt
        try:
            d = _dt.datetime.strptime(m.group(2), "%Y%m%d").date()
        except ValueError:
            d = _dt.datetime.now(B.TZ).date()
        return news.jump_target(m.group(1), d)[1]
    m = re.fullmatch(r"obl_([a-z]+)_(\d{8})", arg)
    if m and news and hasattr(news, "cb"):
        return news.cb("obl", f"{m.group(1)}.{m.group(2)}")
    m = re.fullmatch(r"inb_(\d+)", arg)
    if m:
        return f"m:pl:inb:t:{m.group(1)}"
    return "m:home"


async def run(bot_module, uid, op, data=None, text=None, user=None, file=None):
    """op: screen | text | say | file | history. -> {"text", "rows", "toast", "notes", "awaiting", "replies"}"""
    B = bot_module
    if op == "history":
        return history(B, uid)
    if op == "menu":
        return menu(B, uid)
    getattr(B, "COPY_CARDS", {}).pop(uid, None)  # картки лише від цієї дії
    try:
        import uilayout
        uilayout.SCOPE.set("app")  # у застосунку своя історія «Назад», окремо від чату
    except Exception:  # noqa: BLE001
        pass
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
                await B.handle_awaited(upd, _Ctx(capbot or tbot), text.strip())
            else:
                sink.notes.append("Тут текст не очікувався. Відкрийте розділ і натисніть потрібну дію.")
        else:
            data = deeplink(B, data)
            upd = _Update(uid, sink, tbot, data=data, user=user)
            await (B.open_data if (data or "").startswith("help:") else B.route)(upd, _Ctx(capbot or tbot), data or "m:home", edit=True, record=False)
    finally:
        CAPTURE.reset(token)
    text_out, rows = sink.screen or ("", [])
    replies = [t for t in (capbot.msgs.values() if capbot else []) if t and not t.startswith(("⏳", "🕒", "💭"))]
    import button_hints
    rows = clean_rows(rows)
    secs = getattr(B, "HUB_LAST", {}).pop(uid, None) if op == "screen" else None
    if secs:  # об'єднаний розділ — частинами, щоб кожну можна було згорнути
        for sec in secs:
            sec["rows"] = clean_rows([[(t, d) for t, d in r if d != "m:noop"] for r in sec["rows"]])
            sec["hints"] = button_hints.apply_over(sec["rows"], button_hints.for_rows(sec["rows"]), B.prefs(uid).get("hint_over"))
    cc = getattr(B, "COPY_CARDS", {}).pop(uid, None)
    cards = None
    if cc:  # посилання — окремими віконцями з «Скопіювати»; текст із тими ж посиланнями вже не потрібен
        cards, text_out = cc["cards"], cc["head"]
    poll = next((d for r in rows for t, d in r if t.startswith("🔄 Оновити стан")), None)  # екран зі станом — застосунок оновлює сам
    media = (getattr(B, "APP_MEDIA", {}).pop(uid, None) or []) + sink.media or None
    if sink.media:
        sink.notes.append("📲 Те саме є й у чаті з ботом")
    return {"media": media, "poll": poll, "cards": cards, "text": text_out, "rows": rows, "hints": button_hints.apply_over(rows, button_hints.for_rows(rows), B.prefs(uid).get("hint_over")), "sections": secs or None, "toast": sink.toast, "notes": sink.notes[-5:], "awaiting": uid in B.AWAIT,
            "view": bool(op == "screen" and data and B.is_view(data)),
            "replies": [re.sub(r"<[^>]+>", "", t)[:6000] for t in replies[-5:]]}


def page():
    try:
        return PAGE.read_text("utf-8")
    except OSError:
        return None


def save_hint(B, uid, label, text, size):
    """Власний підпис під кнопкою: текст ("" — сховати, None — повернути стандартний) і розмір шрифту (0 — авто)."""
    import button_hints
    over = dict(B.prefs(uid).get("hint_over") or {})
    key = button_hints.norm_label(label)
    if text is None:
        over.pop(key, None)
    else:
        over[key] = {"t": str(text)[:120], "s": max(0, min(16, float(size or 0)))}
    B.set_pref(uid, "hint_over", over)
    return {"ok": True}
