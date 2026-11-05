"""🏘 Спільноти: бот у групах, каналах і спільнотах.

Що вміє:
• 🛡 Правила — заборони для учасників (посилання, пересилання, медіа, стікери, голосові, згадки, слова, КАПС, флуд,
  тиша для новачків, нічна тиша). Адміністраторів чату правила не стосуються.
• ⚖️ Покарання — видалити / видалити й попередити / тимчасово заглушити / вигнати; після N попереджень — тиша на добу.
• 🙋 Дозволи за запитом — учасник натискає «Попросити дозвіл», власник бота отримує запит і дає дозвіл
  на годину / день / назавжди або відмовляє. Так само — запит на розблокування.
• 🚪 Вступ — заявки на вступ: приймати всіх, питати власника або не чіпати; привітання новачків.
• 📣 Публікації — дописи в канал або групу: зараз, один раз у певний час, щодня, щотижня; дописи від ШІ.
• 👁 Моніторинг — слова-тригери, пересилання власнику; журнал дій і цифри.

Платформи:
• Telegram — усе вище (бот має бути адміністратором з правами видаляти й обмежувати).
• WhatsApp (Cloud API Groups) — публікації в групи вашого номера, заявки на вступ, видалення учасника.
  Видаляти чужі повідомлення WhatsApp не дозволяє — замість цього попередження й видалення з групи.
• Instagram — модерація коментарів під вашими дописами (сховати/видалити) і публікація дописів з картинкою.
"""
import asyncio
import html
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timedelta

log = logging.getLogger("community")

RULES = [
    ("silence", "🤐 Писати взагалі", "писати в чаті"),
    ("links", "🔗 Посилання", "надсилати посилання"),
    ("forwards", "↪️ Пересилання", "пересилати повідомлення"),
    ("media", "🖼 Фото, відео, файли", "надсилати фото, відео й файли"),
    ("stickers", "🎭 Стікери й GIF", "надсилати стікери й GIF"),
    ("voice", "🎤 Голосові й кружечки", "надсилати голосові"),
    ("mentions", "📣 Згадки @", "згадувати інших через @"),
    ("words", "🤬 Заборонені слова", "вживати слова зі списку"),
    ("caps", "🔠 КАПС", "писати великими літерами"),
    ("flood", "🌊 Флуд", "писати дуже часто"),
    ("newbies", "🐣 Тиша для новачків", "писати одразу після вступу"),
    ("night", "🌙 Нічна тиша", "писати вночі"),
]
RULE_T = {k: t for k, t, _ in RULES}
RULE_ASK = {k: a for k, _, a in RULES}
RULE_ASK["unmute"] = "знову писати в чаті (зняти обмеження)"
TEXT_RULES = {"silence", "links", "mentions", "words", "caps", "flood"}  # те, що можна перевірити в WhatsApp-групах і коментарях Instagram
ACTIONS = [("delete", "🗑 Видалити"), ("warn", "⚠️ Видалити й попередити"), ("mute", "🔇 Заглушити"), ("ban", "⛔ Вигнати")]
ACTION_T = dict(ACTIONS)
IG_ACTIONS = [("hide", "🙈 Сховати коментар"), ("delete", "🗑 Видалити коментар")]
JOINS = [("off", "Не чіпати"), ("auto", "✅ Приймати всіх"), ("ask", "🙋 Питати мене")]
MUTES = [10, 60, 180, 1440, 10080]
PERMITS = [("1h", "1 год", 3600), ("1d", "1 день", 86400), ("7d", "7 днів", 7 * 86400), ("inf", "Назавжди", 0)]
WEEK = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
KIND = {"group": "👥", "supergroup": "👥", "channel": "📢", "wa_group": "🟢", "ig": "📸"}
KIND_T = {"group": "група", "supergroup": "група", "channel": "канал", "wa_group": "група WhatsApp", "ig": "коментарі Instagram"}
URL_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|wa\.me/|chat\.whatsapp\.com/|\b[\w-]+\.(com|net|org|ua|ru|io|me|link|ly|xyz|info|site|shop)\b)", re.I)
MENTION_RE = re.compile(r"(^|\s)@\w{4,}")

DEFAULT_CFG = {
    "rules": {k: 0 for k, _, _ in RULES},
    "words": [], "flood_n": 5, "flood_sec": 10, "newbie_min": 60, "night": "23:00-07:00",
    "action": "warn", "ig_action": "hide", "warn_limit": 3, "mute_min": 60,
    "joins": "off", "greet": "", "requests": 1,
    "watch": [], "watch_all": 0, "notify": 1, "enabled": 1,
}


def esc(t):
    return html.escape(str(t or ""), quote=False)


def short(t, n=60):
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def dur(sec):
    if not sec:
        return "назавжди"
    if sec < 3600:
        return f"{sec // 60} хв"
    if sec < 86400:
        return f"{sec // 3600} год"
    return f"{sec // 86400} дн"


def mins_t(m):
    return f"{m} хв" if m < 60 else f"{m // 60} год" if m < 1440 else f"{m // 1440} дн"


def in_window(window, now_hm):
    try:
        a, b = window.split("-")
    except ValueError:
        return False
    return (a <= now_hm < b) if a <= b else (now_hm >= a or now_hm < b)


