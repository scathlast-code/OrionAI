"""📮 Скринька: інші люди пишуть власнику прямо в бот (без SendPulse), власник відповідає звідти ж.

- Незнайомі (не учасники ШІ) пишуть будь-що — повідомлення копіюються власнику з підписом і кнопками.
- Учасники пишуть власнику через «📮 Написати власнику» або /owner (звичайні їхні повідомлення йдуть до ШІ).
- Власник відповідає reply на скопійоване повідомлення (будь-яким типом) або кнопкою «↩️ Відповісти».
- 📣 Канали й коментарі: бот — адміністратор вашого каналу та його групи обговорення → нові дописи й коментарі
  (а також повідомлення каналу, якщо вони приходять у групу) теж падають у скриньку; відповідь reply іде прямо в коментарі.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing

from plugins import Plugin as Base, esc

log = logging.getLogger("orion.plugins.inbox")

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (user INTEGER PRIMARY KEY, name TEXT, username TEXT, first REAL, last REAL,
    count INTEGER DEFAULT 0, unread INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, at REAL, dir TEXT, kind TEXT, text TEXT,
    src_msg INTEGER, owner_chat INTEGER, owner_msg INTEGER);
CREATE INDEX IF NOT EXISTS msgs_owner ON msgs(owner_chat, owner_msg);
CREATE INDEX IF NOT EXISTS msgs_user ON msgs(user, id);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sanctions (chat INTEGER, user INTEGER, name TEXT, kind TEXT, until REAL, at REAL, PRIMARY KEY (chat, user));
CREATE TABLE IF NOT EXISTS chats (chat INTEGER PRIMARY KEY, title TEXT, type TEXT, username TEXT, posts INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0, asked INTEGER DEFAULT 0, seen REAL);
"""
DEFAULTS = {"who": "all", "greeting": "👋 Вітаю! Напишіть повідомлення або надішліть новину, фото чи файл — я передам власнику, і він відповість тут.",
            "ack": "1", "silent": "0", "flood": "20"}
WHO = {"all": "пишуть усі", "members": "лише учасники бота", "closed": "ніхто (закрито)"}


BOT_NAME = lambda: ""  # noqa: E731 — @ім'я бота; бот підставляє його після запуску


def thread_link(uid):
    """Посилання, що відкриває ПЕРЕПИСКУ з людиною в боті (і в застосунку) — працює завжди, на відміну від tg://user."""
    b = BOT_NAME()
    return f"https://t.me/{b}?start=inb_{int(uid)}" if b and uid and int(uid) > 0 else ""


def mention(uid, name, username=""):
    """Ім'я людини — дотик відкриває переписку з нею в боті + @username (відкриває її профіль), якщо є.
    tg://user?id не годиться: Telegram мовчки ігнорує його, якщо ваш застосунок цієї людини «не знає»."""
    link = thread_link(uid)
    if link:
        out = f'<a href="{link}">{esc(name or uid)}</a>'
    elif uid and int(uid) > 0:
        out = f'<a href="tg://user?id={int(uid)}">{esc(name or uid)}</a>'
    elif username:
        out = f'<a href="https://t.me/{esc(username)}">{esc(name)}</a>'
    else:
        out = esc(name or uid)
    return out + (f" @{esc(username)}" if username else "")


def profile_row(cb, uid, username=""):
    """«↗ Відкрити акаунт» — для людей з @username; без нього сторінку відкриває дотик до імені в тексті."""
    return [("↗ Відкрити акаунт", f"url:https://t.me/{username}")] if username else []


def kind_of(m):
    for k in ("photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation", "location", "contact", "poll"):
        if getattr(m, k, None):
            return k
    return "text"


KIND_ICON = {"text": "💬", "photo": "🖼", "video": "🎬", "voice": "🎙", "video_note": "⭕️", "audio": "🎵", "document": "📎",
             "sticker": "🙂", "animation": "🎞", "location": "📍", "contact": "👤", "poll": "📊"}