class Community:
    def __init__(self, db, owners, tz=None, ai=None, on_event=None):
        """db — функція, що повертає sqlite-з'єднання з row_factory=Row; owners() — список id власників (адмінів бота);
        ai(prompt) — корутина, що повертає текст (для дописів від ШІ), або None; on_event(text) — куди ще повідомляти (головний бот)."""
        self.db, self.owners, self.tz, self.ai, self.on_event = db, owners, tz, ai, on_event
        self.bot = None
        self.bridge = None
        self.awaiting = {}
        self.wiz = {}            # майстер «хто що може»: chat -> {scope, target, home, keys, copy}
        self.admins = {}         # cid -> (коли, set(id))
        self.flood = defaultdict(lambda: deque(maxlen=50))
        self.last_wa_poll = 0
        self.init_db()
        from priority import Ranks
        self.ranks = Ranks(lambda k: json.dumps(self.gget(k)) if self.gget(k) is not None else None,
                           lambda k, v: self.gset(k, json.loads(v)), prefix="cmrank:")  # 🏆 які спільноти важливіші

    # ------------------------------------------------------------ база
    def init_db(self):
        with closing(self.db()) as con, con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS cm_chats (id TEXT PRIMARY KEY, platform TEXT, chat_id TEXT, title TEXT, kind TEXT,
                rights TEXT DEFAULT '{}', added_by TEXT, created REAL, cfg TEXT DEFAULT '{}', stats TEXT DEFAULT '{}', state TEXT DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS cm_permits (cid TEXT, user_id TEXT, rule TEXT, until REAL, name TEXT, PRIMARY KEY (cid, user_id, rule));
            CREATE TABLE IF NOT EXISTS cm_warns (cid TEXT, user_id TEXT, n INTEGER, last REAL, PRIMARY KEY (cid, user_id));
            CREATE TABLE IF NOT EXISTS cm_members (cid TEXT, user_id TEXT, joined REAL, PRIMARY KEY (cid, user_id));
            CREATE TABLE IF NOT EXISTS cm_reqs (id INTEGER PRIMARY KEY AUTOINCREMENT, cid TEXT, user_id TEXT, name TEXT, rule TEXT,
                created REAL, status TEXT DEFAULT 'open', kind TEXT DEFAULT 'permit', ref TEXT);
            CREATE TABLE IF NOT EXISTS cm_posts (id INTEGER PRIMARY KEY AUTOINCREMENT, cid TEXT, text TEXT, kind TEXT, sched TEXT,
                last_run REAL DEFAULT 0, status TEXT DEFAULT 'active', created REAL, image TEXT);
            CREATE TABLE IF NOT EXISTS cm_log (id INTEGER PRIMARY KEY AUTOINCREMENT, cid TEXT, t REAL, text TEXT);
            CREATE TABLE IF NOT EXISTS cm_global (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS cm_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, created REAL);
            CREATE TABLE IF NOT EXISTS cm_gmembers (gid INTEGER, user_id TEXT, name TEXT, PRIMARY KEY (gid, user_id));
            CREATE TABLE IF NOT EXISTS cm_policy (scope TEXT, target TEXT, rule TEXT, value TEXT, PRIMARY KEY (scope, target, rule));
            """)
            for table, col in (("cm_members", "name TEXT"), ("cm_members", "username TEXT"), ("cm_members", "last REAL DEFAULT 0"),
                               ("cm_chats", "username TEXT"), ("cm_chats", "link TEXT"), ("cm_chats", "members INTEGER"), ("cm_chats", "checked REAL DEFAULT 0")):
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                except Exception:  # noqa: BLE001
                    pass

    def q(self, sql, args=(), one=False):
        with closing(self.db()) as con:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        return (rows[0] if rows else None) if one else rows

    def x(self, sql, args=()):
        with closing(self.db()) as con, con:
            return con.execute(sql, args).lastrowid

    def gget(self, k, default=None):
        r = self.q("SELECT v FROM cm_global WHERE k=?", (k,), one=True)
        return json.loads(r["v"]) if r else default

    def gset(self, k, v):
        self.x("INSERT OR REPLACE INTO cm_global(k, v) VALUES (?,?)", (k, json.dumps(v)))

    def only_owner_add(self):
        return self.gget("only_owner_add", 1)

    def chat(self, cid):
        c = self.q("SELECT * FROM cm_chats WHERE id=?", (cid,), one=True)
        if c:
            cfg = json.loads(c["cfg"] or "{}")
            full = json.loads(json.dumps(DEFAULT_CFG))
            full.update({k: v for k, v in cfg.items() if k != "rules"})
            full["rules"].update(cfg.get("rules", {}))
            c["cfg"], c["stats"], c["rights"] = full, json.loads(c["stats"] or "{}"), json.loads(c["rights"] or "{}")
        return c

    def chats(self):
        return [self.chat(r["id"]) for r in self.q("SELECT id FROM cm_chats WHERE state='active' ORDER BY created")]

    def save_cfg(self, cid, cfg):
        self.x("UPDATE cm_chats SET cfg=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), cid))

    def register(self, platform, chat_id, title, kind, added_by=None, rights=None):
        cid = f"{platform}:{chat_id}"
        if self.q("SELECT id FROM cm_chats WHERE id=?", (cid,), one=True):
            self.x("UPDATE cm_chats SET title=?, kind=?, state='active'" + (", rights=?" if rights is not None else "") + " WHERE id=?",
                   (title, kind, *([json.dumps(rights)] if rights is not None else []), cid))
        else:
            self.x("INSERT INTO cm_chats(id, platform, chat_id, title, kind, rights, added_by, created) VALUES (?,?,?,?,?,?,?,?)",
                   (cid, platform, str(chat_id), title, kind, json.dumps(rights or {}), str(added_by or ""), time.time()))
        return cid

    def stat(self, cid, key, n=1):
        c = self.q("SELECT stats FROM cm_chats WHERE id=?", (cid,), one=True)
        if not c:
            return
        s = json.loads(c["stats"] or "{}")
        s[key] = s.get(key, 0) + n
        self.x("UPDATE cm_chats SET stats=? WHERE id=?", (json.dumps(s), cid))

    def log(self, cid, text):
        self.x("INSERT INTO cm_log(cid, t, text) VALUES (?,?,?)", (cid, time.time(), text))
        self.x("DELETE FROM cm_log WHERE cid=? AND id NOT IN (SELECT id FROM cm_log WHERE cid=? ORDER BY id DESC LIMIT 200)", (cid, cid))

    def permit_ok(self, cid, uid, rule):
        r = self.q("SELECT until FROM cm_permits WHERE cid=? AND user_id=? AND rule IN (?, '*')", (cid, str(uid), rule))
        return any(not p["until"] or p["until"] > time.time() for p in r)

    def now(self):
        return datetime.now(self.tz) if self.tz else datetime.now()

    # ------------------------------------------------------------ повідомлення власнику
    async def notify(self, text, rows=None):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d[:64]) for t, d in r] for r in rows]) if rows else None
        for o in self.owners():
            try:
                await self.bot.send_message(o, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            except Exception as e:  # noqa: BLE001
                log.warning("не вдалося сповістити %s: %s", o, e)
        if self.on_event:
            try:
                await self.on_event(re.sub(r"<[^>]+>", "", text))
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ перевірка правил
    def violation(self, c, uid, text, kinds, uname=None):
        """kinds — множина ознак повідомлення: forward, media, sticker, voice, link, mention. Повертає ключ правила або None.
        Що заборонено саме цій людині — з урахуванням «👤 Хто що може» (усі / групи / окремі учасники, ця чи всі спільноти)."""
        cfg = c["cfg"]
        r = {k: self.forbidden(c, uid, uname, k) for k, _, _ in RULES}
        if r["silence"] and not self.permit_ok(c["id"], uid, "silence"):
            return "silence"
        text = text or ""
        low = text.lower()
        checks = []
        if r.get("links") and ("link" in kinds or URL_RE.search(text)):
            checks.append("links")
        if r.get("forwards") and "forward" in kinds:
            checks.append("forwards")
        if r.get("media") and "media" in kinds:
            checks.append("media")
        if r.get("stickers") and "sticker" in kinds:
            checks.append("stickers")
        if r.get("voice") and "voice" in kinds:
            checks.append("voice")
        if r.get("mentions") and ("mention" in kinds or MENTION_RE.search(text)):
            checks.append("mentions")
        if r.get("words") and cfg["words"] and any(re.search(r"(?<!\w)" + re.escape(w.lower()), low) for w in cfg["words"] if w):
            checks.append("words")
        letters = [ch for ch in text if ch.isalpha()]
        if r.get("caps") and len(letters) >= 12 and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:
            checks.append("caps")
        if r.get("flood"):
            dq = self.flood[(c["id"], str(uid))]
            now = time.time()
            dq.append(now)
            if sum(1 for t in dq if now - t <= cfg["flood_sec"]) > cfg["flood_n"]:
                checks.append("flood")
        if r.get("newbies"):
            m = self.q("SELECT joined FROM cm_members WHERE cid=? AND user_id=?", (c["id"], str(uid)), one=True)
            if m and time.time() - m["joined"] < cfg["newbie_min"] * 60:
                checks.append("newbies")
        if r.get("night") and in_window(cfg["night"], self.now().strftime("%H:%M")):
            checks.append("night")
        for k in checks:
            if not self.permit_ok(c["id"], uid, k):
                return k
        return None

    def watch_hit(self, c, text):
        cfg = c["cfg"]
        if cfg["watch_all"]:
            return "усе"
        low = (text or "").lower()
        for w in cfg["watch"]:
            if w and w.lower() in low:
                return w
        return None

    def add_warn(self, cid, uid):
        w = self.q("SELECT n FROM cm_warns WHERE cid=? AND user_id=?", (cid, str(uid)), one=True)
        n = (w["n"] if w else 0) + 1
        self.x("INSERT OR REPLACE INTO cm_warns(cid, user_id, n, last) VALUES (?,?,?,?)", (cid, str(uid), n, time.time()))
        return n

    # ------------------------------------------------------------ Telegram
    def install_tg(self, app, pass_bots=False):
        from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberHandler,
                                  MessageHandler, TypeHandler, filters)
        from telegram import Update

        async def on_group(update, ctx):
            self.bot = ctx.bot
            try:
                await self.tg_message(update, ctx)
            except Exception:  # noqa: BLE001
                log.exception("спільноти: помилка")
            u = update.effective_user
            if pass_bots and u and u.is_bot:
                return  # головний бот приймає повідомлення інших ботів і з груп
            raise ApplicationHandlerStop

        async def on_member(update, ctx):
            self.bot = ctx.bot
            try:
                await self.tg_member(update, ctx)
            except Exception:  # noqa: BLE001
                log.exception("спільноти: помилка учасника")
            raise ApplicationHandlerStop

        async def on_join(update, ctx):
            self.bot = ctx.bot
            await self.tg_join(update, ctx)
            raise ApplicationHandlerStop

        async def on_cb(update, ctx):
            self.bot = ctx.bot
            await self.tg_callback(update, ctx)
            raise ApplicationHandlerStop

        async def remember_bot(update, ctx):
            self.bot = ctx.bot

        app.add_handler(TypeHandler(Update, remember_bot), group=-2)
        app.add_handler(ChatMemberHandler(on_member, ChatMemberHandler.ANY_CHAT_MEMBER), group=-1)
        app.add_handler(ChatJoinRequestHandler(on_join), group=-1)
        app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^cm:"), group=-1)
        app.add_handler(MessageHandler(filters.ChatType.GROUPS | filters.ChatType.CHANNEL | filters.UpdateType.CHANNEL_POSTS, on_group), group=-1)

    async def tg_rights(self, chat_id):
        try:
            me = await self.bot.get_chat_member(chat_id, self.bot.id)
        except Exception:  # noqa: BLE001
            return {}
        st = getattr(me, "status", "")
        return {"admin": st in ("administrator", "creator"), "delete": bool(getattr(me, "can_delete_messages", False)),
                "restrict": bool(getattr(me, "can_restrict_members", False)), "invite": bool(getattr(me, "can_invite_users", False)),
                "post": bool(getattr(me, "can_post_messages", st == "administrator")), "pin": bool(getattr(me, "can_pin_messages", False))}

    async def is_chat_admin(self, cid, chat_id, uid):
        ts, ids = self.admins.get(cid, (0, set()))
        if time.time() - ts > 600:
            try:
                ids = {a.user.id for a in await self.bot.get_chat_administrators(chat_id)}
            except Exception:  # noqa: BLE001
                ids = set()
            self.admins[cid] = (time.time(), ids)
        return uid in ids or str(uid) in {str(o) for o in self.owners()}

    async def tg_member(self, update, ctx):
        mcm = update.my_chat_member
        if mcm:  # змінився статус самого бота в чаті
            chat, new = mcm.chat, mcm.new_chat_member.status
            cid = f"tg:{chat.id}"
            by = mcm.from_user
            if new in ("left", "kicked"):
                self.x("UPDATE cm_chats SET state='left' WHERE id=?", (cid,))
                return await self.notify(f"🚪 Бота прибрали з «{esc(chat.title)}».")
            owner = str(by.id) in {str(o) for o in self.owners()}
            if self.only_owner_add() and not owner and not self.q("SELECT id FROM cm_chats WHERE id=? AND state='active'", (cid,), one=True):
                await self.notify(f"🛑 Хтось ({esc(by.full_name)}, @{esc(by.username or '—')}) додав бота в «{esc(chat.title)}». "
                                  "Бот вийшов звідти: додавати його може лише власник (змінити: 🏘 Спільноти → 🔒).")
                try:
                    await ctx.bot.leave_chat(chat.id)
                except Exception:  # noqa: BLE001
                    pass
                return
            self.bot = ctx.bot
            rights = await self.tg_rights(chat.id)
            cid = self.register("tg", chat.id, chat.title or str(chat.id), chat.type, by.id, rights)
            self.log(cid, f"бота додано ({'адмін' if rights.get('admin') else 'учасник'})")
            await self.refresh_info(self.chat(cid))
            await self.notify(f"{KIND.get(chat.type, '👥')} <b>Бот у «{esc(chat.title)}»</b> ({KIND_T.get(chat.type, chat.type)})\n" + self.rights_text(chat.type, rights),
                              [[("⚙️ Налаштувати", f"m:cm:c:{cid}")]])
            return
        cm = update.chat_member
        if cm:
            cid = f"tg:{cm.chat.id}"
            c = self.chat(cid)
            if not c:
                return
            old, new = cm.old_chat_member.status, cm.new_chat_member.status
            u = cm.new_chat_member.user
            if old in ("left", "kicked") and new in ("member", "restricted"):
                self.x("INSERT OR REPLACE INTO cm_members(cid, user_id, joined) VALUES (?,?,?)", (cid, str(u.id), time.time()))
                self.stat(cid, "joined")
                if c["cfg"]["greet"]:
                    await self.say_temp(cm.chat.id, c["cfg"]["greet"].replace("{name}", u.mention_html()), 300, html_mode=True)

    async def tg_join(self, update, ctx):
        r = update.chat_join_request
        cid = f"tg:{r.chat.id}"
        c = self.chat(cid)
        if not c or c["cfg"]["joins"] == "off":
            return
        u = r.from_user
        if c["cfg"]["joins"] == "auto":
            await r.approve()
            self.stat(cid, "approved")
            self.log(cid, f"прийнято заявку: {u.full_name}")
            return
        rid = self.x("INSERT INTO cm_reqs(cid, user_id, name, rule, created, kind) VALUES (?,?,?,?,?, 'join')",
                     (cid, str(u.id), u.full_name + (f" @{u.username}" if u.username else ""), "join", time.time()))
        await self.notify(f"🚪 <b>Заявка на вступ</b> у «{esc(c['title'])}»\n{esc(u.full_name)}" + (f" @{esc(u.username)}" if u.username else "")
                          + (f"\n«{esc(short(r.bio, 120))}»" if getattr(r, "bio", None) else ""),
                          [[("✅ Прийняти", f"cm:ja:{rid}"), ("❌ Відхилити", f"cm:jd:{rid}")]])

    @staticmethod
    def tg_kinds(m):
        k = set()
        if getattr(m, "forward_origin", None):
            k.add("forward")
        if m.photo or m.video or m.document or m.audio:
            k.add("media")
        if m.sticker or m.animation:
            k.add("sticker")
        if m.voice or m.video_note:
            k.add("voice")
        for e in (m.entities or []) + (m.caption_entities or []):
            if e.type in ("url", "text_link"):
                k.add("link")
            if e.type in ("mention", "text_mention"):
                k.add("mention")
        return k

    async def tg_message(self, update, ctx):
        m = update.effective_message
        chat = update.effective_chat
        if not m or not chat:
            return
        cid = f"tg:{chat.id}"
        c = self.chat(cid)
        if not c:  # бот уже був у чаті до появи цього розділу — реєструємо тихо
            rights = await self.tg_rights(chat.id)
            cid = self.register("tg", chat.id, chat.title or str(chat.id), chat.type, None, rights)
            c = self.chat(cid)
        if m.new_chat_members:
            for u in m.new_chat_members:
                if not u.is_bot:
                    self.x("INSERT OR REPLACE INTO cm_members(cid, user_id, joined) VALUES (?,?,?)", (cid, str(u.id), time.time()))
            return
        if m.left_chat_member or m.pinned_message or m.new_chat_title:
            return
        self.stat(cid, "seen")
        text = m.text or m.caption or ""
        u = update.effective_user
        uname = (u.full_name if u else (m.sender_chat.title if m.sender_chat else "?"))
        hit = self.watch_hit(c, text) if text else None
        if hit:
            self.stat(cid, "watched")
            await self.notify(f"👁 <b>{esc(c['title'])}</b> · {esc(uname)}" + (f" (слово «{esc(hit)}»)" if hit != "усе" else "") + f"\n{esc(short(text, 700))}",
                              [[("📜 Журнал", f"m:cm:lg:{cid}")]])
        if not c["cfg"]["enabled"] or chat.type == "channel" or not u or u.is_bot:
            return
        if m.sender_chat and m.sender_chat.id == chat.id:
            return  # анонімний адмін
        if text.startswith("/") and await self.tg_command(update, c, text):
            return
        if await self.is_chat_admin(cid, chat.id, u.id):
            return
        self.seen_member(cid, u.id, u.full_name, u.username)
        rule = self.violation(c, u.id, text, self.tg_kinds(m), u.username)
        if rule:
            await self.punish_tg(c, m, u, rule)

    async def punish_tg(self, c, m, u, rule):
        cid, chat_id, cfg = c["id"], m.chat_id, c["cfg"]
        action = cfg["action"]
        try:
            await m.delete()
            self.stat(cid, "deleted")
        except Exception:  # noqa: BLE001
            self.log(cid, "⚠️ немає права видаляти повідомлення")
        who = u.mention_html()
        rows = [[("🙋 Попросити дозвіл", f"cm:rq:{cid}:{rule}:{u.id}")]] if cfg["requests"] else None
        if action == "warn":
            n = self.add_warn(cid, u.id)
            self.stat(cid, "warned")
            if n >= cfg["warn_limit"]:
                await self.restrict(chat_id, u.id, 1440)
                self.x("DELETE FROM cm_warns WHERE cid=? AND user_id=?", (cid, str(u.id)))
                self.stat(cid, "muted")
                self.log(cid, f"🔇 {u.full_name}: {cfg['warn_limit']} попередження — тиша на добу")
                await self.say_temp(chat_id, f"🔇 {who}, це вже {n}-те порушення — тиша на добу.", 120, html_mode=True,
                                    rows=[[("🙋 Попросити розблокувати", f"cm:rq:{cid}:unmute:{u.id}")]] if cfg["requests"] else None)
                await self.notify(f"🔇 <b>{esc(c['title'])}</b>: {esc(u.full_name)} заглушено на добу ({esc(RULE_T[rule])}).",
                                  [[("🔈 Зняти", f"cm:um:{cid}:{u.id}")]])
            else:
                await self.say_temp(chat_id, f"⚠️ {who}, тут не можна: {RULE_T[rule].split(' ', 1)[1].lower()}. Попередження {n}/{cfg['warn_limit']}.",
                                    60, html_mode=True, rows=rows)
            self.log(cid, f"⚠️ {u.full_name}: {RULE_T[rule]} (попередження {n})")
        elif action == "mute":
            await self.restrict(chat_id, u.id, cfg["mute_min"])
            self.stat(cid, "muted")
            self.log(cid, f"🔇 {u.full_name}: {RULE_T[rule]} — {mins_t(cfg['mute_min'])}")
            await self.say_temp(chat_id, f"🔇 {who} — тиша на {mins_t(cfg['mute_min'])}: {RULE_T[rule].split(' ', 1)[1].lower()}.", 90, html_mode=True,
                                rows=[[("🙋 Попросити розблокувати", f"cm:rq:{cid}:unmute:{u.id}")]] if cfg["requests"] else None)
        elif action == "ban":
            try:
                await self.bot.ban_chat_member(chat_id, u.id)
                self.stat(cid, "banned")
            except Exception:  # noqa: BLE001
                self.log(cid, "⚠️ немає права виганяти")
            self.log(cid, f"⛔ {u.full_name}: {RULE_T[rule]} — вигнано")
            await self.notify(f"⛔ <b>{esc(c['title'])}</b>: вигнано {esc(u.full_name)} ({esc(RULE_T[rule])}).", [[("↩️ Повернути", f"cm:ub:{cid}:{u.id}")]])
        else:
            self.log(cid, f"🗑 {u.full_name}: {RULE_T[rule]}")

    async def restrict(self, chat_id, uid, minutes, allow=False):
        from telegram import ChatPermissions
        perms = ChatPermissions.all_permissions() if allow else ChatPermissions.no_permissions()
        try:
            await self.bot.restrict_chat_member(chat_id, uid, perms, until_date=None if allow else int(time.time() + max(1, minutes) * 60))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("restrict: %s", e)
            return False

    async def say_temp(self, chat_id, text, ttl, html_mode=False, rows=None):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d[:64]) for t, d in r] for r in rows]) if rows else None
        try:
            msg = await self.bot.send_message(chat_id, text, parse_mode="HTML" if html_mode else None, reply_markup=markup)
        except Exception:  # noqa: BLE001
            return

        async def later():
            await asyncio.sleep(ttl)
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass

        asyncio.ensure_future(later())

    async def tg_command(self, update, c, text):
        """Команди в групі. Для адмінів: /warn /mute [хв] /unmute /ban /allow [правило] — у відповідь на повідомлення. Для всіх: /rules, /request."""
        m, u, chat_id, cid = update.effective_message, update.effective_user, update.effective_chat.id, c["id"]
        cmd = text.split()[0].split("@")[0].lower()
        args = text.split()[1:]
        if cmd in ("/rules", "/правила"):
            on = [RULE_T[k] for k, _, _ in RULES if c["cfg"]["rules"].get(k)]
            await self.say_temp(chat_id, "📜 Правила чату:\n" + ("\n".join("• заборонено: " + t.split(" ", 1)[1].lower() for t in on) if on else "особливих заборон немає")
                                + ("\n\nПотрібен виняток? /request" if c["cfg"]["requests"] else ""), 120)
            return True
        if cmd in ("/request", "/дозвіл"):
            if not c["cfg"]["requests"]:
                return True
            rows = [[(RULE_T[k], f"cm:rq:{cid}:{k}:{u.id}")] for k, _, _ in RULES if c["cfg"]["rules"].get(k)] or [[("🔈 Зняти обмеження", f"cm:rq:{cid}:unmute:{u.id}")]]
            await self.say_temp(chat_id, f"🙋 {u.mention_html()}, на що попросити дозвіл?", 90, html_mode=True, rows=rows)
            return True
        CMDS = ("/warn", "/mute", "/unmute", "/ban", "/unban", "/allow", "/group", "/ungroup")
        if cmd not in CMDS:
            return False
        if not await self.is_chat_admin(cid, chat_id, u.id) or not m.reply_to_message or not m.reply_to_message.from_user:
            return True
        t = m.reply_to_message.from_user
        try:
            await m.delete()
        except Exception:  # noqa: BLE001
            pass
        if cmd == "/warn":
            n = self.add_warn(cid, t.id)
            await self.say_temp(chat_id, f"⚠️ {t.mention_html()}, попередження від адміністратора ({n}/{c['cfg']['warn_limit']}).", 60, html_mode=True)
        elif cmd == "/mute":
            mins = int(args[0]) if args and args[0].isdigit() else c["cfg"]["mute_min"]
            await self.restrict(chat_id, t.id, mins)
            await self.say_temp(chat_id, f"🔇 {t.mention_html()} — тиша на {mins_t(mins)}.", 60, html_mode=True)
        elif cmd == "/unmute":
            await self.restrict(chat_id, t.id, 0, allow=True)
            await self.say_temp(chat_id, f"🔈 {t.mention_html()} знову може писати.", 60, html_mode=True)
        elif cmd == "/ban":
            await self.bot.ban_chat_member(chat_id, t.id)
            self.stat(cid, "banned")
        elif cmd == "/unban":
            await self.bot.unban_chat_member(chat_id, t.id, only_if_banned=True)
        elif cmd in ("/group", "/ungroup"):
            name = " ".join(args).strip() or "Довірені"
            gid = self.group_id(name, create=cmd == "/group")
            if gid and cmd == "/group":
                self.x("INSERT OR REPLACE INTO cm_gmembers(gid, user_id, name) VALUES (?,?,?)", (gid, str(t.id), t.full_name))
                await self.say_temp(chat_id, f"👥 {t.mention_html()} тепер у групі «{esc(name)}».", 60, html_mode=True)
            elif gid:
                self.x("DELETE FROM cm_gmembers WHERE gid=? AND user_id=?", (gid, str(t.id)))
                await self.say_temp(chat_id, f"👥 {t.mention_html()} більше не в групі «{esc(name)}».", 60, html_mode=True)
        elif cmd == "/allow":
            rule = args[0] if args and (args[0] in RULE_T) else "*"
            self.x("INSERT OR REPLACE INTO cm_permits(cid, user_id, rule, until, name) VALUES (?,?,?,?,?)", (cid, str(t.id), rule, 0, t.full_name))
            await self.say_temp(chat_id, f"✅ {t.mention_html()}: дозволено " + (RULE_ASK[rule] if rule != "*" else "все, що заборонено правилами") + ".", 60, html_mode=True)
        self.log(cid, f"адмін {u.full_name}: {cmd} → {t.full_name}")
        return True

    async def tg_callback(self, update, ctx):
        q = update.callback_query
        p = q.data.split(":")
        a = p[1]
        if a == "rq":  # учасник просить дозвіл
            cid, rule, uid = f"{p[2]}:{p[3]}", p[4], p[5]
            if str(q.from_user.id) != uid:
                return await q.answer("Цю кнопку може натиснути лише той, кому вона адресована.", show_alert=True)
            c = self.chat(cid)
            if not c:
                return await q.answer()
            if self.q("SELECT id FROM cm_reqs WHERE cid=? AND user_id=? AND rule=? AND status='open'", (cid, uid, rule), one=True):
                return await q.answer("Запит уже надіслано — чекайте відповіді.", show_alert=True)
            name = q.from_user.full_name + (f" @{q.from_user.username}" if q.from_user.username else "")
            rid = self.x("INSERT INTO cm_reqs(cid, user_id, name, rule, created, ref) VALUES (?,?,?,?,?,?)", (cid, uid, name, rule, time.time(), str(c["chat_id"])))
            await q.answer("✅ Запит надіслано власнику. Відповідь з'явиться в чаті.", show_alert=True)
            await self.notify_request(rid)
            return
        if not str(q.from_user.id) in {str(o) for o in self.owners()}:
            return await q.answer("Лише для власника бота", show_alert=True)
        if a in ("pa", "pd"):  # рішення власника щодо дозволу
            await q.answer()
            txt = await self.decide(int(p[2]), p[3] if a == "pa" else None)
            try:
                await q.edit_message_text(q.message.text_html + f"\n\n<b>{esc(txt)}</b>", parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
        elif a in ("ja", "jd"):
            await q.answer()
            txt = await self.decide_join(int(p[2]), a == "ja")
            try:
                await q.edit_message_text(q.message.text_html + f"\n\n<b>{esc(txt)}</b>", parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
        elif a == "um":
            c = self.chat(f"{p[2]}:{p[3]}")
            await self.restrict(int(c["chat_id"]), int(p[4]), 0, allow=True)
            await q.answer("Обмеження знято")
        elif a == "ub":
            c = self.chat(f"{p[2]}:{p[3]}")
            await self.bot.unban_chat_member(int(c["chat_id"]), int(p[4]), only_if_banned=True)
            await q.answer("Можна повернутися в чат")

    async def notify_request(self, rid):
        r = self.q("SELECT * FROM cm_reqs WHERE id=?", (rid,), one=True)
        c = self.chat(r["cid"])
        rows = [[(f"✅ {lbl}", f"cm:pa:{rid}:{k}") for k, lbl, _ in PERMITS[:2]], [(f"✅ {lbl}", f"cm:pa:{rid}:{k}") for k, lbl, _ in PERMITS[2:]],
                [("❌ Відмовити", f"cm:pd:{rid}")]]
        if r["rule"] == "unmute":
            rows = [[("🔈 Зняти обмеження", f"cm:pa:{rid}:inf"), ("❌ Відмовити", f"cm:pd:{rid}")]]
        await self.notify(f"🙋 <b>Запит дозволу</b> · {KIND.get(c['kind'], '👥')} «{esc(c['title'])}»\n{esc(r['name'])} просить: <b>{esc(RULE_ASK.get(r['rule'], r['rule']))}</b>", rows)

    async def decide(self, rid, permit_key):
        r = self.q("SELECT * FROM cm_reqs WHERE id=?", (rid,), one=True)
        if not r or r["status"] != "open":
            return "Цей запит уже розглянуто"
        c = self.chat(r["cid"])
        chat_id = int(c["chat_id"]) if c["platform"] == "tg" else c["chat_id"]
        if permit_key is None:
            self.x("UPDATE cm_reqs SET status='denied' WHERE id=?", (rid,))
            self.log(c["id"], f"❌ відмовлено: {r['name']} — {RULE_ASK.get(r['rule'])}")
            if c["platform"] == "tg":
                await self.say_temp(chat_id, f"❌ {r['name']}, у дозволі ({RULE_ASK.get(r['rule'])}) відмовлено.", 120)
            return "❌ Відмовлено"
        secs = {k: s for k, _, s in PERMITS}[permit_key]
        until = time.time() + secs if secs else 0
        self.x("UPDATE cm_reqs SET status='approved' WHERE id=?", (rid,))
        if r["rule"] == "unmute":
            await self.restrict(chat_id, int(r["user_id"]), 0, allow=True)
            self.x("DELETE FROM cm_warns WHERE cid=? AND user_id=?", (c["id"], r["user_id"]))
            msg = f"🔈 {r['name']}, обмеження знято — можна писати."
        else:
            self.x("INSERT OR REPLACE INTO cm_permits(cid, user_id, rule, until, name) VALUES (?,?,?,?,?)", (c["id"], r["user_id"], r["rule"], until, r["name"]))
            msg = f"✅ {r['name']}, дозволено {RULE_ASK.get(r['rule'])} — {dur(secs)}."
        self.stat(c["id"], "permits")
        self.log(c["id"], msg)
        if c["platform"] == "tg":
            await self.say_temp(chat_id, msg, 300)
        return "✅ Дозволено · " + dur(secs) if r["rule"] != "unmute" else "🔈 Обмеження знято"

    async def decide_join(self, rid, ok):
        r = self.q("SELECT * FROM cm_reqs WHERE id=?", (rid,), one=True)
        if not r or r["status"] != "open":
            return "Цю заявку вже розглянуто"
        c = self.chat(r["cid"])
        try:
            if c["platform"] == "tg":
                if ok:
                    await self.bot.approve_chat_join_request(int(c["chat_id"]), int(r["user_id"]))
                else:
                    await self.bot.decline_chat_join_request(int(c["chat_id"]), int(r["user_id"]))
            elif c["platform"] == "wa":
                await self.bridge.api("POST" if ok else "DELETE", f"{c['chat_id']}/join_requests",
                                      json={"messaging_product": "whatsapp", "join_requests": [r["ref"]]})
        except Exception as e:  # noqa: BLE001
            return f"⚠️ Не вдалося: {e}"
        self.x("UPDATE cm_reqs SET status=? WHERE id=?", ("approved" if ok else "denied", rid))
        self.stat(c["id"], "approved" if ok else "declined")
        self.log(c["id"], f"{'✅ прийнято' if ok else '❌ відхилено'} заявку: {r['name']}")
        return "✅ Прийнято" if ok else "❌ Відхилено"

    @staticmethod
    def rights_text(kind, r):
        if not r.get("admin") and kind == "channel":
            return "⚠️ Бот — не адміністратор каналу. Щоб публікувати, зробіть його адміністратором з правом «Публікувати повідомлення»."
        if not r.get("admin"):
            return ("⚠️ Бот — звичайний учасник. Щоб він бачив усі повідомлення, видаляв порушення й приймав заявки, "
                    "зробіть його адміністратором чату (права: видаляти повідомлення, блокувати користувачів, запрошувати).")
        if kind == "channel":
            return "✅ Адміністратор каналу" + ("" if r.get("post") else " · ⚠️ немає права публікувати")
        miss = [t for k, t in (("delete", "видаляти повідомлення"), ("restrict", "блокувати користувачів"), ("invite", "запрошувати (заявки)")) if not r.get(k)]
        return "✅ Адміністратор" + (f" · ⚠️ бракує прав: {', '.join(miss)}" if miss else " з усіма потрібними правами")

    # ------------------------------------------------------------ WhatsApp / Instagram (через міст)
    async def wa_refresh(self):
        b = self.bridge
        data = await b.api("GET", f"{b.phone_id}/groups")
        found = 0
        for g in data.get("data", data.get("groups", [])):
            gid = g.get("id") or g.get("group_id")
            if gid:
                self.register("wa", gid, g.get("subject") or g.get("name") or gid, "wa_group")
                found += 1
        return found

    async def wa_send(self, gid, text):
        b = self.bridge
        await b.api("POST", f"{b.phone_id}/messages", json={"messaging_product": "whatsapp", "recipient_type": "group", "to": gid,
                                                             "type": "text", "text": {"body": text[:4096]}})
        if hasattr(b, "count_sent"):
            b.count_sent()

    async def wa_group_message(self, gid, uid, name, text):
        """Повідомлення в групі WhatsApp (якщо Meta надсилає їх на вебхук). Чужі повідомлення видаляти не можна:
        бот попереджає, а після ліміту попереджень — видаляє учасника з групи."""
        cid = f"wa:{gid}"
        c = self.chat(cid) or self.chat(self.register("wa", gid, gid, "wa_group"))
        self.stat(cid, "seen")
        hit = self.watch_hit(c, text)
        if hit:
            await self.notify(f"👁 <b>{esc(c['title'])}</b> · {esc(name or uid)}\n{esc(short(text, 700))}")
        if not c["cfg"]["enabled"] or str(uid) in {str(o) for o in self.owners()}:
            return
        self.seen_member(cid, uid, name, None)
        rule = self.violation(c, uid, text, set())
        if rule not in TEXT_RULES:
            return
        n = self.add_warn(cid, uid)
        self.stat(cid, "warned")
        if c["cfg"]["action"] in ("ban", "mute") or n >= c["cfg"]["warn_limit"]:
            try:
                await self.bridge.api("DELETE", f"{gid}/participants", json={"messaging_product": "whatsapp", "participants": [{"user": str(uid)}]})
                self.stat(cid, "banned")
                self.log(cid, f"⛔ {name or uid}: {RULE_T[rule]} — видалено з групи")
                await self.notify(f"⛔ <b>{esc(c['title'])}</b>: видалено {esc(name or uid)} з групи ({esc(RULE_T[rule])}). Повернутися можна за посиланням-запрошенням.")
            except Exception as e:  # noqa: BLE001
                self.log(cid, f"⚠️ не вдалося видалити учасника: {e}")
        else:
            await self.wa_send(gid, f"⚠️ {name or 'Учаснику'}, тут не можна: {RULE_T[rule].split(' ', 1)[1].lower()}. Попередження {n}/{c['cfg']['warn_limit']}.")
            self.log(cid, f"⚠️ {name or uid}: {RULE_T[rule]} (попередження {n})")

    async def wa_poll_joins(self):
        for c in self.chats():
            if c["platform"] != "wa" or c["cfg"]["joins"] == "off":
                continue
            try:
                data = await self.bridge.api("GET", f"{c['chat_id']}/join_requests")
            except Exception:  # noqa: BLE001
                continue
            for r in data.get("data", []):
                jr = r.get("join_request_id") or r.get("id")
                uid = r.get("wa_id") or r.get("user") or r.get("whatsapp_user_id") or "?"
                if not jr or self.q("SELECT id FROM cm_reqs WHERE cid=? AND ref=? AND kind='join'", (c["id"], jr), one=True):
                    continue
                rid = self.x("INSERT INTO cm_reqs(cid, user_id, name, rule, created, kind, ref) VALUES (?,?,?,?,?, 'join', ?)",
                             (c["id"], str(uid), str(uid), "join", time.time(), jr))
                if c["cfg"]["joins"] == "auto":
                    await self.decide_join(rid, True)
                else:
                    await self.notify(f"🚪 <b>Заявка на вступ</b> у групу WhatsApp «{esc(c['title'])}»: +{esc(uid)}",
                                      [[("✅ Прийняти", f"cm:ja:{rid}"), ("❌ Відхилити", f"cm:jd:{rid}")]])

    async def ig_comment(self, me, v):
        """Коментар під дописом Instagram: правила (сховати/видалити), моніторинг."""
        cid = f"ig:{me}"
        c = self.chat(cid) or self.chat(self.register("ig", me, "Коментарі Instagram", "ig"))
        frm = v.get("from") or {}
        uid, uname, text, com = frm.get("id"), frm.get("username") or frm.get("id"), v.get("text", ""), v.get("id")
        if not com or str(uid) == str(me):
            return
        self.stat(cid, "seen")
        hit = self.watch_hit(c, text)
        if hit:
            await self.notify(f"👁 <b>Instagram</b> · @{esc(uname)}\n{esc(short(text, 700))}")
        if not c["cfg"]["enabled"]:
            return
        self.seen_member(cid, uid, "@" + str(uname), uname)
        rule = self.violation(c, uid, text, set(), uname)
        if rule not in TEXT_RULES:
            return
        try:
            if c["cfg"]["ig_action"] == "delete":
                await self.bridge.api("DELETE", com)
                self.stat(cid, "deleted")
            else:
                await self.bridge.api("POST", com, params={"hide": "true"})
                self.stat(cid, "hidden")
            self.log(cid, f"{'🗑' if c['cfg']['ig_action'] == 'delete' else '🙈'} @{uname}: {RULE_T[rule]} — «{short(text, 60)}»")
        except Exception as e:  # noqa: BLE001
            self.log(cid, f"⚠️ не вдалося змінити коментар: {e}")

    async def ig_publish(self, image, caption):
        b = self.bridge
        cont = await b.api("POST", "me/media", data={"image_url": image, "caption": caption[:2200]})
        await asyncio.sleep(3)
        await b.api("POST", "me/media_publish", data={"creation_id": cont["id"]})

    # ------------------------------------------------------------ публікації за розкладом
    def sched_text(self, s):
        k = s.get("k")
        if k == "now":
            return "одразу"
        if k == "once":
            return "один раз " + datetime.fromtimestamp(s["ts"], self.tz).strftime("%d.%m %H:%M")
        if k == "daily":
            return f"щодня о {s['t']}"
        if k == "weekly":
            return f"щотижня ({', '.join(WEEK[d] for d in s.get('d', []))}) о {s['t']}"
        return "вручну"

    def due(self, p):
        s = json.loads(p["sched"] or "{}")
        now = self.now()
        if s.get("k") == "once":
            return time.time() >= s["ts"] and not p["last_run"]
        if s.get("k") in ("daily", "weekly"):
            if s.get("k") == "weekly" and now.weekday() not in s.get("d", []):
                return False
            hh, mm = map(int, s["t"].split(":"))
            slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            return now >= slot and p["last_run"] < slot.timestamp()
        return False

    async def publish(self, pid):
        p = self.q("SELECT * FROM cm_posts WHERE id=?", (pid,), one=True)
        c = self.chat(p["cid"])
        text = p["text"]
        if p["kind"] == "ai":
            if not self.ai:
                raise RuntimeError("дописи від ШІ доступні лише в ШІ-боті")
            text = await self.ai("Напиши готовий до публікації допис (без пояснень, лише сам текст) для "
                                 f"{KIND_T.get(c['kind'], 'спільноти')} «{c['title']}». Завдання: {p['text']}")
        if c["platform"] == "tg":
            await self.bot.send_message(int(c["chat_id"]), text[:4096])
        elif c["platform"] == "wa":
            await self.wa_send(c["chat_id"], text)
        elif c["platform"] == "ig":
            if not p["image"]:
                raise RuntimeError("для Instagram потрібне посилання на картинку")
            await self.ig_publish(p["image"], text)
        s = json.loads(p["sched"] or "{}")
        self.x("UPDATE cm_posts SET last_run=?, status=? WHERE id=?", (time.time(), "done" if s.get("k") in ("now", "once") else p["status"], pid))
        self.stat(c["id"], "posted")
        self.log(c["id"], f"📣 опубліковано допис #{pid}: «{short(text, 50)}»")
        return text

    async def tick(self, bot=None):
        """Раз на хвилину: дописи за розкладом, прострочені дозволи, заявки WhatsApp."""
        if bot:
            self.bot = bot
        for p in sorted(self.q("SELECT * FROM cm_posts WHERE status='active'"), key=lambda p: -self.ranks.score("c", p["cid"])):
            if self.due(p):
                try:
                    await self.publish(p["id"])
                except Exception as e:  # noqa: BLE001
                    self.x("UPDATE cm_posts SET last_run=? WHERE id=?", (time.time(), p["id"]))
                    await self.notify(f"⚠️ Допис #{p['id']} не опубліковано: {esc(e)}", [[("📣 Публікації", f"m:cm:p:{p['cid']}")]])
        self.x("DELETE FROM cm_permits WHERE until>0 AND until<?", (time.time(),))
        for c in self.chats():  # раз на добу оновлюємо посилання й кількість учасників
            if time.time() - (c.get("checked") or 0) > 86400:
                await self.refresh_info(c)
                break
        if self.bridge and getattr(self.bridge, "platform", "") == "wa" and time.time() - self.last_wa_poll > 300:
            self.last_wa_poll = time.time()
            await self.wa_poll_joins()

    # ------------------------------------------------------------ 👤 хто що може: усі / групи / окремі учасники
    # Ціль: all — усі учасники; new — новачки; group:<id> — група учасників; user:<id> або uname:<@ім'я> — окрема людина.
    # Область: «*» — у всіх спільнотах, або id конкретної спільноти. Значення: allow — дозволено, deny — заборонено.
    # Що важливіше: людина > група > новачки > усі; у межах рівня налаштування спільноти важливіші за «у всіх»;
    # якщо людина в кількох групах і одна дозволяє, а інша забороняє, — забороняє.
    def seen_member(self, cid, uid, name, username):
        if uid is None:
            return
        self.x("INSERT INTO cm_members(cid, user_id, joined, name, username, last) VALUES (?,?,0,?,?,?) "
               "ON CONFLICT(cid, user_id) DO UPDATE SET name=excluded.name, username=COALESCE(excluded.username, cm_members.username), last=excluded.last",
               (cid, str(uid), name, (username or "").lstrip("@").lower() or None, time.time()))

    def group_id(self, name, create=False):
        r = self.q("SELECT id FROM cm_groups WHERE lower(name)=lower(?)", (name.strip(),), one=True)
        if r:
            return r["id"]
        return self.x("INSERT INTO cm_groups(name, created) VALUES (?,?)", (name.strip()[:40], time.time())) if create else None

    def user_groups(self, uid, uname=None):
        keys = [str(uid)] + ([f"@{uname.lstrip('@').lower()}"] if uname else [])
        return [r["gid"] for r in self.q(f"SELECT DISTINCT gid FROM cm_gmembers WHERE user_id IN ({','.join('?' * len(keys))})", keys)]

    def policy(self, scope, target):
        return {r["rule"]: r["value"] for r in self.q("SELECT rule, value FROM cm_policy WHERE scope=? AND target=?", (scope, target))}

    def forbidden(self, c, uid, uname, k):
        base = bool(c["cfg"]["rules"].get(k))
        cid = c["id"]
        is_new = False
        if uid is not None:
            m = self.q("SELECT joined FROM cm_members WHERE cid=? AND user_id=?", (cid, str(uid)), one=True)
            is_new = bool(m and m["joined"] and time.time() - m["joined"] < c["cfg"]["newbie_min"] * 60)
        levels = [["all"], ["new"] if is_new else [], [f"group:{g}" for g in self.user_groups(uid, uname)] if uid is not None else [],
                  ([f"user:{uid}"] if uid is not None else []) + ([f"uname:{uname.lstrip('@').lower()}"] if uname else [])]
        val = None
        for targets in levels:
            if not targets:
                continue
            ph = ",".join("?" * len(targets))
            rows = self.q(f"SELECT scope, value FROM cm_policy WHERE rule=? AND target IN ({ph}) AND scope IN ('*', ?)", (k, *targets, cid))
            local = [r["value"] for r in rows if r["scope"] == cid]
            vals = local or [r["value"] for r in rows]
            if vals:
                val = "deny" if "deny" in vals else "allow"
        return base if val is None else val == "deny"

    def target_title(self, t):
        if t == "all":
            return "👥 Усі учасники"
        if t == "new":
            return "🐣 Новачки"
        kind, _, ref = t.partition(":")
        if kind == "group":
            g = self.q("SELECT name FROM cm_groups WHERE id=?", (int(ref),), one=True)
            return f"🏷 {g['name'] if g else 'група'}"
        if kind == "uname":
            return f"👤 @{ref}"
        m = self.q("SELECT name, username FROM cm_members WHERE user_id=? ORDER BY last DESC LIMIT 1", (ref,), one=True)
        return f"👤 {m['name'] or ref}" if m else f"👤 {ref}"

    def scope_title(self, scope):
        if scope == "*":
            return "у всіх спільнотах"
        c = self.chat(scope)
        return f"у «{c['title']}»" if c else scope

    def scr_policies(self, chat, scope):
        """Список налаштувань «хто що може» для спільноти (і загальних, що на неї діють)."""
        scopes = ["*"] if scope == "*" else [scope, "*"]
        lines = [f"👤 <b>Хто що може · {esc(self.scope_title(scope))}</b>",
                 "Окремі дозволи й заборони для всіх, для груп або для конкретних людей — понад загальні 🛡 Правила.",
                 "Пріоритет: людина › група › новачки › усі. Налаштування спільноти важливіші за «у всіх спільнотах»."]
        rows = []
        for sc in scopes:
            ts = [r["target"] for r in self.q("SELECT DISTINCT target FROM cm_policy WHERE scope=? ORDER BY target", (sc,))]
            if ts:
                lines.append(f"\n<b>{'🌐 У всіх спільнотах' if sc == '*' else '📍 Лише тут'}:</b>")
            for t in ts:
                pol = self.policy(sc, t)
                desc = ", ".join(("✅ " if v == "allow" else "⛔ ") + RULE_T.get(k, k).split(" ", 1)[1].lower() for k, v in pol.items())
                lines.append(f"• {esc(self.target_title(t))}: {esc(desc)}")
                rows.append([(f"✏️ {short(self.target_title(t), 22)} · {'🌐' if sc == '*' else '📍'}", self.wiz_btn(chat, sc, t))])
        if len(lines) == 3:
            lines.append("\nПоки немає жодного окремого налаштування.")
        rows.append([("➕ Усім", self.wiz_btn(chat, scope, "all")), ("➕ Новачкам", self.wiz_btn(chat, scope, "new"))])
        rows.append([("➕ Групі…", f"m:cm:ptg"), ("➕ Учаснику…", "m:cm:ptu:0")])
        rows.append([("🏷 Групи учасників", "m:cm:grs")])
        rows.append([("« Назад", f"m:cm:c:{scope}" if scope != "*" else "m:cm")])
        self.wiz[chat] = {"scope": scope, **self.wiz.get(chat, {}), "home": scope}
        return "\n".join(lines), rows

    def wiz_btn(self, chat, scope, target):
        """Кнопка відкриття матриці: id запам'ятовуємо в майстрі, щоб дані кнопки були короткими."""
        w = self.wiz.setdefault(chat, {})
        keys = w.setdefault("keys", [])
        if (scope, target) not in keys:
            keys.append((scope, target))
        return f"m:cm:pm:{keys.index((scope, target))}"

    def scr_matrix(self, chat):
        w = self.wiz[chat]
        scope, target = w["scope"], w["target"]
        pol = self.policy(scope, target)
        names = {"allow": "✅ дозволено", "deny": "⛔ заборонено"}
        lines = [f"🎚 <b>{esc(self.target_title(target))}</b> · {esc(self.scope_title(scope))}",
                 "Натискайте правило, щоб змінити: ▫️ як у загальних правилах → ✅ дозволено → ⛔ заборонено."]
        rows = []
        for k, t, _ in RULES:
            v = pol.get(k)
            rows.append([((("✅ " if v == "allow" else "⛔ ") if v else "▫️ ") + t + (f" — {names[v]}" if v else ""), f"m:cm:pv:{k}")])
        rows.append([("✅ Дозволити все", "m:cm:pva:allow"), ("⛔ Заборонити все", "m:cm:pva:deny"), ("▫️ Скинути", "m:cm:pva:none")])
        other = "*" if scope != "*" else None
        sc_row = []
        if other:
            sc_row.append(("🌐 Те саме у всіх спільнотах", "m:cm:psa"))
        sc_row.append(("📍 Скопіювати в інші…", "m:cm:pcp"))
        rows.append(sc_row)
        rows.append([("« Назад", "m:cm:pl:" + w.get("home", scope))])
        return "\n".join(lines), rows

    def scr_pick_user(self, chat, page):
        w = self.wiz.get(chat, {})
        scope = w.get("home", "*")
        where, args = ("WHERE cid=?", (scope,)) if scope != "*" else ("", ())
        ms = self.q(f"SELECT user_id, MAX(name) name, MAX(username) username, MAX(last) last FROM cm_members {where} GROUP BY user_id ORDER BY last DESC LIMIT 9 OFFSET ?", (*args, page * 8))
        rows = [[(f"👤 {short(m['name'] or m['user_id'], 22)}" + (f" @{m['username']}" if m["username"] else ""), f"m:cm:ptp:{m['user_id']}")] for m in ms[:8]]
        nav = ([("⬅️", f"m:cm:ptu:{page - 1}")] if page else []) + ([("➡️", f"m:cm:ptu:{page + 1}")] if len(ms) > 8 else [])
        if nav:
            rows.append(nav)
        rows.append([("✍️ Ввести @ім'я або ID", "m:cm:pti")])
        rows.append([("« Назад", "m:cm:pl:" + scope)])
        return ("👤 <b>Для кого?</b>\nОстанні, хто писав" + (f" у «{esc(self.chat(scope)['title'])}»" if scope != "*" else "") + ". Або введіть @ім'я чи ID.", rows)

    def scr_pick_group(self, chat):
        gs = self.q("SELECT g.id, g.name, COUNT(m.user_id) n FROM cm_groups g LEFT JOIN cm_gmembers m ON m.gid=g.id GROUP BY g.id ORDER BY g.name")
        rows = [[(f"🏷 {g['name']} · {g['n']}", f"m:cm:ptgp:{g['id']}")] for g in gs]
        rows.append([("➕ Нова група", "m:cm:grn")])
        rows.append([("« Назад", "m:cm:pl:" + self.wiz.get(chat, {}).get("home", "*"))])
        return "🏷 <b>Для якої групи?</b>" + ("" if gs else "\nГруп ще немає — створіть першу."), rows

    def scr_groups(self):
        gs = self.q("SELECT g.id, g.name, COUNT(m.user_id) n FROM cm_groups g LEFT JOIN cm_gmembers m ON m.gid=g.id GROUP BY g.id ORDER BY g.name")
        lines = ["🏷 <b>Групи учасників</b>", "Набори людей (з будь-яких спільнот), яким можна разом дозволяти чи забороняти: «Довірені», «VIP», «Модератори», «Під наглядом»…",
                 "Додати людину: тут зі списку, за @ім'ям/ID, або в чаті у відповідь на її повідомлення: <code>/group VIP</code> (прибрати — <code>/ungroup VIP</code>)."]
        rows = [[(f"🏷 {g['name']} · {g['n']}", f"m:cm:gr:{g['id']}")] for g in gs]
        rows.append([("➕ Нова група", "m:cm:grn")])
        rows.append([("« Спільноти", "m:cm")])
        return "\n".join(lines), rows

    def scr_group(self, gid, page=0):
        g = self.q("SELECT * FROM cm_groups WHERE id=?", (gid,), one=True)
        if not g:
            return self.scr_groups()
        ms = self.q("SELECT * FROM cm_gmembers WHERE gid=? ORDER BY name LIMIT 40", (gid,))
        used = self.q("SELECT DISTINCT scope FROM cm_policy WHERE target=?", (f"group:{gid}",))
        lines = [f"🏷 <b>{esc(g['name'])}</b> · {len(ms)} учасн.",
                 ("Діє: " + ", ".join(self.scope_title(u["scope"]) for u in used)) if used else "Ще не має окремих дозволів — «🎚 Що можна» нижче."]
        lines += [f"• {esc(m['name'] or m['user_id'])}" for m in ms]
        rows = [[(f"✖️ {short(m['name'] or m['user_id'], 26)}", f"m:cm:gmx:{gid}:{m['user_id']}")] for m in ms[:12]]
        rows.append([("➕ Зі списку", f"m:cm:gpk:{gid}:0"), ("✍️ За @ім'ям/ID", f"m:cm:gad:{gid}")])
        rows.append([("🎚 Що можна цій групі", f"m:cm:ptgp:{gid}")])
        rows.append([("✏️ Перейменувати", f"m:cm:grr:{gid}"), ("🗑 Видалити групу", f"m:cm:grd:{gid}")])
        rows.append([("« Групи", "m:cm:grs")])
        return "\n".join(lines), rows

    def scr_group_pick(self, gid, page):
        inside = {m["user_id"] for m in self.q("SELECT user_id FROM cm_gmembers WHERE gid=?", (gid,))}
        ms = self.q("SELECT user_id, MAX(name) name, MAX(username) username, MAX(last) last FROM cm_members GROUP BY user_id ORDER BY last DESC LIMIT 9 OFFSET ?", (page * 8,))
        rows = [[(("☑️ " if m["user_id"] in inside else "◻️ ") + short(m["name"] or m["user_id"], 24) + (f" @{m['username']}" if m["username"] else ""),
                  f"m:cm:gtg:{gid}:{m['user_id']}:{page}")] for m in ms[:8]]
        nav = ([("⬅️", f"m:cm:gpk:{gid}:{page - 1}")] if page else []) + ([("➡️", f"m:cm:gpk:{gid}:{page + 1}")] if len(ms) > 8 else [])
        if nav:
            rows.append(nav)
        rows.append([("✅ Готово", f"m:cm:gr:{gid}")])
        return "➕ <b>Оберіть учасників</b> (☑️ — уже в групі). Це люди, яких бот бачив у ваших спільнотах.", rows

    def scr_copy(self, chat):
        w = self.wiz[chat]
        sel = w.setdefault("copy", [])
        rows = []
        for i, c in enumerate(self.chats()):
            if c["id"] == w["scope"]:
                continue
            rows.append([(("☑️ " if c["id"] in sel else "◻️ ") + f"{KIND.get(c['kind'], '👥')} {short(c['title'], 26)}", f"m:cm:pck:{i}")])
        rows.append([("✅ Застосувати", "m:cm:pcgo"), ("« Назад", "m:cm:pmb")])
        return f"📍 <b>Куди скопіювати</b> налаштування «{esc(self.target_title(w['target']))}»?\nОберіть одну, кілька або всі спільноти.", rows

    async def route_policy(self, chat, P, a):
        """Маршрути «👤 Хто що може» й «🏷 Групи». Повертає (toast, screen) або None, якщо дія не звідси."""
        w = self.wiz.setdefault(chat, {})
        if a == "pl":
            scope = "*" if P[3] == "*" else f"{P[3]}:{P[4]}"
            w.update(home=scope, scope=scope)
            return None, self.scr_policies(chat, scope)
        if a == "pm":
            keys = w.get("keys", [])
            i = int(P[3])
            if i >= len(keys):
                return None, self.scr_policies(chat, w.get("home", "*"))
            w["scope"], w["target"] = keys[i]
            return None, self.scr_matrix(chat)
        if a == "pmb":
            return None, self.scr_matrix(chat)
        if a in ("pv", "pva") and "target" in w:
            sc, t = w["scope"], w["target"]
            if a == "pv":
                cur = self.policy(sc, t).get(P[3])
                nxt = {None: "allow", "allow": "deny", "deny": None}[cur]
                ks = [P[3]]
            else:
                nxt = None if P[3] == "none" else P[3]
                ks = [k for k, _, _ in RULES]
            for k in ks:
                if nxt:
                    self.x("INSERT OR REPLACE INTO cm_policy(scope, target, rule, value) VALUES (?,?,?,?)", (sc, t, k, nxt))
                else:
                    self.x("DELETE FROM cm_policy WHERE scope=? AND target=? AND rule=?", (sc, t, k))
            return None, self.scr_matrix(chat)
        if a == "psa" and "target" in w:
            for k, v in self.policy(w["scope"], w["target"]).items():
                self.x("INSERT OR REPLACE INTO cm_policy(scope, target, rule, value) VALUES ('*',?,?,?)", (w["target"], k, v))
            w["scope"] = "*"
            return "Тепер діє у всіх спільнотах", self.scr_matrix(chat)
        if a == "pcp":
            w["copy"] = []
            return None, self.scr_copy(chat)
        if a == "pck":
            cs = self.chats()
            cid = cs[int(P[3])]["id"] if int(P[3]) < len(cs) else None
            if cid:
                sel = w.setdefault("copy", [])
                sel.remove(cid) if cid in sel else sel.append(cid)
            return None, self.scr_copy(chat)
        if a == "pcgo":
            pol = self.policy(w["scope"], w["target"])
            for cid in w.get("copy", []):
                self.x("DELETE FROM cm_policy WHERE scope=? AND target=?", (cid, w["target"]))
                for k, v in pol.items():
                    self.x("INSERT OR REPLACE INTO cm_policy(scope, target, rule, value) VALUES (?,?,?,?)", (cid, w["target"], k, v))
            n = len(w.get("copy", []))
            return f"Скопійовано в {n} спільнот(и)", self.scr_matrix(chat)
        if a == "ptg":
            return None, self.scr_pick_group(chat)
        if a == "ptgp":
            w["target"] = f"group:{int(P[3])}"
            w["scope"] = w.get("home", "*")
            self.wiz_btn(chat, w["scope"], w["target"])
            return None, self.scr_matrix(chat)
        if a == "ptu":
            return None, self.scr_pick_user(chat, int(P[3]))
        if a == "ptp":
            w["target"], w["scope"] = f"user:{P[3]}", w.get("home", "*")
            self.wiz_btn(chat, w["scope"], w["target"])
            return None, self.scr_matrix(chat)
        if a == "pti":
            self.awaiting[chat] = ("ptarget", None)
            return None, ("✍️ Напишіть @ім'я або числовий ID учасника (можна кілька через кому — тоді бот створить для них групу).",
                          [[("✖️ Скасувати", "m:cm:pl:" + w.get("home", "*"))]])
        if a == "grs":
            return None, self.scr_groups()
        if a == "grn":
            self.awaiting[chat] = ("gname", None)
            return None, ("🏷 Як назвати групу? Наприклад: Довірені, VIP, Модератори, Під наглядом.", [[("✖️ Скасувати", "m:cm:grs")]])
        if a == "gr":
            return None, self.scr_group(int(P[3]))
        if a == "gpk":
            return None, self.scr_group_pick(int(P[3]), int(P[4]))
        if a == "gtg":
            gid, uid, page = int(P[3]), P[4], int(P[5])
            if self.q("SELECT 1 FROM cm_gmembers WHERE gid=? AND user_id=?", (gid, uid), one=True):
                self.x("DELETE FROM cm_gmembers WHERE gid=? AND user_id=?", (gid, uid))
            else:
                m = self.q("SELECT name FROM cm_members WHERE user_id=? ORDER BY last DESC LIMIT 1", (uid,), one=True)
                self.x("INSERT INTO cm_gmembers(gid, user_id, name) VALUES (?,?,?)", (gid, uid, m["name"] if m else uid))
            return None, self.scr_group_pick(gid, page)
        if a == "gmx":
            self.x("DELETE FROM cm_gmembers WHERE gid=? AND user_id=?", (int(P[3]), P[4]))
            return "Прибрано з групи", self.scr_group(int(P[3]))
        if a == "gad":
            self.awaiting[chat] = ("gadd", int(P[3]))
            return None, ("✍️ Напишіть @імена або ID через кому чи з нового рядка.", [[("✖️ Скасувати", f"m:cm:gr:{P[3]}")]])
        if a == "grr":
            self.awaiting[chat] = ("gren", int(P[3]))
            return None, ("✏️ Нова назва групи:", [[("✖️ Скасувати", f"m:cm:gr:{P[3]}")]])
        if a == "grd":
            gid = int(P[3])
            self.x("DELETE FROM cm_gmembers WHERE gid=?", (gid,))
            self.x("DELETE FROM cm_policy WHERE target=?", (f"group:{gid}",))
            self.x("DELETE FROM cm_groups WHERE id=?", (gid,))
            return "Групу видалено", self.scr_groups()
        return None

    def parse_people(self, t):
        """'@olya, 12345, @petro' -> [(user_id або '@ім'я', підпис)]. @ім'я, яке бот уже бачив, замінюється на ID."""
        out = []
        for p in re.split(r"[,\n;\s]+", t.strip()):
            p = p.strip()
            if not p:
                continue
            if p.lstrip("-").isdigit():
                m = self.q("SELECT name FROM cm_members WHERE user_id=? LIMIT 1", (p,), one=True)
                out.append((p, m["name"] if m else p))
            else:
                u = p.lstrip("@").lower()
                m = self.q("SELECT user_id, name FROM cm_members WHERE username=? ORDER BY last DESC LIMIT 1", (u,), one=True)
                out.append((m["user_id"], m["name"]) if m else (f"@{u}", f"@{u}"))
        return out

    async def on_text_policy(self, chat, what, ref, t):
        w = self.wiz.setdefault(chat, {})
        if what == "gname":
            gid = self.group_id(t, create=True)
            return "Групу створено", self.scr_group(gid)
        if what == "gren":
            self.x("UPDATE cm_groups SET name=? WHERE id=?", (t.strip()[:40], ref))
            return "Перейменовано", self.scr_group(ref)
        if what == "gadd":
            ppl = self.parse_people(t)
            for uid, name in ppl:
                self.x("INSERT OR REPLACE INTO cm_gmembers(gid, user_id, name) VALUES (?,?,?)", (ref, uid, name))
            return f"Додано: {len(ppl)}", self.scr_group(ref)
        if what == "ptarget":
            ppl = self.parse_people(t)
            if not ppl:
                return None, self.scr_pick_user(chat, 0)
            if len(ppl) == 1:
                uid = ppl[0][0]
                w["target"] = f"uname:{uid[1:]}" if uid.startswith("@") else f"user:{uid}"
            else:
                gid = self.group_id("Вибірка " + self.now().strftime("%d.%m %H:%M"), create=True)
                for uid, name in ppl:
                    self.x("INSERT OR REPLACE INTO cm_gmembers(gid, user_id, name) VALUES (?,?,?)", (gid, uid, name))
                w["target"] = f"group:{gid}"
            w["scope"] = w.get("home", "*")
            self.wiz_btn(chat, w["scope"], w["target"])
            return None, self.scr_matrix(chat)
        return None

    # ------------------------------------------------------------ 📋 таблиця: де бот, з посиланнями
    async def refresh_info(self, c):
        """Оновлює посилання, @ім'я й кількість учасників спільноти."""
        username, link, members = c.get("username"), c.get("link"), c.get("members")
        try:
            if c["platform"] == "tg" and self.bot:
                try:
                    ch = await self.bot.get_chat(int(c["chat_id"]))
                except Exception as e:  # noqa: BLE001
                    log.info("getChat %s: %s", c["id"], e)
                    ch = None
                username = getattr(ch, "username", None) or username
                if username:
                    link = f"https://t.me/{username}"
                elif getattr(ch, "invite_link", None):
                    link = ch.invite_link
                elif not link and (c["rights"].get("invite") or c["rights"].get("admin")):
                    try:  # окреме посилання для бота: основне посилання чату не змінюється й не відкликається
                        link = (await self.bot.create_chat_invite_link(int(c["chat_id"]), name="🏘 посилання бота")).invite_link
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    members = await self.bot.get_chat_member_count(int(c["chat_id"]))
                except Exception:  # noqa: BLE001
                    pass
                if ch is not None and ch.title and ch.title != c["title"]:
                    self.x("UPDATE cm_chats SET title=? WHERE id=?", (ch.title, c["id"]))
            elif c["platform"] == "wa" and self.bridge:
                link = (await self.bridge.api("GET", f"{c['chat_id']}/invite_link")).get("invite_link") or link
            elif c["platform"] == "ig":
                m = re.search(r"@([\w.]+)", c["title"])
                if m:
                    username, link = m.group(1), f"https://instagram.com/{m.group(1)}"
        except Exception as e:  # noqa: BLE001
            log.info("не вдалося оновити дані %s: %s", c["id"], e)
        self.x("UPDATE cm_chats SET username=?, link=?, members=?, checked=? WHERE id=?", (username, link, members, time.time(), c["id"]))

    def table_rows(self, include_left=False):
        q = "SELECT * FROM cm_chats" + ("" if include_left else " WHERE state='active'") + " ORDER BY platform, kind, title"
        rows = self.q(q)
        return sorted(rows, key=lambda r: -self.ranks.score("c", r["id"]))  # 🏆 важливіші — вище

    def scr_table(self):
        rows_ = self.table_rows()
        left = self.q("SELECT COUNT(*) n FROM cm_chats WHERE state!='active'", one=True)["n"]
        by = defaultdict(int)
        for r in rows_:
            by["group" if r["kind"] in ("group", "supergroup") else r["kind"]] += 1
        total_m = sum(r.get("members") or 0 for r in rows_)
        head = (f"📋 <b>Де працює бот</b> · усього <b>{len(rows_)}</b>\n"
                f"👥 груп Telegram: {by['group']} · 📢 каналів: {by['channel']} · 🟢 груп WhatsApp: {by['wa_group']} · 📸 Instagram: {by['ig']}"
                + (f"\n👤 учасників разом: {total_m}" if total_m else "") + (f"\n🚪 вийшов або прибрали: {left}" if left else ""))
        lines = [head, ""]
        for i, r in enumerate(rows_, 1):
            rights = json.loads(r["rights"] or "{}")
            title = esc(short(r["title"], 40))
            name = f'<a href="{esc(r["link"])}">{title}</a>' if r.get("link") else title
            extra = [f"@{esc(r['username'])}" if r.get("username") else ("приватний" if r["platform"] == "tg" else "")]
            if r.get("members"):
                extra.append(f"👤 {r['members']}")
            if r["platform"] == "tg":
                extra.append("адмін ✅" if rights.get("admin") else "не адмін ⚠️")
            extra.append("з " + datetime.fromtimestamp(r["created"], self.tz).strftime("%d.%m.%y"))
            sc = self.ranks.score("c", r["id"])
            lines.append(f"{i}. {'🔺' if sc > 0 else '🔻' if sc < 0 else ''}{KIND.get(r['kind'], '👥')} {name} · " + " · ".join(e for e in extra if e))
        if not rows_:
            lines.append("Бота ще не додано в жодну групу чи канал.")
        if any(not r.get("link") for r in rows_):
            lines.append("\n<i>Без посилання — приватний чат, де бот не адмін із правом запрошувати. Посилання з'явиться, коли дасте це право й натиснете «🔄 Оновити».</i>")
        arrows = [[(f"{i}. {short(r['title'], 20)}", f"m:cm:c:{r['id']}"), ("⬆️", f"m:cm:tbm:{r['id']}:up"), ("⬇️", f"m:cm:tbm:{r['id']}:down")]
                  for i, r in enumerate(rows_[:12], 1)]
        if rows_:
            lines.append("\n🏆 Порядок = пріоритет: вище — важливіше (дописи й завдання для спільнот виконуються в цьому порядку). ⬆️⬇️ — змінити.")
        return "\n".join(lines)[:4000], arrows + [[("🔄 Оновити дані", "m:cm:tbr"), ("📄 Файл (Excel/CSV)", "m:cm:tbf")], [("« Спільноти", "m:cm")]]

    def table_csv(self):
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["№", "Пріоритет", "Тип", "Назва", "Посилання", "@ім'я", "Учасників", "Платформа", "Бот адмін", "Додано", "Стан", "Правил увімкнено", "Побачено", "Прибрано", "Дописів"])
        for i, r in enumerate(self.table_rows(include_left=True), 1):
            rights, stats = json.loads(r["rights"] or "{}"), json.loads(r["stats"] or "{}")
            cfg = json.loads(r["cfg"] or "{}")
            sc = self.ranks.score("c", r["id"])
            w.writerow([i, "вищий" if sc > 0 else "нижчий" if sc < 0 else "звичайний", KIND_T.get(r["kind"], r["kind"]), r["title"], r.get("link") or "", r.get("username") or "", r.get("members") or "",
                        {"tg": "Telegram", "wa": "WhatsApp", "ig": "Instagram"}.get(r["platform"], r["platform"]),
                        ("так" if rights.get("admin") else "ні") if r["platform"] == "tg" else "—",
                        datetime.fromtimestamp(r["created"], self.tz).strftime("%d.%m.%Y"), "активний" if r["state"] == "active" else "вийшов",
                        sum(1 for v in (cfg.get("rules") or {}).values() if v), stats.get("seen", 0), stats.get("deleted", 0) + stats.get("hidden", 0), stats.get("posted", 0)])
        return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM — щоб Excel одразу показав українські літери

    # ------------------------------------------------------------ меню власника (дані «m:cm:…»)
    VIEWS = {"", "c", "r", "a", "rq", "j", "p", "w", "lg", "pe", "pl", "grs", "gr", "tb"}

    def is_view(self, parts):
        return (parts[2] if len(parts) > 2 else "") in self.VIEWS

    def counts(self):
        cs = self.chats()
        reqs = self.q("SELECT COUNT(*) n FROM cm_reqs WHERE status='open'", one=True)["n"]
        posts = self.q("SELECT COUNT(*) n FROM cm_posts WHERE status='active'", one=True)["n"]
        return {"chats": len(cs), "reqs": reqs, "posts": posts,
                "noadmin": [c["title"] for c in cs if c["platform"] == "tg" and not c["rights"].get("admin")],
                "rules_on": sum(sum(1 for v in c["cfg"]["rules"].values() if v) for c in cs),
                "stats": {k: sum(c["stats"].get(k, 0) for c in cs) for k in ("seen", "deleted", "warned", "muted", "banned", "posted", "permits", "approved", "hidden")}}

    def scr_list(self):
        cs = self.chats()
        lines = ["🏘 <b>Спільноти</b>", "Групи, канали й спільноти, де працює бот: правила, дозволи, вступ, публікації, моніторинг.", ""]
        rows = []
        for c in cs:
            badge = "" if c["cfg"]["enabled"] else " ⏸"
            open_r = self.q("SELECT COUNT(*) n FROM cm_reqs WHERE cid=? AND status='open'", (c["id"],), one=True)["n"]
            rows.append([(f"{KIND.get(c['kind'], '👥')} {short(c['title'], 28)}{badge}" + (f" · 🙋{open_r}" if open_r else ""), f"m:cm:c:{c['id']}")])
        if not cs:
            lines.append("Поки немає жодної. Як додати — «➕ Як додати бота».")
        plat = getattr(self.bridge, "platform", "tg") if self.bridge else "tg"
        extra = []
        if plat == "wa":
            extra.append(("🔄 Мої групи WhatsApp", "m:cm:warf"))
        if plat == "ig":
            extra.append(("📸 Коментарі Instagram", "m:cm:igon"))
        rows += [extra] if extra else []
        if cs:
            rows.insert(0, [(f"📋 Таблиця: де бот ({len(cs)})", "m:cm:tb")])
            rows.append([("👤 Хто що може (усюди)", "m:cm:pl:*"), ("🏷 Групи учасників", "m:cm:grs")])
        rows.append([("➕ Як додати бота", "m:cm:how"), (("🔒" if self.only_owner_add() else "🔓") + " Хто може додавати", "m:cm:own")])
        return "\n".join(lines), rows

    def scr_how(self):
        plat = getattr(self.bridge, "platform", "tg") if self.bridge else "tg"
        if plat == "wa":
            t = ("➕ <b>Групи WhatsApp</b>\n• Працює через WhatsApp Groups API: лише для номерів з офіційним бізнес-статусом (OBA), "
                 "до 8 учасників у групі, бот бачить групи, створені його номером.\n• Натисніть «🔄 Мої групи WhatsApp» — бот підтягне список.\n"
                 "• Можна: публікувати, приймати заявки на вступ, видаляти порушників з групи.\n• Не можна: видаляти чужі повідомлення "
                 "(WhatsApp цього не дозволяє) — тому замість видалення бот попереджає. Кожне повідомлення бота в групі рахується в ліміт 1000/міс.")
        elif plat == "ig":
            t = ("➕ <b>Instagram</b>\n• Груп і каналів API Instagram не дає, але бот може модерувати <b>коментарі</b> під вашими дописами "
                 "(сховати або видалити за правилами) і <b>публікувати</b> дописи з картинкою.\n• У кабінеті Meta підпишіть вебхук на поле <code>comments</code> "
                 "і дайте застосунку дозволи instagram_business_manage_comments та instagram_business_content_publish.\n• Натисніть «📸 Коментарі Instagram».")
        else:
            t = ("➕ <b>Як додати бота в групу або канал</b>\n1. Відкрийте групу/канал → Учасники → Додати → знайдіть бота за @ім'ям.\n"
                 "2. Зробіть його <b>адміністратором</b> з правами: видаляти повідомлення, блокувати користувачів, запрошувати (для заявок). "
                 "У каналі — право публікувати.\n3. Бот сам з'явиться тут і напише вам.\n\n"
                 "Команди в групі:\n• для всіх: /rules — правила, /request — попросити дозвіл;\n"
                 "• для адмінів чату (у відповідь на повідомлення): /warn, /mute 30, /unmute, /ban, /unban, /allow links, "
                 "/group VIP (додати людину в групу), /ungroup VIP.\n\n"
                 "💡 Щоб бот бачив усі повідомлення групи, він має бути адміністратором (або вимкніть Privacy Mode у @BotFather).")
        return t, [[("« Спільноти", "m:cm")]]

    def scr_chat(self, cid):
        c = self.chat(cid)
        if not c:
            return self.scr_list()
        s, cfg = c["stats"], c["cfg"]
        on = [RULE_T[k] for k, _, _ in RULES if cfg["rules"].get(k)]
        reqs = self.q("SELECT COUNT(*) n FROM cm_reqs WHERE cid=? AND status='open'", (cid,), one=True)["n"]
        posts = self.q("SELECT COUNT(*) n FROM cm_posts WHERE cid=? AND status='active'", (cid,), one=True)["n"]
        lines = [f"{KIND.get(c['kind'], '👥')} <b>{esc(c['title'])}</b> · {KIND_T.get(c['kind'], c['kind'])}" + ("" if cfg["enabled"] else " · ⏸ вимкнено")]
        if c.get("link"):
            lines.append(f"🔗 {esc(c['link'])}" + (f" · 👤 {c['members']}" if c.get("members") else ""))
        if c["platform"] == "tg":
            lines.append(self.rights_text(c["kind"], c["rights"]))
        if c["kind"] != "channel":
            lines.append("🛡 Правила: " + (", ".join(on) if on else "немає"))
        if c["kind"] not in ("channel", "ig"):
            lines.append(f"⚖️ Покарання: {ACTION_T[cfg['action']]}" + (f", після {cfg['warn_limit']} — тиша на добу" if cfg["action"] == "warn" else ""))
        elif c["kind"] == "ig":
            lines.append("⚖️ Порушення: " + dict(IG_ACTIONS)[cfg["ig_action"]])
        lines.append(f"📊 Побачено {s.get('seen', 0)} · видалено {s.get('deleted', 0) + s.get('hidden', 0)} · попереджень {s.get('warned', 0)} · "
                     f"тиша {s.get('muted', 0)} · вигнано {s.get('banned', 0)} · дописів {s.get('posted', 0)} · дозволів {s.get('permits', 0)}")
        rows = []
        if c["kind"] != "channel":
            rows.append([("🛡 Правила", f"m:cm:r:{cid}"), ("⚖️ Покарання", f"m:cm:a:{cid}")])
            rows.append([("👤 Хто що може", f"m:cm:pl:{cid}")])
            if c["kind"] != "ig":
                rows.append([(f"🙋 Запити{f' ({reqs})' if reqs else ''}", f"m:cm:rq:{cid}"), ("🚪 Вступ", f"m:cm:j:{cid}")])
        rows.append([(f"📣 Публікації{f' ({posts})' if posts else ''}", f"m:cm:p:{cid}"), ("👁 Моніторинг", f"m:cm:w:{cid}")])
        rows.append([("📜 Журнал", f"m:cm:lg:{cid}"), ("⏸ Вимкнути" if cfg["enabled"] else "▶️ Увімкнути", f"m:cm:en:{cid}")])
        tail = [("🔄 Перевірити права", f"m:cm:rf:{cid}")] if c["platform"] == "tg" else []
        tail.append(("🚪 Вийти" if c["platform"] == "tg" else "🗑 Прибрати", f"m:cm:lv:{cid}"))
        rows.append(tail)
        rows.append([("« Спільноти", "m:cm")])
        return "\n".join(lines), rows

    def scr_rules(self, cid):
        c = self.chat(cid)
        cfg = c["cfg"]
        limited = c["kind"] in ("wa_group", "ig")
        lines = [f"🛡 <b>Правила · {esc(c['title'])}</b>", "Увімкнене правило — заборона для учасників. Адміністраторів і тих, кому ви дали дозвіл, не стосується."]
        if limited:
            lines.append("<i>Тут доступні лише правила для тексту.</i>")
        rows = []
        for k, t, _ in RULES:
            if limited and k not in TEXT_RULES:
                continue
            rows.append([(("✅ " if cfg["rules"].get(k) else "▫️ ") + t, f"m:cm:rt:{cid}:{k}")])
        lines.append(f"\n🤬 Слова: {esc(', '.join(cfg['words'])) if cfg['words'] else '—'}")
        lines.append(f"🌊 Флуд: більше {cfg['flood_n']} повідомлень за {cfg['flood_sec']} с")
        if not limited:
            lines.append(f"🐣 Новачки мовчать: {mins_t(cfg['newbie_min'])} · 🌙 Нічна тиша: {cfg['night']}")
        rows.append([("✏️ Слова", f"m:cm:pe:{cid}:words"), ("🌊 Ліміт флуду", f"m:cm:fl:{cid}")])
        if not limited:
            rows.append([("🐣 Час для новачків", f"m:cm:nb:{cid}"), ("🌙 Години тиші", f"m:cm:pe:{cid}:night")])
        rows.append([("« Назад", f"m:cm:c:{cid}")])
        return "\n".join(lines), rows

    def scr_action(self, cid):
        c = self.chat(cid)
        cfg = c["cfg"]
        if c["kind"] == "ig":
            rows = [[(("● " if cfg["ig_action"] == k else "") + t, f"m:cm:ia:{cid}:{k}")] for k, t in IG_ACTIONS]
            return f"⚖️ <b>Що робити з коментарем-порушником</b>\n«Сховати» — коментар бачите лише ви й автор.", rows + [[("« Назад", f"m:cm:c:{cid}")]]
        lines = [f"⚖️ <b>Покарання · {esc(c['title'])}</b>", "Що робити, коли учасник порушує правило:"]
        if c["kind"] == "wa_group":
            lines.append("<i>У WhatsApp бот не може видалити чуже повідомлення: «Попередити» — попередження в групі, «Вигнати» — видалення з групи.</i>")
        rows = [[(("● " if cfg["action"] == k else "") + t, f"m:cm:ac:{cid}:{k}")] for k, t in ACTIONS if not (c["kind"] == "wa_group" and k in ("delete", "mute"))]
        rows.append([(f"⚠️ Ліміт попереджень: {cfg['warn_limit']}", f"m:cm:wl:{cid}"), (f"🔇 Тиша: {mins_t(cfg['mute_min'])}", f"m:cm:mm:{cid}")])
        rows.append([(("✅" if cfg["requests"] else "▫️") + " Кнопка «Попросити дозвіл»", f"m:cm:rqt:{cid}")])
        rows.append([("« Назад", f"m:cm:c:{cid}")])
        return "\n".join(lines), rows

    def scr_requests(self, cid):
        c = self.chat(cid)
        reqs = self.q("SELECT * FROM cm_reqs WHERE cid=? AND status='open' ORDER BY id DESC LIMIT 10", (cid,))
        perms = self.q("SELECT * FROM cm_permits WHERE cid=? ORDER BY until", (cid,))
        lines = [f"🙋 <b>Запити й дозволи · {esc(c['title'])}</b>"]
        rows = []
        if reqs:
            lines.append("\n<b>Чекають рішення:</b>")
        for r in reqs:
            what = "вступ" if r["kind"] == "join" else RULE_ASK.get(r["rule"], r["rule"])
            lines.append(f"• #{r['id']} {esc(r['name'])}: {esc(what)}")
            if r["kind"] == "join":
                rows.append([(f"✅ #{r['id']}", f"m:cm:jok:{r['id']}"), (f"❌ #{r['id']}", f"m:cm:jno:{r['id']}")])
            else:
                rows.append([(f"✅ #{r['id']} 1 день", f"m:cm:pok:{r['id']}:1d"), (f"✅ назавжди", f"m:cm:pok:{r['id']}:inf"), (f"❌", f"m:cm:pno:{r['id']}")])
        lines.append("\n<b>Діючі дозволи:</b>" if perms else "\nДіючих дозволів немає.")
        for p in perms:
            left = "назавжди" if not p["until"] else "до " + datetime.fromtimestamp(p["until"], self.tz).strftime("%d.%m %H:%M")
            lines.append(f"• {esc(p['name'] or p['user_id'])}: {esc(RULE_ASK.get(p['rule'], 'усе'))} — {left}")
            rows.append([(f"✖️ Забрати: {short(p['name'] or p['user_id'], 16)} · {RULE_T.get(p['rule'], 'усе')[:14]}", f"m:cm:prm:{cid}:{p['user_id']}:{p['rule']}")])
        rows.append([("« Назад", f"m:cm:c:{cid}")])
        return "\n".join(lines), rows

    def scr_joins(self, cid):
        c = self.chat(cid)
        cfg = c["cfg"]
        lines = [f"🚪 <b>Вступ · {esc(c['title'])}</b>", "Заявки на вступ (у Telegram — коли в посиланні-запрошенні ввімкнено «Заявки на вступ»)."]
        rows = [[(("● " if cfg["joins"] == k else "") + t, f"m:cm:jm:{cid}:{k}") for k, t in JOINS]]
        if c["platform"] == "tg":
            lines.append(f"\n👋 Привітання новачків: {esc(cfg['greet']) if cfg['greet'] else '—'}")
            rows.append([("✏️ Привітання", f"m:cm:pe:{cid}:greet")] + ([("✖️ Без привітання", f"m:cm:gx:{cid}")] if cfg["greet"] else []))
            rows.append([("🔗 Нове посилання-запрошення", f"m:cm:inv:{cid}")])
        else:
            rows.append([("🔗 Посилання-запрошення", f"m:cm:inv:{cid}")])
        rows.append([("« Назад", f"m:cm:c:{cid}")])
        return "\n".join(lines), rows

    def scr_posts(self, cid):
        c = self.chat(cid)
        ps = self.q("SELECT * FROM cm_posts WHERE cid=? AND status!='deleted' ORDER BY status='done', id DESC LIMIT 12", (cid,))
        lines = [f"📣 <b>Публікації · {esc(c['title'])}</b>"]
        if c["kind"] == "ig":
            lines.append("<i>Для Instagram перший рядок допису — посилання на картинку (https://…jpg), далі — підпис.</i>")
        rows = []
        for p in ps:
            s = json.loads(p["sched"] or "{}")
            st = {"active": "🟢", "paused": "⏸", "done": "✅"}.get(p["status"], "")
            lines.append(f"{st} #{p['id']} {'🤖 ' if p['kind'] == 'ai' else ''}«{esc(short(p['text'], 50))}» — {self.sched_text(s)}")
            if p["status"] != "done":
                rows.append([(f"▶️ #{p['id']} зараз", f"m:cm:pgo:{p['id']}"), ("⏸" if p["status"] == "active" else "▶️", f"m:cm:pps:{p['id']}"), ("🗑", f"m:cm:pdel:{p['id']}")])
        if not ps:
            lines.append("Дописів ще немає.")
        add = [("➕ Допис", f"m:cm:pe:{cid}:post")]
        if self.ai:
            add.append(("🤖 Допис від ШІ", f"m:cm:pe:{cid}:aipost"))
        rows.append(add)
        rows.append([("« Назад", f"m:cm:c:{cid}")])
        return "\n".join(lines), rows

    def scr_sched(self, pid):
        p = self.q("SELECT * FROM cm_posts WHERE id=?", (pid,), one=True)
        return (f"⏱ <b>Коли опублікувати допис #{pid}?</b>\n«{esc(short(p['text'], 120))}»",
                [[("▶️ Зараз", f"m:cm:ps:{pid}:now"), ("⏳ Через 1 год", f"m:cm:ps:{pid}:1h")],
                 [("🕘 У певний час…", f"m:cm:pe:{pid}:once"), ("📅 Щодня о…", f"m:cm:pe:{pid}:daily")],
                 [("🗓 Щотижня…", f"m:cm:pe:{pid}:weekly"), ("📝 Чернетка", f"m:cm:ps:{pid}:draft")]])

    def scr_watch(self, cid):
        c = self.chat(cid)
        cfg = c["cfg"]
        return (f"👁 <b>Моніторинг · {esc(c['title'])}</b>\nБот пересилає вам повідомлення з цими словами"
                + (" (і передає головному боту)" if self.on_event else "") + f".\nСлова: {esc(', '.join(cfg['watch'])) if cfg['watch'] else '—'}",
                [[("✏️ Слова-тригери", f"m:cm:pe:{cid}:watch")],
                 [(("✅" if cfg["watch_all"] else "▫️") + " Пересилати все", f"m:cm:wa:{cid}")],
                 [("« Назад", f"m:cm:c:{cid}")]])

    def scr_log(self, cid):
        c = self.chat(cid)
        rows = self.q("SELECT * FROM cm_log WHERE cid=? ORDER BY id DESC LIMIT 25", (cid,))
        lines = [f"📜 <b>Журнал · {esc(c['title'])}</b>"] + [f"{datetime.fromtimestamp(r['t'], self.tz).strftime('%d.%m %H:%M')} {esc(r['text'])}" for r in rows]
        if not rows:
            lines.append("Поки порожньо.")
        return "\n".join(lines), [[("« Назад", f"m:cm:c:{cid}")]]

    PROMPTS = {
        "words": "🤬 Напишіть заборонені слова через кому (порожньо — «-»).",
        "watch": "👁 Напишіть слова-тригери через кому (порожньо — «-»).",
        "night": "🌙 Напишіть години тиші, наприклад 23:00-07:00",
        "greet": "👋 Напишіть привітання. {name} — ім'я новачка.",
        "post": "📣 Напишіть текст допису.",
        "aipost": "🤖 Опишіть, про що має бути допис — ШІ напише його в момент публікації.",
        "once": "🕘 Коли? Формат: 25.12 18:00 або 18:00 (сьогодні/завтра).",
        "daily": "📅 О котрій щодня? Наприклад 09:30",
        "weekly": "🗓 Дні й час, наприклад: пн,ср,пт 10:00",
    }

    async def route(self, chat, parts):
        """parts = data.split(':'), data = 'm:cm:…'. Повертає (toast, screen)."""
        a = parts[2] if len(parts) > 2 else ""
        P = parts
        res = await self.route_policy(chat, P, a)
        if res is not None:
            return res
        toast = None
        if a == "":
            return None, self.scr_list()
        if a == "how":
            return None, self.scr_how()
        if a == "tb":
            return None, self.scr_table()
        if a == "tbm":
            self.ranks.move("c", f"{P[3]}:{P[4]}", P[5])
            return None, self.scr_table()
        if a == "tbr":
            for c in self.chats():
                await self.refresh_info(c)
            return "Дані оновлено", self.scr_table()
        if a == "tbf":
            import io
            from telegram import InputFile
            name = "де-бот-" + self.now().strftime("%Y-%m-%d") + ".csv"
            await self.bot.send_document(chat, InputFile(io.BytesIO(self.table_csv()), filename=name),
                                         caption="📋 Усі групи, канали й спільноти бота з посиланнями. Відкривається в Excel, Google Таблицях, Numbers.")
            return "Файл надіслано", self.scr_table()
        if a == "own":
            self.gset("only_owner_add", 0 if self.only_owner_add() else 1)
            return ("Додавати бота може лише власник" if self.only_owner_add() else "Додавати бота може будь-хто"), self.scr_list()
        if a == "warf":
            try:
                n = await self.wa_refresh()
                toast = f"Знайдено груп: {n}"
            except Exception as e:  # noqa: BLE001
                toast = f"Не вдалося: {e}"[:190]
            return toast, self.scr_list()
        if a == "igon":
            try:
                me = await self.bridge.api("GET", "me", params={"fields": "user_id,username"})
                cid = self.register("ig", me.get("user_id") or me.get("id"), "Коментарі @" + (me.get("username") or "instagram"), "ig")
                return "Коментарі під вашими дописами підключено", self.scr_chat(cid)
            except Exception as e:  # noqa: BLE001
                return f"Не вдалося: {e}"[:190], self.scr_list()
        # дії з дописом (перший аргумент — id допису)
        if a in ("pgo", "pps", "pdel", "ps"):
            pid = int(P[3])
            p = self.q("SELECT * FROM cm_posts WHERE id=?", (pid,), one=True)
            if not p:
                return None, self.scr_list()
            if a == "pgo":
                try:
                    await self.publish(pid)
                    toast = "Опубліковано"
                except Exception as e:  # noqa: BLE001
                    toast = f"Не вдалося: {e}"[:190]
            elif a == "pps":
                self.x("UPDATE cm_posts SET status=? WHERE id=?", ("paused" if p["status"] == "active" else "active", pid))
            elif a == "pdel":
                self.x("UPDATE cm_posts SET status='deleted' WHERE id=?", (pid,))
                toast = "Допис видалено"
            elif a == "ps":
                k = P[4]
                if k == "draft":
                    self.x("UPDATE cm_posts SET status='paused', sched='{}' WHERE id=?", (pid,))
                elif k == "1h":
                    self.x("UPDATE cm_posts SET sched=?, status='active' WHERE id=?", (json.dumps({"k": "once", "ts": time.time() + 3600}), pid))
                elif k == "now":
                    self.x("UPDATE cm_posts SET sched=?, status='active' WHERE id=?", (json.dumps({"k": "now"}), pid))
                    try:
                        await self.publish(pid)
                        toast = "Опубліковано"
                    except Exception as e:  # noqa: BLE001
                        toast = f"Не вдалося: {e}"[:190]
                toast = toast or "Збережено"
            return toast, self.scr_posts(p["cid"])
        if a == "pe" and len(P) > 4 and P[3].isdigit():  # час для допису: m:cm:pe:<id допису>:<як>
            p = self.q("SELECT cid FROM cm_posts WHERE id=?", (int(P[3]),), one=True)
            self.awaiting[chat] = (P[4], P[3])
            return None, (self.PROMPTS[P[4]], [[("✖️ Скасувати", f"m:cm:p:{p['cid']}")]])
        if a in ("jok", "jno"):
            r = self.q("SELECT cid FROM cm_reqs WHERE id=?", (int(P[3]),), one=True)
            toast = await self.decide_join(int(P[3]), a == "jok")
            return toast, self.scr_requests(r["cid"])
        if a in ("pok", "pno"):
            r = self.q("SELECT cid FROM cm_reqs WHERE id=?", (int(P[3]),), one=True)
            toast = await self.decide(int(P[3]), P[4] if a == "pok" else None)
            return toast, self.scr_requests(r["cid"])
        # усе інше — дії над спільнотою: m:cm:<дія>:<платформа>:<id чату>[:аргумент]
        cid = f"{P[3]}:{P[4]}" if len(P) > 4 else None
        extra = P[5] if len(P) > 5 else None
        c = self.chat(cid) if cid else None
        if not c:
            return None, self.scr_list()
        cfg = c["cfg"]
        if a == "c":
            return None, self.scr_chat(cid)
        if a == "r":
            return None, self.scr_rules(cid)
        if a == "a":
            return None, self.scr_action(cid)
        if a == "rq":
            return None, self.scr_requests(cid)
        if a == "j":
            return None, self.scr_joins(cid)
        if a == "p":
            return None, self.scr_posts(cid)
        if a == "w":
            return None, self.scr_watch(cid)
        if a == "lg":
            return None, self.scr_log(cid)
        if a == "pe":
            self.awaiting[chat] = (extra, cid)
            return None, (self.PROMPTS[extra], [[("✖️ Скасувати", f"m:cm:c:{cid}")]])
        if a == "rt":
            cfg["rules"][extra] = 0 if cfg["rules"].get(extra) else 1
            self.save_cfg(cid, cfg)
            if extra == "words" and cfg["rules"]["words"] and not cfg["words"]:
                self.awaiting[chat] = ("words", cid)
                return None, (self.PROMPTS["words"], [[("✖️ Скасувати", f"m:cm:r:{cid}")]])
            warn = c["platform"] == "tg" and cfg["rules"][extra] and not c["rights"].get("delete")
            return ("⚠️ Боту бракує права видаляти повідомлення" if warn else None), self.scr_rules(cid)
        if a == "fl":
            opts = [(3, 5), (5, 10), (8, 15), (12, 30)]
            cur = (cfg["flood_n"], cfg["flood_sec"])
            cfg["flood_n"], cfg["flood_sec"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[1]
            self.save_cfg(cid, cfg)
            return None, self.scr_rules(cid)
        if a == "nb":
            opts = [10, 30, 60, 180, 1440]
            cfg["newbie_min"] = opts[(opts.index(cfg["newbie_min"]) + 1) % len(opts)] if cfg["newbie_min"] in opts else 60
            self.save_cfg(cid, cfg)
            return None, self.scr_rules(cid)
        if a == "ac":
            cfg["action"] = extra
            self.save_cfg(cid, cfg)
            return None, self.scr_action(cid)
        if a == "ia":
            cfg["ig_action"] = extra
            self.save_cfg(cid, cfg)
            return None, self.scr_action(cid)
        if a == "wl":
            cfg["warn_limit"] = {2: 3, 3: 5, 5: 2}.get(cfg["warn_limit"], 3)
            self.save_cfg(cid, cfg)
            return None, self.scr_action(cid)
        if a == "mm":
            cfg["mute_min"] = MUTES[(MUTES.index(cfg["mute_min"]) + 1) % len(MUTES)] if cfg["mute_min"] in MUTES else 60
            self.save_cfg(cid, cfg)
            return None, self.scr_action(cid)
        if a == "rqt":
            cfg["requests"] = 0 if cfg["requests"] else 1
            self.save_cfg(cid, cfg)
            return None, self.scr_action(cid)
        if a == "jm":
            cfg["joins"] = extra
            self.save_cfg(cid, cfg)
            return None, self.scr_joins(cid)
        if a == "gx":
            cfg["greet"] = ""
            self.save_cfg(cid, cfg)
            return None, self.scr_joins(cid)
        if a == "inv":
            try:
                if c["platform"] == "tg":
                    link = (await self.bot.create_chat_invite_link(int(c["chat_id"]), creates_join_request=cfg["joins"] != "off")).invite_link
                else:
                    link = (await self.bridge.api("GET", f"{c['chat_id']}/invite_link")).get("invite_link")
                return None, (f"🔗 Посилання-запрошення в «{esc(c['title'])}»:\n{esc(link)}" + ("\n(з заявками на вступ)" if cfg["joins"] != "off" else ""),
                              [[("« Назад", f"m:cm:j:{cid}")]])
            except Exception as e:  # noqa: BLE001
                return f"Не вдалося: {e}"[:190], self.scr_joins(cid)
        if a == "wa":
            cfg["watch_all"] = 0 if cfg["watch_all"] else 1
            self.save_cfg(cid, cfg)
            return None, self.scr_watch(cid)
        if a == "en":
            cfg["enabled"] = 0 if cfg["enabled"] else 1
            self.save_cfg(cid, cfg)
            return ("Бот знову стежить за правилами" if cfg["enabled"] else "Правила на паузі (публікації й моніторинг працюють)"), self.scr_chat(cid)
        if a == "rf":
            rights = await self.tg_rights(int(c["chat_id"]))
            self.x("UPDATE cm_chats SET rights=? WHERE id=?", (json.dumps(rights), cid))
            return "Права оновлено", self.scr_chat(cid)
        if a == "prm":
            self.x("DELETE FROM cm_permits WHERE cid=? AND user_id=? AND rule=?", (cid, extra, P[6] if len(P) > 6 else "*"))
            return "Дозвіл забрано", self.scr_requests(cid)
        if a == "lv":
            if c["platform"] == "tg":
                try:
                    await self.bot.leave_chat(int(c["chat_id"]))
                except Exception:  # noqa: BLE001
                    pass
            self.x("UPDATE cm_chats SET state='left' WHERE id=?", (cid,))
            return "Готово", self.scr_list()
        return None, self.scr_chat(cid)

    async def on_text(self, chat, text):
        """Текст, на який чекав розділ. Повертає (toast, screen)."""
        what, ref = self.awaiting.pop(chat)
        t = text.strip()
        res = await self.on_text_policy(chat, what, ref, t)
        if res is not None:
            return res
        if what in ("once", "daily", "weekly"):
            pid = int(ref)
            p = self.q("SELECT * FROM cm_posts WHERE id=?", (pid,), one=True)
            try:
                s = self.parse_sched(what, t)
            except ValueError as e:
                self.awaiting[chat] = (what, ref)
                return None, (f"⚠️ {e}\n{self.PROMPTS[what]}", [[("✖️ Скасувати", f"m:cm:p:{p['cid']}")]])
            self.x("UPDATE cm_posts SET sched=?, status='active', last_run=? WHERE id=?",
                   (json.dumps(s), 0 if what == "once" else time.time(), pid))
            return f"Заплановано: {self.sched_text(s)}", self.scr_posts(p["cid"])
        c = self.chat(ref)
        cfg = c["cfg"]
        if what in ("words", "watch"):
            cfg[what] = [] if t in ("-", "—") else [w.strip() for w in re.split(r"[,\n;]", t) if w.strip()][:200]
            if what == "words":
                cfg["rules"]["words"] = 1 if cfg["words"] else 0
            self.save_cfg(ref, cfg)
            return "Збережено", self.scr_rules(ref) if what == "words" else self.scr_watch(ref)
        if what == "night":
            if not re.fullmatch(r"\d\d:\d\d-\d\d:\d\d", t.replace(" ", "")):
                self.awaiting[chat] = (what, ref)
                return None, ("⚠️ Формат: 23:00-07:00", [[("✖️ Скасувати", f"m:cm:r:{ref}")]])
            cfg["night"] = t.replace(" ", "")
            cfg["rules"]["night"] = 1
            self.save_cfg(ref, cfg)
            return "Нічну тишу ввімкнено", self.scr_rules(ref)
        if what == "greet":
            cfg["greet"] = t[:1000]
            self.save_cfg(ref, cfg)
            return "Привітання збережено", self.scr_joins(ref)
        if what in ("post", "aipost"):
            image = None
            if c["kind"] == "ig" and what == "post":
                first, _, rest = t.partition("\n")
                if not first.startswith("http"):
                    self.awaiting[chat] = (what, ref)
                    return None, ("⚠️ Перший рядок — посилання на картинку (https://…), далі — підпис.", [[("✖️ Скасувати", f"m:cm:p:{ref}")]])
                image, t = first.strip(), rest.strip()
            pid = self.x("INSERT INTO cm_posts(cid, text, kind, sched, created, image, status) VALUES (?,?,?,?,?,?, 'paused')",
                         (ref, t[:4000], "ai" if what == "aipost" else "text", "{}", time.time(), image))
            return None, self.scr_sched(pid)
        return None, self.scr_chat(ref)

    def parse_sched(self, what, t):
        now = self.now()
        if what == "daily":
            if not re.fullmatch(r"\d{1,2}:\d\d", t):
                raise ValueError("Не зрозумів час.")
            return {"k": "daily", "t": f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}"}
        if what == "weekly":
            m = re.fullmatch(r"([а-яієї,\s]+)\s+(\d{1,2}:\d\d)", t.lower())
            days = [WEEK.index(d.strip()) for d in (m.group(1).split(",") if m else []) if d.strip() in WEEK]
            if not m or not days:
                raise ValueError("Не зрозумів дні або час.")
            return {"k": "weekly", "d": sorted(set(days)), "t": f"{int(m.group(2).split(':')[0]):02d}:{m.group(2).split(':')[1]}"}
        m = re.fullmatch(r"(?:(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+)?(\d{1,2}):(\d\d)", t)
        if not m:
            raise ValueError("Не зрозумів дату й час.")
        d, mo, y, hh, mm = m.groups()
        when = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if d:
            when = when.replace(day=int(d), month=int(mo), year=int(y) if y else now.year)
        elif when <= now:
            when += timedelta(days=1)
        if when <= now:
            raise ValueError("Цей час уже минув.")
        return {"k": "once", "ts": when.timestamp()}

    def wants_text(self, chat):
        return chat in self.awaiting

    def reset(self, keep_chats=True):
        """Для «🏭 Скидання»: стерти налаштування й журнал, але не виходити з чатів."""
        with closing(self.db()) as con, con:
            for t in ("cm_permits", "cm_warns", "cm_reqs", "cm_posts", "cm_log", "cm_members", "cm_policy", "cm_gmembers", "cm_groups"):
                con.execute(f"DELETE FROM {t}")
            con.execute("UPDATE cm_chats SET cfg='{}', stats='{}'")