class Plugin(Base):
    id = "inb"
    icon = "📮"
    title = "Скринька"
    version = "1.0"
    admin_only = False
    summary = ("Люди пишуть вам прямо в бот — текст, фото, голосові, файли, новини. Вам приходить сповіщення з підписом, хто це, "
               "а відповідаєте ви просто відповіддю (reply) на повідомлення. Без SendPulse і сторонніх сервісів.")
    VIEWS = {"", "t", "set", "who", "bl"}

    def __init__(self, host):
        super().__init__(host)
        self.db_path = self.dir / "inbox.db"
        with closing(self.db()) as con, con:
            con.executescript(SCHEMA)
            if "src_chat" not in {r[1] for r in con.execute("PRAGMA table_info(msgs)")}:
                con.execute("ALTER TABLE msgs ADD COLUMN src_chat INTEGER")
        self.reply_to: dict[int, int] = {}       # власник -> кому відповідаємо наступним повідомленням
        self.compose: set[int] = set()           # учасники, які зараз пишуть власнику
        self.flood: dict[int, list] = {}
        self.commands = [("owner", self.cmd_owner, "Написати власнику бота")]

    def db(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def get(self, k):
        with closing(self.db()) as con:
            r = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return r["v"] if r else DEFAULTS.get(k, "")

    def set(self, k, v):
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def person(self, uid):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM people WHERE user=?", (int(uid),)).fetchone()
        return dict(r) if r else None

    def status_line(self):
        with closing(self.db()) as con:
            n, u = con.execute("SELECT COUNT(*), COALESCE(SUM(unread),0) FROM people").fetchone()
        return f"розмов {n} · непрочитаних {u} · пишуть: {WHO.get(self.get('who'))}"

    # ================================================================ вхідні
    async def on_private(self, update, ctx):
        """True — повідомлення оброблене скринькою (далі в боті не йде)."""
        msg = update.message
        if not msg or msg.chat.type != "private" or not update.effective_user:
            return False
        uid = update.effective_user.id
        H = self.host
        if H.is_admin(uid):
            target = None
            if msg.reply_to_message:
                target = self._owner_map(msg.chat_id, msg.reply_to_message.message_id)
            if target is None and uid in self.reply_to and not (msg.text or "").startswith("/"):
                target = self.reply_to.pop(uid)
            if target is None:
                return False
            if isinstance(target, tuple):  # коментар у групі каналу — відповідаємо там же, reply на нього
                return await self._send_to_chat(ctx, msg, *target)
            return await self._send_to_person(ctx, msg, target)
        if H.is_allowed(uid):
            if uid in self.compose and not (msg.text or "").startswith("/"):
                self.compose.discard(uid)
                await self._deliver(ctx, msg, update.effective_user, member=True)
                return True
            return False
        # незнайомий
        if self.host.extra.get("preinvited", lambda u: False)(update.effective_user):
            return False  # запрошений — далі стандартний потік згоди
        if self.get("who") != "all":
            return False
        if (msg.text or "").split(" ")[0].split("@")[0] in ("/id",) or (msg.text or "").startswith("/start inv_"):
            return False  # /id і запрошення — стандартний потік бота
        p = self.person(uid)
        if p and p["blocked"]:
            return True  # мовчки
        if (msg.text or "").startswith("/start"):
            return False  # «Старт» — заявка на доступ власнику (стандартний потік бота); писати в скриньку можна й далі
        if not self._flood_ok(uid):
            await msg.reply_text("⏳ Забагато повідомлень поспіль. Спробуйте трохи пізніше.")
            return True
        await self._deliver(ctx, msg, update.effective_user)
        return True

    def _flood_ok(self, uid):
        now = time.time()
        lst = [t for t in self.flood.get(uid, []) if t > now - 600]
        lst.append(now)
        self.flood[uid] = lst
        return len(lst) <= int(self.get("flood") or 20)

    def _owner_map(self, owner_chat, owner_msg):
        with closing(self.db()) as con:
            r = con.execute("SELECT user, src_chat, src_msg FROM msgs WHERE owner_chat=? AND owner_msg=? ORDER BY id DESC LIMIT 1",
                            (owner_chat, owner_msg)).fetchone()
        if not r:
            return None
        return (r["src_chat"], r["src_msg"]) if r["src_chat"] else r["user"]

    # ================================================================ 📣 канали й коментарі
    def chat_row(self, cid):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM chats WHERE chat=?", (int(cid),)).fetchone()
        return dict(r) if r else None

    def chats(self):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM chats ORDER BY type, title")]

    async def on_channel_post(self, update, ctx):
        """Дописи каналу й коментарі в його групі обговорення → скринька власнику (якщо для цього чату ввімкнено)."""
        m = update.channel_post or update.message
        if not m or not m.chat or m.chat.type == "private":
            return
        c = m.chat
        row = self.chat_row(c.id)
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO chats(chat,title,type,username,seen) VALUES(?,?,?,?,?) ON CONFLICT(chat) DO UPDATE SET "
                        "title=excluded.title, username=excluded.username, seen=excluded.seen",
                        (c.id, c.title or str(c.id), c.type, c.username or "", time.time()))
        if not row:  # бот уперше побачив цей чат — питаємо власника, чи стежити
            row = self.chat_row(c.id)
            kind = "канал" if c.type == "channel" else "групу"
            what = ("нові дописи" if c.type == "channel" else "коментарі й повідомлення")
            rows = [[(f"✅ Так, {what} у скриньку", self.cb("cht", c.id, "posts" if c.type == "channel" else "comments", 1))],
                    [("Ні", self.cb("chs"))]]
            for owner in self.host.admins():
                try:
                    await ctx.bot.send_message(owner, f"📣 Бот бачить {kind} «{esc(c.title or c.id)}». Надсилати {what} в 📮 Скриньку?",
                                               parse_mode="HTML", reply_markup=self._kb(rows))
                except Exception:  # noqa: BLE001
                    pass
            return
        if c.type == "channel":
            if not row["posts"]:
                return
            await self._deliver_chat(ctx, m, c, None, "📣 Новий допис")
            return
        if not row["comments"] or getattr(m, "is_automatic_forward", False):
            return  # копія допису каналу в групі — це не коментар
        u = m.from_user
        if u and (u.is_bot or self.host.is_admin(u.id)):
            return  # власні відповіді й інші боти — не потрібні
        post = m.reply_to_message
        about = ""
        if post is not None:
            about = (post.text or post.caption or "")[:120]
        await self._deliver_chat(ctx, m, c, u, "💬 Коментар" if post is not None else "💬 Повідомлення", about)

    async def _deliver_chat(self, ctx, m, c, u, what, about=""):
        now = time.time()
        sender = m.sender_chat
        uid = u.id if u else (sender.id if sender else c.id)
        name = u.full_name if u else (sender.title if sender else c.title or "")
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO people(user,name,username,first,last,count,unread) VALUES(?,?,?,?,?,1,1) "
                        "ON CONFLICT(user) DO UPDATE SET name=excluded.name, last=excluded.last, count=count+1, unread=unread+1",
                        (uid, name if uid < 0 or self.host.is_allowed(uid) else "", "", now, now))
        link = (f"https://t.me/{c.username}/{m.message_id}" if c.username
                else f"https://t.me/c/{str(c.id).removeprefix('-100')}/{m.message_id}")
        uname = (u.username if u else getattr(sender, "username", "")) or ""
        head = (f"{what} · <b>{esc(c.title or '')}</b>\n👤 {mention(uid, name, uname)} · <code>{uid}</code>"
                + (f"\n↪️ до допису: «{esc(about)}»" if about else "")
                + ("\n<i>Відповідь — reply на повідомлення нижче: бот відповість прямо в коментарях.</i>" if c.type != "channel" else ""))
        rows = [[("🔗 Відкрити", "url:" + link)]]
        if c.type != "channel":
            rows[0].insert(0, ("↩️ Відповісти там", self.cb("rc", c.id, m.message_id)))
            rows.append([("🔇 Мут 1 год", self.cb("mu", c.id, uid, 3600)), ("🔇 Мут 1 добу", self.cb("mu", c.id, uid, 86400))])
            rows.append([("🚫 Забанити", self.cb("ban", c.id, uid)), ("🗑 Видалити коментар", self.cb("del", c.id, m.message_id))])
        if uname:
            rows.append(profile_row(self.cb, uid, uname))
        rows.append([("✅ Прочитано", self.cb("ok", uid))])
        for owner in self.host.admins():
            try:
                await ctx.bot.send_message(owner, head, parse_mode="HTML", disable_notification=self.get("silent") == "1")
                copied = await ctx.bot.copy_message(owner, c.id, m.message_id, reply_markup=self._kb(rows),
                                                    disable_notification=self.get("silent") == "1")
                with closing(self.db()) as con, con:
                    con.execute("INSERT INTO msgs(user,at,dir,kind,text,src_msg,owner_chat,owner_msg,src_chat) VALUES(?,?,?,?,?,?,?,?,?)",
                                (uid, now, "in", kind_of(m), (m.text or m.caption or "")[:4000] if self.host.is_allowed(uid) else "",
                                 m.message_id, owner, copied.message_id, c.id))
            except Exception:  # noqa: BLE001
                log.exception("inbox: chat deliver failed")

    async def _send_to_chat(self, ctx, msg, chat, reply_to):
        from telegram import ReplyParameters
        try:
            await ctx.bot.copy_message(chat, msg.chat_id, msg.message_id,
                                       reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True))
        except Exception as exc:  # noqa: BLE001
            await msg.reply_text(f"⚠️ Не вдалося відповісти в чаті: {exc}\n(бот має бути адміністратором групи обговорення)")
            return True
        note = ""
        try:  # від чийого імені видно відповідь: бот-«анонімний адміністратор» пише від імені групи
            me = await ctx.bot.get_chat_member(chat, ctx.bot.id)
            if not getattr(me, "is_anonymous", False):
                note = ("\n⚠️ Відповідь видно від імені бота. Щоб бот писав від імені групи: група → Адміністратори → бот → "
                        "увімкніть «Залишатися анонімним» (Remain anonymous).")
        except Exception:  # noqa: BLE001
            pass
        await msg.reply_text("✅ Відповів у коментарях" + note)
        return True

    async def moderate(self, ctx, chat, user, kind, secs=0):
        """🔇 мут (тиша на час), 🚫 бан, а також зняття. user < 0 — людина пише від імені каналу (бан каналу-відправника)."""
        from telegram import ChatPermissions
        until = int(time.time() + secs) if secs else None
        name = (self.person(user) or {}).get("name") or str(user)
        bot = ctx.bot
        if kind == "mu":
            await bot.restrict_chat_member(chat, user, ChatPermissions(can_send_messages=False, can_send_audios=False, can_send_documents=False,
                                           can_send_photos=False, can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False,
                                           can_send_polls=False, can_send_other_messages=False, can_add_web_page_previews=False), until_date=until)
        elif kind == "unmu":
            await bot.restrict_chat_member(chat, user, ChatPermissions.all_permissions())
        elif kind == "ban":
            if user < 0:
                await bot.ban_chat_sender_chat(chat, user)
            else:
                await bot.ban_chat_member(chat, user)
        elif kind == "unban":
            if user < 0:
                await bot.unban_chat_sender_chat(chat, user)
            else:
                await bot.unban_chat_member(chat, user, only_if_banned=True)
        with closing(self.db()) as con, con:
            if kind in ("mu", "ban"):
                con.execute("INSERT OR REPLACE INTO sanctions(chat,user,name,kind,until,at) VALUES(?,?,?,?,?,?)",
                            (chat, user, name, kind, until, time.time()))
            else:
                con.execute("DELETE FROM sanctions WHERE chat=? AND user=?", (chat, user))
        return name

    def sanctions_screen(self, chat):
        with closing(self.db()) as con:
            rs = [dict(r) for r in con.execute("SELECT * FROM sanctions WHERE chat=? AND (until IS NULL OR until>?) ORDER BY at DESC LIMIT 40",
                                                (int(chat), time.time()))]
        c = self.chat_row(chat) or {}
        lines = [f"🛡 <b>Модерація · {esc(c.get('title', chat))}</b>", ""]
        rows = []
        for r in rs:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            till = datetime.fromtimestamp(r["until"], ZoneInfo("Europe/Kyiv")).strftime("до %d.%m %H:%M") if r["until"] else "назавжди"
            lines.append(f"{'🔇' if r['kind'] == 'mu' else '🚫'} {esc(r['name'])} · {till}")
            rows.append([(f"{'🔊 Зняти мут' if r['kind'] == 'mu' else '✅ Розбанити'}: {r['name']}"[:40],
                          self.cb("unmu" if r["kind"] == "mu" else "unban", chat, r["user"]))])
        if not rs:
            lines.append("Нікого не обмежено. Мут і бан — кнопками під коментарями у скриньці.")
        rows.append([("« Канали й коментарі", self.cb("chs"))])
        return "\n".join(lines), rows

    def forget_strangers(self, allowed):
        """Стерти імена й тексти людей без доступу. Автоматично більше НЕ викликається (власник вирішив зберігати все)."""
        with closing(self.db()) as con, con:
            for (uid,) in con.execute("SELECT user FROM people").fetchall():
                if uid > 0 and not allowed(uid):
                    con.execute("UPDATE people SET name='', username='', note='' WHERE user=?", (uid,))
                    con.execute("UPDATE msgs SET text='' WHERE user=?", (uid,))

    def chats_screen(self):
        cs = self.chats()
        lines = ["📣 <b>Канали й коментарі</b>",
                 "Як підключити: додайте бота <b>адміністратором</b> у ваш канал і в його групу обговорення (де коментарі). "
                 "Щойно там з'явиться перший допис чи коментар, бот спитає, чи надсилати їх у скриньку. "
                 "Відповідь reply на коментар у скриньці бот опублікує прямо під ним. Щоб бот писав <b>від імені групи</b>, а не від свого: "
                 "група → Адміністратори → бот → «Залишатися анонімним». У самому каналі бот завжди пише від імені каналу. "
                 "Для мутів і банів боту потрібне право «Блокувати користувачів», для видалення — «Видаляти повідомлення».", ""]
        rows = []
        for c in cs:
            ch = c["type"] == "channel"
            key = "posts" if ch else "comments"
            on = c[key]
            lines.append(f"{'📣' if ch else '💬'} {esc(c['title'])} — {'дописи' if ch else 'коментарі'}: {'✅' if on else '⏸'}")
            rows.append([(f"{'✅' if on else '⏸'} {c['title']}"[:40], self.cb("cht", c["chat"], key, 0 if on else 1))]
                        + ([("🛡 Модерація", self.cb("mod", c["chat"]))] if not ch else []))
        if not cs:
            lines.append("Поки бот не бачив жодного каналу чи групи.")
        rows.append([("« Налаштування скриньки", self.cb("set"))])
        return "\n".join(lines), rows

    async def _deliver(self, ctx, msg, user, member=False):
        now = time.time()
        k = kind_of(msg)
        text = msg.text or msg.caption or ""
        sname, suser, stext = user.full_name, user.username or "", text  # зберігаємо все: бот приватний (рішення власника)
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO people(user,name,username,first,last,count,unread) VALUES(?,?,?,?,?,1,1) "
                        "ON CONFLICT(user) DO UPDATE SET name=excluded.name, username=excluded.username, last=excluded.last, "
                        "count=count+1, unread=unread+1", (user.id, sname, suser, now, now))
            n = con.execute("SELECT count FROM people WHERE user=?", (user.id,)).fetchone()[0]
        fwd = getattr(msg, "forward_origin", None)
        fsrc = ""
        if fwd is not None:
            ch = getattr(fwd, "chat", None) or getattr(fwd, "sender_chat", None)
            fsrc = (ch.title if ch else getattr(getattr(fwd, "sender_user", None), "full_name", "") or getattr(fwd, "sender_user_name", "")) or "?"
        head = (f"📮 <b>{mention(user.id, user.full_name, user.username or '')}</b> · <code>{user.id}</code>"
                + (" · учасник" if member else "") + f" · {n}-е" + (f"\n↪️ переслано з «{esc(fsrc)}»" if fsrc else "")
                + "\n<i>Відповідь — просто reply на повідомлення нижче.</i>")
        rows = [[("↩️ Відповісти", self.cb("r", user.id)), ("📰 У ньюзрум", self.cb("n", user.id, msg.message_id))],
                [("🔇 Мут 1 год", f"m:bpn:{user.id}:mute:3600"), ("🔇 Мут 1 добу", f"m:bpn:{user.id}:mute:86400")],
                [("🚫 Забанити в боті", f"m:bpn:{user.id}:ban"), ("✅ Прочитано", self.cb("ok", user.id))]]
        if user.username:
            rows.append(profile_row(self.cb, user.id, user.username))
        silent = self.get("silent") == "1"
        bot = ctx.bot
        for owner in self.host.admins():
            try:
                await bot.send_message(owner, head, parse_mode="HTML", disable_notification=silent)
                copied = await bot.copy_message(owner, msg.chat_id, msg.message_id, reply_markup=self._kb(rows), disable_notification=silent)
                with closing(self.db()) as con, con:
                    con.execute("INSERT INTO msgs(user,at,dir,kind,text,src_msg,owner_chat,owner_msg) VALUES(?,?,?,?,?,?,?,?)",
                                (user.id, now, "in", k, stext[:4000], msg.message_id, owner, copied.message_id))
            except Exception:  # noqa: BLE001
                log.exception("inbox deliver to owner failed")
        if self.get("ack") == "1":
            await msg.reply_text("✅ Передав. Відповідь прийде сюди.")

    def _kb(self, rows):
        kb = self.host.extra.get("kb")
        return kb(rows) if kb else None

    async def _send_to_person(self, ctx, msg, target):
        p = self.person(target)
        try:
            sent = await ctx.bot.copy_message(target, msg.chat_id, msg.message_id)
        except Exception as exc:  # noqa: BLE001
            await msg.reply_text(f"⚠️ Не доставлено: {exc}\n(людина могла заблокувати бота)")
            return True
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO msgs(user,at,dir,kind,text,src_msg,owner_chat,owner_msg) VALUES(?,?,?,?,?,?,?,?)",
                        (target, time.time(), "out", kind_of(msg), (msg.text or msg.caption or "")[:4000], sent.message_id, msg.chat_id, msg.message_id))
            con.execute("UPDATE people SET unread=0 WHERE user=?", (target,))
        await msg.reply_text(f"✅ Надіслано: {(p or {}).get('name') or target}")
        return True

    async def cmd_owner(self, update, ctx):
        uid = update.effective_user.id
        text = (update.message.text or "").partition(" ")[2].strip()
        if self.host.is_admin(uid):
            t, rows = self.home(uid)
            return await self.host.notify(uid, t, rows)
        if self.get("who") == "closed":
            return await update.message.reply_text("📮 Власник зараз не приймає повідомлень.")
        if text:
            update.message.text = text
            await self._deliver(ctx, update.message, update.effective_user, member=True)
            return
        self.compose.add(uid)
        await update.message.reply_text("📮 Напишіть повідомлення власнику — наступне, що ви надішлете (текст, фото, файл, голосове), отримає саме він.")

    # ================================================================ екрани
    def home(self, chat):
        if not self.host.is_admin(chat):
            return ("📮 <b>Написати власнику</b>\n\nНаступне ваше повідомлення (будь-якого типу) отримає власник бота, а не ШІ.",
                    [[("✍️ Написати", self.cb("w"))]])
        with closing(self.db()) as con:
            ps = [dict(r) for r in con.execute("SELECT * FROM people WHERE blocked=0 ORDER BY unread>0 DESC, last DESC LIMIT 12")]
            tot = con.execute("SELECT COALESCE(SUM(unread),0) FROM people").fetchone()[0]
        lines = ["📮 <b>Скринька</b>", f"Непрочитаних: {tot} · пишуть: {WHO.get(self.get('who'))}", "",
                 "Повідомлення людей приходять вам одразу. Відповідайте reply на них — людина отримає відповідь від бота.",
                 "Дотик до людини нижче (або до її імені в сповіщенні) відкриває всю переписку з нею."]
        rows = []
        for p in ps:
            mark = f"🔵{p['unread']} " if p["unread"] else ""
            nm = p['name'] or ('@' + p['username'] if p['username'] else 'ID ' + str(p['user']))
            rows.append([(f"{mark}{nm[:26]} · {p['count']}", self.cb("t", p["user"]))])
        if not ps:
            lines.append("\nПоки ніхто не писав. Поділіться посиланням на бота — t.me/ваш_бот.")
        rows.append([("👥 Учасники та їхні розмови з ШІ", "m:acc")])
        rows.append([("⚙️ Налаштування скриньки", self.cb("set")), ("🚫 Заблоковані", self.cb("bl"))])
        return "\n".join(lines), rows

    PAGE = 15

    def thread(self, uid, page=0):
        p = self.person(uid)
        if not p:
            return "Розмову не знайдено.", [[("« Скринька", self.cb())]]
        owners = (self.host.admins() or [0])[:1]  # «вхідне» записане по копії на кожного власника — беремо копії першого
        q = ("FROM msgs WHERE user=? AND (dir='out' OR owner_chat IN (%s) OR owner_chat IS NULL)" % ",".join("?" * len(owners)))
        args = (int(uid), *owners)
        with closing(self.db()) as con:
            total = con.execute("SELECT COUNT(*) " + q, args).fetchone()[0]
            pages = max(1, (total + self.PAGE - 1) // self.PAGE)
            page = max(0, min(int(page or 0), pages - 1))
            ms = [dict(r) for r in con.execute("SELECT * " + q + " ORDER BY id DESC LIMIT ? OFFSET ?",
                                               (*args, self.PAGE, page * self.PAGE))][::-1]
            con.execute("UPDATE people SET unread=0 WHERE user=?", (int(uid),))
        name = p["name"] or ("@" + p["username"] if p["username"] else f"ID {p['user']}")
        lines = [f"📮 <b>{esc(name)}</b>" + (f" @{esc(p['username'])}" if p["username"] else "") + f" · <code>{p['user']}</code>",
                 f"Повідомлень: {p['count']} · перше: {time.strftime('%d.%m.%Y', time.localtime(p['first']))}" + (" · 🚫 заблоковано" if p["blocked"] else ""),
                 (f"Сторінка {page + 1} з {pages} (новіші — на першій)" if pages > 1 else ""), ""]
        for m in ms:
            who = "👤" if m["dir"] == "in" else "↩️ ви"
            lines.append(f"{time.strftime('%d.%m %H:%M', time.localtime(m['at']))} {who} {KIND_ICON.get(m['kind'], '')} {esc((m['text'] or '')[:300])}")
        if not ms:
            lines.append("<i>Повідомлень у скриньці ще немає.</i>")
        rows = [[("↩️ Відповісти", self.cb("r", uid)), (("✅ Розблокувати" if p["blocked"] else "🚫 Заблокувати"), self.cb("b", uid))]]
        nav = []
        if page < pages - 1:
            nav.append(("⬅️ Давніші", self.cb("t", uid, page + 1)))
        if page > 0:
            nav.append(("Новіші ➡️", self.cb("t", uid, page - 1)))
        if nav:
            rows.append(nav)
        if self.host.is_allowed(int(uid)):  # учасник бота — його розмови з ШІ теж відкриваються звідси
            rows.append([("🤖 Його розмови з ШІ", f"m:mdl:{int(uid)}")])
        if p["username"]:
            rows.append(profile_row(self.cb, uid, p["username"]))
        rows.append([("« Скринька", self.cb())])
        return "\n".join(x for x in lines if x is not None)[:4000], rows

    def settings(self):
        text = ("⚙️ <b>Налаштування скриньки</b>\n\n"
                f"Хто може писати вам: <b>{WHO.get(self.get('who'))}</b>\n"
                f"Відповідь людині «✅ Передав»: {'так' if self.get('ack') == '1' else 'ні'}\n"
                f"Сповіщення: {'без звуку' if self.get('silent') == '1' else 'зі звуком'}\n"
                f"Захист від флуду: до {self.get('flood')} повідомлень за 10 хв від однієї людини\n\n"
                f"Привітання (на /start):\n<i>{esc(self.get('greeting'))}</i>\n\n"
                "Учасники бота пишуть вам через /owner або «📮 Написати власнику»; незнайомі — просто пишуть боту.")
        rows = [[(("● " if self.get("who") == k else "") + v.split(" (")[0].capitalize(), self.cb("who", k))] for k, v in WHO.items()]
        rows += [[(("✅ «Передав»: так" if self.get("ack") == "1" else "✅ «Передав»: ні"), self.cb("ack")),
                  (("🔕 Без звуку" if self.get("silent") == "1" else "🔔 Зі звуком"), self.cb("sil"))],
                 [("✏️ Привітання", self.cb("gr")), ("📣 Канали й коментарі", self.cb("chs"))], [("« Скринька", self.cb())]]
        return text, rows

    def blocked_screen(self):
        with closing(self.db()) as con:
            ps = [dict(r) for r in con.execute("SELECT * FROM people WHERE blocked=1 ORDER BY last DESC LIMIT 30")]
        rows = [[(f"✅ Розблокувати {p['name'][:24]}", self.cb("b", p["user"]))] for p in ps] + [[("« Скринька", self.cb())]]
        return ("🚫 <b>Заблоковані</b>\n\n" + ("\n".join(esc(p["name"]) for p in ps) or "Нікого.")), rows

    async def route(self, update, ctx, chat, args):
        a = args[0] if args else ""
        x = args[1] if len(args) > 1 else None
        y = args[2] if len(args) > 2 else None
        if a == "w":
            self.compose.add(chat)
            return "✍️ Пишіть", ("📮 Наступне ваше повідомлення отримає власник.", [])
        if not self.host.is_admin(chat):
            return None, self.home(chat)
        if a == "":
            return None, self.home(chat)
        if a == "t":
            return None, self.thread(int(x), int(y or 0))
        if a == "r":
            self.reply_to[chat] = int(x)
            p = self.person(x) or {}
            return None, (f"↩️ Наступне ваше повідомлення (будь-якого типу) піде до <b>{esc(p.get('name', x))}</b>.",
                          [[("✖️ Скасувати", self.cb("rx"))]])
        if a == "rx":
            self.reply_to.pop(chat, None)
            return "Скасовано", self.home(chat)
        if a == "b":
            p = self.person(x)
            with closing(self.db()) as con, con:
                con.execute("UPDATE people SET blocked=? , unread=0 WHERE user=?", (0 if p and p["blocked"] else 1, int(x)))
            return ("Розблоковано" if p and p["blocked"] else "🚫 Заблоковано: більше не отримаєте від цієї людини"), self.thread(int(x))
        if a == "ok":
            with closing(self.db()) as con, con:
                con.execute("UPDATE people SET unread=0 WHERE user=?", (int(x),))
            return "✅ Прочитано", None
        if a == "n":
            with closing(self.db()) as con:
                r = con.execute("SELECT text, kind FROM msgs WHERE user=? AND src_msg=? ORDER BY id DESC LIMIT 1", (int(x), int(y))).fetchone()
            news = (self.host.extra.get("plugin") or (lambda pid: None))("news")
            if not news or not r:
                return "Ньюзрум недоступний", None
            p = self.person(x) or {}
            iid, st, dup = news.ingest("reader", r["text"] or "", f"Читач: {p.get('name', x)}", requested_by=chat)
            if st == "duplicate":
                return f"♻️ Повтор матеріалу #{dup}", None
            return (f"📰 Узяв у ньюзрум #{iid}" if iid else "Порожньо (фото/файл без тексту — перешліть його в ньюзрум вручну)"), None
        if a == "set":
            return None, self.settings()
        if a == "chs":
            return None, self.chats_screen()
        if a == "cht" and x and y:
            with closing(self.db()) as con, con:
                con.execute(f"UPDATE chats SET {'posts' if y == 'posts' else 'comments'}=?, asked=1 WHERE chat=?",
                            (int(args[3]) if len(args) > 3 else 1, int(x)))
            return "Збережено", self.chats_screen()
        if a in ("mu", "unmu", "ban", "unban") and x and y:
            try:
                name = await self.moderate(ctx, int(x), int(y), a, int(args[3]) if len(args) > 3 else 0)
            except Exception as exc:  # noqa: BLE001
                return f"Не вдалося: {str(exc)[:150]}", None
            msg = {"mu": f"🔇 {name}: тиша " + ("на годину" if len(args) > 3 and args[3] == "3600" else "на добу"),
                   "unmu": f"🔊 {name} знову може писати", "ban": f"🚫 {name} забанено", "unban": f"✅ {name} розбанено"}[a]
            return msg, (self.sanctions_screen(int(x)) if a in ("unmu", "unban") else None)
        if a == "del" and x and y:
            try:
                await ctx.bot.delete_message(int(x), int(y))
                return "🗑 Коментар видалено", None
            except Exception as exc:  # noqa: BLE001
                return f"Не вдалося видалити: {str(exc)[:150]}", None
        if a == "mod" and x:
            return None, self.sanctions_screen(int(x))
        if a == "rc" and x and y:
            self.reply_to[chat] = (int(x), int(y))
            return None, ("↩️ Наступне ваше повідомлення бот опублікує відповіддю на цей коментар.", [[("✖️ Скасувати", self.cb("rx"))]])
        if a == "who":
            self.set("who", x)
            return None, self.settings()
        if a == "ack":
            self.set("ack", "0" if self.get("ack") == "1" else "1")
            return None, self.settings()
        if a == "sil":
            self.set("silent", "0" if self.get("silent") == "1" else "1")
            return None, self.settings()
        if a == "gr":
            self.ask(chat, "gr")
            return None, ("✏️ Текст привітання для тих, хто вперше пише боту:", [[("✖️ Скасувати", "m:cancel")]])
        if a == "bl":
            return None, self.blocked_screen()
        return None, self.home(chat)

    def is_view(self, args):
        return not args or args[0] in self.VIEWS

    async def on_text(self, update, ctx, chat, key, data, text):
        if key == "gr":
            self.set("greeting", text.strip()[:1000])
            return "Збережено", self.settings()
        return None, self.home(chat)
