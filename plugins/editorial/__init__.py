"""🗞 Редакція: тихо збирає з редакційних чатів переслані новини, завдання, рішення й обговорення — списками з посиланнями
на конкретні повідомлення й відповідальними; плюс завдання з контент-плану (сайт, блог, соцмережі) з виконавцями.

У редакційні чати бот НІЧОГО не пише сам: ні уточнень, ні звітів, ні помилок. Усе редагують люди — будь-хто й будь-коли."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sqlite3
import time
from contextlib import closing

from plugins import Plugin as Base, esc

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, title TEXT, username TEXT, on_ INTEGER NOT NULL DEFAULT 0, seen REAL);
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, msg INTEGER, kind TEXT, text TEXT,
    author TEXT, who TEXT DEFAULT '', url TEXT DEFAULT '', at REAL, done INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0,
    day TEXT DEFAULT '', reason TEXT DEFAULT '', where_ TEXT DEFAULT '', key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS people (name TEXT PRIMARY KEY, src TEXT, added REAL);
CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, chats TEXT, text TEXT, mode TEXT, at REAL, hm TEXT,
    last REAL DEFAULT 0, on_ INTEGER DEFAULT 1, by INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS links (name TEXT PRIMARY KEY, uid INTEGER, username TEXT, at REAL);
CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, uid INTEGER, username TEXT DEFAULT '', at REAL);
CREATE TABLE IF NOT EXISTS uprefs (uid INTEGER PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}');
"""
# ⚙️ Особисте в «Редакції» — у кожного своє: порядок і фільтр списків, сповіщення, нагадування
UDEF = {"mine_first": True, "only_mine": False, "notify_assigned": True, "notify_new": "off", "remind": "", "show_done": False}
NEW_MODES = {"off": "вимкнено", "tasks": "лише нові завдання", "all": "усе нове з чатів"}
KINDS = {"news": "📰 Новини", "task": "✅ Завдання", "decision": "📌 Рішення", "talk": "💬 Обговорення"}
TALK = "💬 Обговорення й рішення"  # рішення й обговорення — один список (рішення позначені 📌)
TASK_RE = re.compile(r"(?i)\b(?:треба|потрібно|необхідно|зробити|напиши|напишіть|підготу\w*|завдання|дедлайн|до (?:завтра|понеділка|вівторка|"
                     r"середи|четверга|п.ятниці|суботи|неділі|\d{1,2}[.:]\d{2})|хто (?:візьме|напише|зробить)|візьм\w*|беру|прошу)\b")
DECISION_RE = re.compile(r"(?i)\b(?:вирішили|домовились|домовилися|рішення|затвердж\w*|погоджено|погодили|ухвалили|остаточно)\b")
TAKE_RE = re.compile(r"(?i)\b(?:я візьму|беру|візьму на себе|я напишу|я зроблю)\b")
WHERE_TASK = {"Сайт": "🌐 Сайт", "Блог": "✍️ Блог", "Соцмережі": "📱 Соцмережі", "SMM": "📱 Соцмережі"}
SOCIAL = {"Соцмережі", "SMM"}
# стан завдання: від «нове» до «готово»; для соцмереж — окремо по кожній мережі редакції
STATUS = {"new": "◽ Нове", "work": "🛠 У роботі", "review": "👀 На перевірці", "done": "✅ Готово"}
NET_STEP = {"new": "work", "work": "done", "done": "new"}
NET_MARK = {"new": "◽", "work": "🛠", "done": "✅"}
DRAFT_TO = {"draft": "work", "length_mismatch": "work", "approved": "review", "published": "done"}


def parse_day(t, today):
    """«30.11», «30.11.2026», «завтра», «сьогодні», «+3» → ISO-дата; «-» → None (без дедлайну); незрозуміле → False."""
    t = (t or "").strip().lower()
    if t in ("-", "—", "без"):
        return None
    if t in ("сьогодні",):
        return today.isoformat()
    if t in ("завтра",):
        return (today + dt.timedelta(days=1)).isoformat()
    if t in ("післязавтра",):
        return (today + dt.timedelta(days=2)).isoformat()
    m = re.fullmatch(r"\+\s*(\d{1,3})", t)
    if m:
        return (today + dt.timedelta(days=int(m[1]))).isoformat()
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", t)
    if m:
        y = int(m[3]) + (2000 if m[3] and len(m[3]) == 2 else 0) if m[3] else today.year
        try:
            d = dt.date(y, int(m[2]), int(m[1]))
        except ValueError:
            return False
        if d < today - dt.timedelta(days=30) and not m[3]:
            d = d.replace(year=y + 1)
        return d.isoformat()
    return False


class Plugin(Base):
    id = "red"
    icon = "🗞"
    title = "Редакція"
    version = "1.0"
    admin_only = False
    summary = ("Новини, завдання, рішення й обговорення з редакційних чатів — списками з посиланнями на повідомлення й відповідальними; "
               "завдання з контент-плану з виконавцями. У чати бот сам нічого не пише.")
    VIEWS = {"", "l", "i", "ch", "pp", "pc", "rep", "who", "po", "me"}
    REPORT_URL = "https://docs.google.com/presentation/d/1akz9pZoUv576-nLXmIJ-4nQBHTElNLcP/edit"

    def __init__(self, host):
        super().__init__(host)
        self.db_path = self.dir / "editorial.db"
        with closing(self.db()) as con, con:
            con.executescript(SCHEMA)
            cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
            for col, typ in (("status", "TEXT DEFAULT ''"), ("nets", "TEXT DEFAULT ''"), ("news", "INTEGER DEFAULT 0")):
                if col not in cols:
                    con.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
            if not con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():  # одна прив'язка на ім'я → кілька акаунтів
                con.execute("INSERT INTO accounts(name,uid,username,at) SELECT name,uid,COALESCE(username,''),at FROM links")

    def db(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    # ---------------------------------------------------------------- люди
    def people(self):
        names = set()
        news = self.host.extra.get("plugin", lambda pid: None)("news")
        if news:
            for items in (news.store.get("cplan_data") or {}).values():
                names.update(i["author"] for i in items if i.get("author"))
        with closing(self.db()) as con:
            names.update(r[0] for r in con.execute("SELECT name FROM people"))
            gone = {r[0] for r in con.execute("SELECT name FROM people WHERE src='removed'")}
        return sorted(n for n in names if n and n not in gone)

    # ---------------------------------------------------------------- 📥 тихий збір із чатів
    @staticmethod
    def msg_url(chat, msg_id):
        if chat.username:
            return f"https://t.me/{chat.username}/{msg_id}"
        cid = str(chat.id)
        return f"https://t.me/c/{cid[4:] if cid.startswith('-100') else cid.lstrip('-')}/{msg_id}"

    @staticmethod
    def classify(post, text):
        if getattr(post, "forward_origin", None) is not None or getattr(post, "forward_date", None):
            return "news"
        if DECISION_RE.search(text):
            return "decision"
        if TASK_RE.search(text):
            return "task"
        return "talk"

    async def on_channel_post(self, update, ctx):
        post = update.message if update.message and update.message.chat.type in ("group", "supergroup") else None
        if not post or (post.from_user and post.from_user.is_bot):
            return None
        ch = post.chat
        with closing(self.db()) as con, con:
            r = con.execute("SELECT on_ FROM chats WHERE id=?", (ch.id,)).fetchone()
            if not r:
                con.execute("INSERT INTO chats(id,title,username,on_,seen) VALUES(?,?,?,0,?)", (ch.id, ch.title or "", ch.username or "", time.time()))
        if not r:  # новий чат — питаємо адміністраторів ПРИВАТНО (у сам чат нічого не пишемо)
            await self.host.notify_admins(f"🗞 <b>Редакція</b>: бот бачить групу «{esc(ch.title or str(ch.id))}». Це редакційний чат? "
                                          "Якщо так — тихо збиратиму звідти новини, завдання, рішення й обговорення (у сам чат нічого не пишу).",
                                          [[("✅ Так, редакційний", self.cb("chon", ch.id)), ("💬 Чати", self.cb("ch"))]])
            return None
        if not r["on_"]:
            return None
        text = post.text or post.caption or ""
        if not text.strip():
            return None
        kind = self.classify(post, text)
        author = post.from_user.full_name if post.from_user else (post.sender_chat.title if post.sender_chat else "")
        who = ""
        mentions = re.findall(r"@(\w{4,})", text)
        if kind == "task" and TAKE_RE.search(text):
            who = author
        elif kind == "task" and mentions:
            who = "@" + mentions[0]
        with closing(self.db()) as con, con:
            con.execute("INSERT OR IGNORE INTO items(chat,msg,kind,text,author,who,url,at,key) VALUES(?,?,?,?,?,?,?,?,?)",
                        (ch.id, post.message_id, kind, text[:2000], author, who, self.msg_url(ch, post.message_id), time.time(),
                         f"m{ch.id}:{post.message_id}"))
            if author:
                con.execute("INSERT OR IGNORE INTO people(name,src,added) VALUES(?,?,?)", (author, "chat", time.time()))
            un = getattr(post.from_user, "username", None) if post.from_user else None
            if un:  # нікнейм, указаний для імені з плану, — тепер знаємо й ID людини
                con.execute("UPDATE accounts SET uid=? WHERE lower(username)=lower(?) AND uid IS NULL", (post.from_user.id, un))
        try:
            await self.notify_new(kind, text, self.msg_url(ch, post.message_id), ch.title or "")
        except Exception:  # noqa: BLE001
            pass
        return None

    # ---------------------------------------------------------------- 📋 завдання з контент-плану
    def sync_plan(self):
        """Позначки «сайт / блог / соцмережі» в плані — це завдання, імена — виконавці. Від учора на 60 днів уперед."""
        news = self.host.extra.get("plugin", lambda pid: None)("news")
        if not news:
            return
        data = news.store.get("cplan_data") or {}
        t0 = dt.date.today()
        lo, hi = (t0 - dt.timedelta(days=1)).isoformat(), (t0 + dt.timedelta(days=60)).isoformat()
        with closing(self.db()) as con, con:
            for day, items in data.items():
                if not (lo <= day <= hi):
                    continue
                topics = [i["title"] for i in items if not i.get("where")]
                for i in items:
                    where = i.get("where") or ""
                    reason = i["title"] if not where else (", ".join(topics[:2]) or "")
                    key = f"p{day}|{i['title']}|{where}"
                    con.execute("INSERT OR IGNORE INTO items(chat,msg,kind,text,author,who,url,at,day,reason,where_,key) "
                                "VALUES(0,0,'task',?,?,?,?,?,?,?,?,?)",
                                (i["title"], "контент-план", i.get("author") or "", i.get("src") or "", time.time(), day, reason, where, key))

    def up(self, uid):
        with closing(self.db()) as con:
            r = con.execute("SELECT data FROM uprefs WHERE uid=?", (int(uid),)).fetchone()
        try:
            d = json.loads(r[0]) if r else {}
        except ValueError:
            d = {}
        return {**UDEF, **d}

    def set_up(self, uid, **kw):
        d = self.up(uid)
        d.update(kw)
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO uprefs(uid, data) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET data=excluded.data",
                        (int(uid), json.dumps({k: d[k] for k in UDEF}, ensure_ascii=False)))
        return d

    def users_with(self, pred):
        with closing(self.db()) as con:
            uids = [r[0] for r in con.execute("SELECT uid FROM uprefs")]
        return [u for u in uids if pred(self.up(u))]

    def uids_of(self, name):
        """Акаунти Telegram, прив'язані до імені виконавця (може бути кілька)."""
        return sorted({a["uid"] for a in self.accounts(name) if a.get("uid")})

    def me_screen(self, chat):
        u = self.up(chat)
        names = self.my_names(chat)
        yn = lambda v: "так" if v else "ні"  # noqa: E731
        text = ("⚙️ <b>Мої налаштування редакції</b>\n<i>Лише для вас — інші бачать своє.</i>\n\n"
                f"🙋 Я в контент-плані й чатах: {', '.join(f'<code>{esc(n.upper())}</code>' for n in names) if names else 'ще не вказано — «👥 Люди» → своє ім’я → «🙋 Це я»'}\n"
                f"⬆️ Мої завдання першими: {yn(u['mine_first'])}\n"
                f"👤 Показувати лише мої: {yn(u['only_mine'])}\n"
                f"✅ Показувати й виконані: {yn(u['show_done'])}\n"
                f"🔔 Мене призначили виконавцем — сповіщати: {yn(u['notify_assigned'])}\n"
                f"📥 Нове з редакційних чатів — сповіщати: {NEW_MODES[u['notify_new']]}\n"
                f"⏰ Щоденне нагадування про мої завдання: {u['remind'] or 'вимкнено'}")
        nxt = {"off": "tasks", "tasks": "all", "all": "off"}[u["notify_new"]]
        rows = [[("⬆️ Мої першими: " + yn(u["mine_first"]), self.cb("mef", "mine_first")), ("👤 Лише мої: " + yn(u["only_mine"]), self.cb("mef", "only_mine"))],
                [("✅ Виконані: " + yn(u["show_done"]), self.cb("mef", "show_done")), ("🔔 Призначення: " + yn(u["notify_assigned"]), self.cb("mef", "notify_assigned"))],
                [("📥 Нове з чатів: " + NEW_MODES[u["notify_new"]], self.cb("men", nxt))],
                [("⏰ Нагадування: " + (u["remind"] or "вимк."), self.cb("met")), ("🙋 Хто я", self.cb("pp"))],
                [("« Редакція", self.cb())]]
        return text, rows

    async def notify_new(self, kind, text, url, chat_title):
        """📥 Сповіщення тим, хто сам цього захотів (у налаштуваннях) — лише в особисті, у редакційні чати нічого."""
        for uid in self.users_with(lambda u: u["notify_new"] == "all" or (u["notify_new"] == "tasks" and kind == "task")):
            await self.host.notify(uid, f"📥 <b>{KINDS.get(kind, kind)}</b> · {esc(chat_title)}\n{esc(text[:300])}"
                                   + (f'\n<a href="{esc(url)}">повідомлення</a>' if url else ""), [[("🗞 Редакція", self.cb())]])

    async def notify_assigned(self, iid, name, by=None):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM items WHERE id=?", (int(iid),)).fetchone()
        if not r:
            return
        for uid in self.uids_of(name):
            if uid == by or not self.up(uid)["notify_assigned"]:
                continue
            await self.host.notify(uid, f"🔔 Вас призначено виконавцем: {esc(self.task_line(dict(r)))}", [[("✍️ Відкрити", self.cb("i", iid))]])

    async def remind_due(self, now):
        hm = now.strftime("%H:%M")
        for uid in self.users_with(lambda u: u["remind"] == hm):
            key = f"{uid}:{now.date()}"
            if key in getattr(self, "_reminded", set()):
                continue
            self._reminded = getattr(self, "_reminded", set()) | {key}
            rs = self.my_tasks(uid, None, now.date().isoformat(), 15)
            if rs:
                await self.host.notify(uid, f"⏰ <b>Ваші редакційні завдання</b> на сьогодні й прострочені · {len(rs)}\n\n"
                                       + "\n".join(f"• {esc(self.task_line(r))}" for r in rs), [[("✅ Завдання", self.cb("l", "task"))]])

    def my_open(self, chat):
        """(усі відкриті завдання, з них — на цю людину за іменем у Telegram)."""
        try:
            self.sync_plan()
        except Exception:  # noqa: BLE001
            pass
        names = self.my_names(chat)
        with closing(self.db()) as con:
            n = con.execute("SELECT COUNT(*) FROM items WHERE kind='task' AND hidden=0 AND done=0").fetchone()[0]
            mine = con.execute(f"SELECT COUNT(*) FROM items WHERE kind='task' AND hidden=0 AND done=0 AND who IN ({','.join('?' * len(names))})",
                               names).fetchone()[0] if names else 0
        return n, mine

    def accounts(self, name):
        """Усі акаунти Telegram, прив'язані до імені (одна людина може мати кілька)."""
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM accounts WHERE name=? ORDER BY id", (name,))]

    def my_names(self, chat):
        """Імена з плану/чатів, прив'язані до цього користувача Telegram."""
        with closing(self.db()) as con:
            return sorted({r[0] for r in con.execute("SELECT name FROM accounts WHERE uid=?", (int(chat),))})

    def my_tasks(self, chat, day_from=None, day_to=None, limit=10):
        """Відкриті завдання, де ця людина — виконавець (за прив'язаними іменами): напр. «написати про день на сайт»."""
        names = self.my_names(chat)
        if not names:
            return []
        q = f"SELECT * FROM items WHERE kind='task' AND hidden=0 AND done=0 AND who IN ({','.join('?' * len(names))})"
        args = list(names)
        if day_from:
            q += " AND day>=?"
            args.append(day_from)
        if day_to:
            q += " AND day<=?"
            args.append(day_to)
        with closing(self.db()) as con:
            rs = [dict(r) for r in con.execute(q + " ORDER BY CASE WHEN day='' THEN 1 ELSE 0 END, day LIMIT ?", (*args, limit))]
        return rs

    @staticmethod
    def task_line(r):
        what = WHERE_TASK.get(r.get("where_") or "", "")
        head = f"{what}: " if what else ""
        verb = "написати про «" + r["text"] + "»" if r.get("chat") == 0 else r["text"]
        when = f" · до {r['day'][8:]}.{r['day'][5:7]}" if r.get("day") else ""
        why = f" (день «{r['reason'][:50]}»)" if r.get("reason") and r.get("where_") and r["reason"] != r["text"] else ""
        return head + verb[:140] + why + when

    def assigned(self, day):
        """Виконавці, призначені в боті, для днів контент-плану: {назва: [(хто, де)]}."""
        out = {}
        with closing(self.db()) as con:
            for r in con.execute("SELECT text, reason, who, where_ FROM items WHERE chat=0 AND day=? AND who<>'' AND hidden=0", (day,)):
                out.setdefault(r["reason"] if r["where_"] and r["reason"] else r["text"], []).append((r["who"], r["where_"]))
        return out

    # ---------------------------------------------------------------- екрани
    def counts(self):
        with closing(self.db()) as con:
            return {k: con.execute("SELECT COUNT(*) FROM items WHERE kind=? AND hidden=0 AND done=0", (k,)).fetchone()[0] for k in KINDS}

    def home(self, chat):
        try:
            self.sync_plan()
        except Exception:  # noqa: BLE001
            pass
        c = self.counts()
        with closing(self.db()) as con:
            on = con.execute("SELECT COUNT(*) FROM chats WHERE on_=1").fetchone()[0]
            nobody = con.execute("SELECT COUNT(*) FROM items WHERE kind='task' AND hidden=0 AND done=0 AND who=''").fetchone()[0]
        text = ("🗞 <b>Редакція</b>\n\n"
                f"Редакційних чатів: {on}" + ("" if on else " — додайте бота в редакційні чати, і він запитає вас тут, чи збирати з них") + "\n"
                + f"{KINDS['task']}: {c['task']}\n📰 Новини з чатів: {c['news']}\n{TALK}: {c['decision'] + c['talk']}" + (f"\n◽ Без відповідального: {nobody}" if nobody else "")
                + "\n\n<i>У редакційні чати бот сам нічого не пише — лише те й тоді, що ви скажете. Списки й відповідальних може правити будь-хто.</i>")
        drafts = self.drafts_new()
        rows = [[(f"{KINDS['task']} · {c['task']}", self.cb("l", "task")), (f"📰 Новини · {c['news'] + drafts}", self.cb("l", "news"))],
                [(f"{TALK} · {c['decision'] + c['talk']}", self.cb("l", "talk"))],
                [("💬 Редакційні чати", self.cb("ch")), ("👥 Люди", self.cb("pp"))],
                [("📋 Контент-план", "m:pl:news:obcp"), ("📊 Звіт", self.cb("rep"))],
                [("⚙️ Мої налаштування", self.cb("me"))]]
        return text, rows

    def news_plugin(self):
        return self.host.extra.get("plugin", lambda pid: None)("news")

    def drafts_new(self):
        news = self.news_plugin()
        try:
            return int(news.store.stats()["new"]) if news else 0
        except Exception:  # noqa: BLE001
            return 0

    def list_screen(self, chat, kind, page=0, mode=None):
        u = self.up(chat)
        mode = mode or ("all" if u["show_done"] else "open")
        cond = "hidden=0 AND done=0" if mode == "open" else "hidden=0"
        if kind == "decision":
            kind = "talk"
        kinds = ("decision", "talk") if kind == "talk" else (kind,)
        with closing(self.db()) as con:
            rs = [dict(r) for r in con.execute(f"SELECT * FROM items WHERE kind IN ({','.join('?' * len(kinds))}) AND {cond} "
                                               "ORDER BY CASE WHEN day='' THEN 1 ELSE 0 END, day, at DESC", kinds)]
        mine = set(self.my_names(chat))
        if u["only_mine"] and mine and kind == "task":
            rs = [r for r in rs if r.get("who") in mine]
        if u["mine_first"]:
            rs.sort(key=lambda r: r.get("who") not in mine)  # свої — першими, але видно всі
        per = 8
        page = max(0, min(int(page or 0), max(0, (len(rs) - 1) // per)))
        chunk = rs[page * per:(page + 1) * per]
        title = TALK if kind == "talk" else KINDS[kind]
        lines = [f"<b>{title} · {len(rs)}</b>", ""]
        if kind == "news":  # 📰 Новини: і з редакційних чатів, і вся робота з новинами (чернетки, пошук, джерела, архів)
            lines = [f"📰 <b>Новини</b>", "", f"📥 Нових чернеток: <b>{self.drafts_new()}</b> — перешліть сюди будь-який допис, і він стане чернеткою.",
                     "🔎 Теми нижче (🔥 гарячі, ♾ вічні, 🌿 екологія, 🌾 агро, 🩺 здоровʼя, ⚡️ енергетика…): бот знайде свіже, перевірить і напише.",
                     "", f"<b>З редакційних чатів · {len(rs)}</b>"]
        for n, r in enumerate(chunk, page * per + 1):
            lines.append(f"{n}. {'📌 ' if r['kind'] == 'decision' and kind == 'talk' else ''}{self.line(r)}")
        if not rs:
            lines.append("Поки порожньо." if kind != "news" else "Поки нічого — з'являться, щойно в редакційних чатах напишуть новину.")
        nums = [(str(page * per + k + 1), self.cb("i", r["id"])) for k, r in enumerate(chunk)]
        rows = [nums[i:i + 4] for i in range(0, len(nums), 4)]
        nav = ([("◀️", self.cb("l", kind, page - 1, mode))] if page else []) + ([("▶️", self.cb("l", kind, page + 1, mode))] if (page + 1) * per < len(rs) else [])
        if nav:
            rows.append(nav)
        rows.append([("👁 Усі, з виконаними" if mode == "open" else "👁 Лише відкриті", self.cb("l", kind, 0, "all" if mode == "open" else "open"))])
        news = self.news_plugin() if kind == "news" else None
        if news:
            try:  # 🔎 теми одразу тут: гарячі, вічні, провокації, блоги, екологія, агро, здоровʼя, енергетика… — і вся робота з чернетками
                back = news.cb()
                topics = [r for r in news.D_home(chat)[1] if not any(d == back for _, d in r)]
                rest = [[b for b in r if b[1] != news.cb("D")] for r in news.home(chat)[1]]
                rows += topics + [[("📊 Звіт: де просідаємо", news.cb("rp"))]] + [r for r in rest if r]
            except Exception:  # noqa: BLE001
                pass
        rows.append([("« Редакція", self.cb())])
        from footnotes import html_cut
        return html_cut("\n".join(lines), 4000), rows

    # ---------------------------------------------------------------- стан завдань
    def our_nets(self):
        """Мережі редакції — ті, що вибрані в «📲 Під мережі» Новинарні (там же їх і змінюють)."""
        news = self.news_plugin()
        try:
            from plugins.newsroom.crosspost import NETWORKS, ORDER
            nets = news.xp_cfg()["nets"] if news else ORDER
            return [(k, NETWORKS[k]) for k in ORDER if k in nets]
        except Exception:  # noqa: BLE001
            return []

    def nets_of(self, r):
        try:
            d = json.loads(r.get("nets") or "{}")
        except ValueError:
            d = {}
        if (r.get("where_") or "") in SOCIAL:
            for k, _v in self.our_nets():
                d.setdefault(k, "new")
            dr = self.draft_of(r) if r.get("news") else None
            if dr and dr["status"] == "published" and "tg" in d:
                d["tg"] = "done"  # опубліковано з Новинарні в Telegram-канал
        return d

    def draft_of(self, r):
        news = self.news_plugin()
        if not news or not r.get("news"):
            return None
        did = news.store.draft_for_item(int(r["news"]))
        return news.store.draft(did) if did else None

    def status_of(self, r):
        """Стан з урахуванням новини з Новинарні (чернетка → у роботі, схвалено → на перевірці, опубліковано → готово)
        і мереж (усі ✅ → готово)."""
        if r.get("done"):
            return "done"
        d = self.draft_of(r)
        st = r.get("status") or "new"
        nets = self.nets_of(r)
        if d:
            ds = DRAFT_TO.get(d["status"], "work")
            if nets and ds == "done":
                ds = "work"  # у соцмережах готово лише тоді, коли вийшло в усіх мережах (Telegram позначається сам)
            st = max(st, ds, key=list(STATUS).index)
        if nets and all(v == "done" for v in nets.values()):
            return "done"
        if nets and any(v != "new" for v in nets.values()) and st == "new":
            st = "work"
        return st

    def set_status(self, iid, st):
        with closing(self.db()) as con, con:
            con.execute("UPDATE items SET status=?, done=? WHERE id=?", (st, 1 if st == "done" else 0, int(iid)))

    def line(self, r):
        head = ""
        if r.get("day"):
            head = f"📅 {r['day'][8:]}.{r['day'][5:7]} · "
        if r.get("where_"):
            head += WHERE_TASK.get(r["where_"], r["where_"]) + ": "
        text = esc(re.sub(r"\s+", " ", r["text"])[:160])
        who = f" · 👤 <code>{esc(r['who'].upper())}</code>" if r.get("who") else " · ◽ ніхто"
        why = f" · до дня «{esc(r['reason'][:60])}»" if r.get("reason") and r.get("where_") else ""
        src = f' · <a href="{esc(r["url"])}">{"контент-план" if r["chat"] == 0 else "повідомлення"}</a>' if r.get("url") else ""
        mark = ""
        if r["kind"] == "task":
            st = self.status_of(r)
            mark = STATUS[st].split(" ")[0] + " "
            if r.get("day") and st != "done" and r["day"] < dt.date.today().isoformat():
                mark += "⏰ "
            nets = self.nets_of(r)
            if nets:
                icons = {k: v[0] for k, v in self.our_nets()}
                text += " · " + " ".join(icons.get(k, k) + NET_MARK[v] for k, v in nets.items() if k in icons)
        elif r.get("done"):
            mark = "✅ "
        return mark + head + text + why + (who if r["kind"] == "task" else (f" · ✍️ <code>{esc(r['author'].upper())}</code>" if r.get("author") else "")) + src

    def item_screen(self, chat, iid, note=""):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM items WHERE id=?", (int(iid),)).fetchone()
        if not r:
            return self.home(chat)
        r = dict(r)
        text = f"<b>{KINDS[r['kind']]}</b>" + (f" · {TALK}" if r["kind"] in ("decision", "talk") else "") + f"\n\n{self.line(r)}" + (f"\n\n{note}" if note else "")
        if r["kind"] == "task":
            return self.task_screen(chat, r, text)
        rows = [[("👤 Відповідальний", self.cb("who", iid)), (("↩️ Не виконано" if r["done"] else "✅ Виконано"), self.cb("done", iid))],
                [("✏️ Текст", self.cb("ed", iid)), ("🔀 Тип", self.cb("kd", iid)), ("🗑 Прибрати", self.cb("hide", iid))],
                [("« До списку", self.cb("l", "talk" if r["kind"] == "decision" else r["kind"]))]]
        return text, rows

    def task_screen(self, chat, r, text):
        iid = r["id"]
        st = self.status_of(r)
        d = self.draft_of(r)
        nets = self.nets_of(r)
        lines = [text, "", f"Стан: <b>{STATUS[st]}</b>" + (f" · дедлайн {r['day'][8:]}.{r['day'][5:7]}" if r.get("day") else " · без дедлайну")]
        if d:
            lines.append(f"📰 Новина #{d['id']} у Новинарні: {esc(d['status'])} · «{esc(d['headline'][:80])}»")
        if nets:
            names = dict(self.our_nets())
            lines += ["", "<b>По мережах</b> (натискайте: ◽ → 🛠 → ✅):"] + [f"{names[k][0]} {names[k][1]}: {NET_MARK[v]}" for k, v in nets.items() if k in names]
        rows = [[((("● " if st == k else "") + v), self.cb("st", iid, k)) for k, v in STATUS.items()][:2],
                [((("● " if st == k else "") + v), self.cb("st", iid, k)) for k, v in STATUS.items()][2:]]
        if nets:
            names = dict(self.our_nets())
            btns = [(names[k][0] + NET_MARK[v], self.cb("nt", iid, k)) for k, v in nets.items() if k in names]
            rows += [btns[i:i + 4] for i in range(0, len(btns), 4)]
        news = self.news_plugin()
        if news:
            rows.append([(f"📰 Новина #{d['id']}", news.cb("d", d["id"]))] if d else [("📰 Написати новину", self.cb("wn", iid))])
        rows += [[("👤 Відповідальний", self.cb("who", iid)), ("📅 Дедлайн", self.cb("dl", iid))],
                 [("✏️ Текст", self.cb("ed", iid)), ("🔀 Тип", self.cb("kd", iid)), ("🗑 Прибрати", self.cb("hide", iid))],
                 [("« До списку", self.cb("l", "task"))]]
        return "\n".join(lines), rows

    def who_screen(self, chat, iid):
        btns = [(n, self.cb("set", iid, k)) for k, n in enumerate(self.people())][:24]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        rows += [[("✏️ Вписати інше ім'я", self.cb("whoask", iid)), ("◽ Нікого", self.cb("set", iid, "-"))], [("« Назад", self.cb("i", iid))]]
        return "👤 <b>Хто відповідальний?</b>\nСписок людей — з контент-плану й редакційних чатів (правиться в «👥 Люди»).", rows

    def set_who(self, iid, name):
        """Призначити; одну тему на сайт двоє не беруть."""
        with closing(self.db()) as con, con:
            r = con.execute("SELECT * FROM items WHERE id=?", (int(iid),)).fetchone()
            if not r:
                return "Не знайдено"
            if name and r["where_"] == "Сайт" and r["day"]:
                other = con.execute("SELECT who FROM items WHERE id<>? AND day=? AND where_='Сайт' AND text=? AND who<>'' AND hidden=0",
                                    (int(iid), r["day"], r["text"])).fetchone()
                if other and other["who"] != name:
                    return f"⚠️ Цю тему на сайт уже бере {other['who']} — двоє на одну тему для сайту не беруть"
            con.execute("UPDATE items SET who=? WHERE id=?", (name or "", int(iid)))
        return "👤 Призначено" if name else "◽ Без відповідального"

    def chats_screen(self, chat):
        with closing(self.db()) as con:
            rs = [dict(r) for r in con.execute("SELECT * FROM chats ORDER BY on_ DESC, title")]
        lines = ["💬 <b>Редакційні чати</b>", "", "Увімкнені — бот тихо збирає звідти новини, завдання, рішення й обговорення. "
                 "Писати в них він не буде нічого, доки ви не задасте що й коли — кнопкою «📤 Що й коли писати» нижче.", ""]
        rows = []
        for r in rs:
            lines.append(f"{'✅' if r['on_'] else '▫️'} {esc(r['title'] or str(r['id']))}")
            rows.append([(("✅ " if r["on_"] else "▫️ ") + (r["title"] or str(r["id"]))[:30], self.cb("chon" if not r["on_"] else "choff", r["id"]))])
        if not rs:
            lines.append("Поки жодного: додайте бота в редакційний чат — він з'явиться тут, а вам прийде питання в особисті.")
        with closing(self.db()) as con:
            planned = con.execute("SELECT COUNT(*) FROM posts WHERE on_=1").fetchone()[0]
        lines += ["", f"📤 Що й коли писати в ці чати: {'заплановано ' + str(planned) if planned else 'нічого не заплановано'}."]
        rows.append([(f"📤 Що й коли писати · {planned}", self.cb("po"))])
        rows.append([("« Редакція", self.cb())])
        return "\n".join(lines)[:4000], rows

    def people_screen(self, chat):
        ppl = self.people()
        lines = ["👥 <b>Люди редакції</b>", "", "Імена беруться з контент-плану й чатів. Торкніться імені, щоб прив'язати його до людини в Telegram "
                 "(або натисніть «Це я»), — тоді бот знатиме, чиї це завдання.", ""]
        for n in ppl:
            acc = self.accounts(n)
            tg = (" → " + ", ".join(("@" + esc(a["username"])) if a.get("username") else "акаунт" for a in acc)) if acc else ""
            lines.append(f"• <code>{esc(n.upper())}</code>{tg}")
        btns = [(n, self.cb("pc", k)) for k, n in enumerate(ppl)][:30]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)] + [[("➕ Додати людину", self.cb("ppadd"))], [("« Редакція", self.cb())]]
        return "\n".join(lines)[:4000], rows

    def person_screen(self, chat, k):
        ppl = self.people()
        if not (k is not None and str(k).isdigit() and int(k) < len(ppl)):
            return self.people_screen(chat)
        n = ppl[int(k)]
        acc = self.accounts(n)
        name_of = self.host.extra.get("user_name", lambda c: "")
        lines = [f"👤 <b><code>{esc(n.upper())}</code></b>", "", f"Акаунти Telegram · {len(acc)}:" if acc else "Акаунти Telegram: не прив'язано"]
        for a in acc:
            lines.append("• " + (f"@{esc(a['username'])}" if a.get("username") else "") + (f" {esc(name_of(a['uid']))}" if a.get("uid") else "")
                         + ("" if a.get("uid") else " (ID стане відомий, щойно людина напише в редакційному чаті)"))
        lines += ["", "Прив'язувати може будь-хто; в однієї людини може бути кілька акаунтів."]
        rows = [[("🙋 Це я", self.cb("pme", k)), ("➕ @нікнейм", self.cb("pnick", k))]]
        rows += [[(f"🔓 Відв'язати {('@' + a['username']) if a.get('username') else 'акаунт ' + str(i + 1)}", self.cb("punl", k, a["id"]))]
                 for i, a in enumerate(acc)][:8]
        rows += [[("✖️ Прибрати зі списку", self.cb("ppdel", k))], [("« Люди", self.cb("pp"))]]
        return "\n".join(lines), rows

    def add_account(self, name, uid=None, username=""):
        with closing(self.db()) as con, con:
            if uid is not None and con.execute("SELECT 1 FROM accounts WHERE name=? AND uid=?", (name, uid)).fetchone():
                return False
            if username and con.execute("SELECT 1 FROM accounts WHERE name=? AND lower(username)=lower(?)", (name, username)).fetchone():
                return False
            con.execute("INSERT INTO accounts(name,uid,username,at) VALUES(?,?,?,?)", (name, uid, username or "", time.time()))
        return True

    # ---------------------------------------------------------------- 📤 що й коли писати в редакційні чати (лише за вказівкою)
    def tz(self):
        return self.host.tz or dt.timezone(dt.timedelta(hours=2))

    def chats_on(self):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM chats WHERE on_=1 ORDER BY title")]

    def posts_screen(self, chat):
        with closing(self.db()) as con:
            ps = [dict(r) for r in con.execute("SELECT * FROM posts ORDER BY on_ DESC, id DESC")]
        names = {c["id"]: c["title"] for c in self.chats_on()}
        lines = ["📤 <b>Що й коли писати в редакційні чати</b>", "",
                 "Бот пише в редакційні чати лише те й тоді, що ви тут задали, — і нічого більше (ні уточнень, ні звітів, ні помилок).", ""]
        rows = []
        for p in ps[:20]:
            ch = ", ".join(names.get(c, "чат") for c in json.loads(p["chats"] or "[]"))
            when = {"daily": f"щодня о {p['hm']}", "once": dt.datetime.fromtimestamp(p["at"] or 0, self.tz()).strftime("%d.%m %H:%M")}.get(p["mode"], "одразу")
            lines.append(f"{'🟢' if p['on_'] else '⚪'} {esc(when)} → {esc(ch)}: {esc(p['text'][:80])}")
            rows.append([(f"{'⏸' if p['on_'] else '▶️'} {p['text'][:20]}", self.cb("poon", p["id"])), ("🗑", self.cb("podel", p["id"]))])
        if not ps:
            lines.append("Поки нічого не заплановано.")
        rows += [[("➕ Нове повідомлення", self.cb("ponew"))], [("« Редакційні чати", self.cb("ch"))]]
        return "\n".join(lines)[:4000], rows

    def draft_screen(self, chat):
        d = self._pd.get(chat) or {}
        sel = set(d.get("chats", []))
        chats = self.chats_on()
        rows = [[(("✅ " if c["id"] in sel else "▫️ ") + (c["title"] or str(c["id"]))[:30], self.cb("pochat", c["id"]))] for c in chats]
        rows += [[("⚡ Надіслати зараз", self.cb("powhen", "now")), ("🕐 Один раз…", self.cb("powhen", "once"))],
                 [("🔁 Щодня о…", self.cb("powhen", "daily"))], [("✖️ Скасувати", self.cb("po"))]]
        text = (f"📤 <b>Нове повідомлення</b>\n\n<blockquote>{esc(d.get('text', ''))[:1500]}</blockquote>\n"
                "Оберіть чати (✅) і коли надіслати." + ("" if chats else "\n\n⚠️ Немає увімкнених редакційних чатів — увімкніть у «💬 Редакційні чати»."))
        return text, rows

    def confirm_screen(self, chat):
        d = self._pd.get(chat) or {}
        names = {c["id"]: c["title"] for c in self.chats_on()}
        when = {"now": "одразу", "daily": f"щодня о {d.get('hm')}",
                "once": dt.datetime.fromtimestamp(d.get("at") or 0, self.tz()).strftime("%d.%m %H:%M")}.get(d.get("mode"), "?")
        return (f"📤 <b>Перевірте</b>\n\nКуди: {esc(', '.join(names.get(c, 'чат') for c in d.get('chats', [])))}\nКоли: {when}\n\n"
                f"<blockquote>{esc(d.get('text', ''))[:1500]}</blockquote>"), [[("✅ Підтвердити", self.cb("pook")), ("✖️ Скасувати", self.cb("po"))]]

    async def send_post(self, p):
        bot = self.host.bot()
        for c in json.loads(p["chats"] or "[]"):
            try:
                await bot.send_message(int(c), p["text"][:4096])
            except Exception:  # noqa: BLE001 — у чат про помилку НЕ пишемо; лише адмінам у приватні
                await self.host.notify_admins(f"⚠️ Редакція: не вдалося надіслати заплановане повідомлення в чат {c}.")

    async def posts_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                now = dt.datetime.now(self.tz())
                try:
                    await self.remind_due(now)
                except Exception:  # noqa: BLE001
                    pass
                with closing(self.db()) as con:
                    ps = [dict(r) for r in con.execute("SELECT * FROM posts WHERE on_=1")]
                for p in ps:
                    due = False
                    if p["mode"] == "once" and p["at"] and p["at"] <= now.timestamp() and not p["last"]:
                        due = True
                    elif p["mode"] == "daily" and p["hm"] == now.strftime("%H:%M") and now.timestamp() - (p["last"] or 0) > 3600:
                        due = True
                    if due:
                        await self.send_post(p)
                        with closing(self.db()) as con, con:
                            con.execute("UPDATE posts SET last=?, on_=CASE WHEN mode='once' THEN 0 ELSE on_ END WHERE id=?", (now.timestamp(), p["id"]))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

    async def start(self):
        self._pd = {}
        self._loop = asyncio.get_running_loop().create_task(self.posts_loop())
        self._rep = asyncio.get_running_loop().create_task(self.report_watch())

    # ---------------------------------------------------------------- 📊 звіт (лише читаємо; правки — людьми)
    def report_file(self):
        return self.dir / "report.json"

    def report_data(self):
        try:
            return json.loads(self.report_file().read_text())
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def parse_pptx(data: bytes):
        """Презентація → [[рядки слайда 1], [рядки слайда 2], …] (текст з усіх написів)."""
        import io
        import zipfile
        from html import unescape
        z = zipfile.ZipFile(io.BytesIO(data))
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
        slides = []
        for n in names:
            xml = z.read(n).decode("utf-8", "ignore")
            paras = []
            for para in re.findall(r"<a:p>(.*?)</a:p>", xml, re.S):
                t = "".join(unescape(x) for x in re.findall(r"<a:t>(.*?)</a:t>", para, re.S)).strip()
                if t:
                    paras.append(t)
            slides.append(paras)
        return slides

    async def report_fetch(self):
        """Завантажити звіт (файл у Google Диску або Google Презентацію), розібрати й порівняти з попередньою версією."""
        import httpx
        fid = re.search(r"/d/([\w-]{20,})", self.REPORT_URL).group(1)
        urls = [f"https://docs.google.com/presentation/d/{fid}/export/pptx", f"https://drive.google.com/uc?export=download&id={fid}"]
        status = []
        async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 OrionAI"}) as cl:
            for u in urls:
                try:
                    r = await cl.get(u)
                    if r.status_code == 200 and r.content.startswith(b"PK"):
                        slides = self.parse_pptx(r.content)
                        old = self.report_data()
                        changed = [i + 1 for i in range(max(len(slides), len(old.get("slides", []))))
                                   if (slides[i] if i < len(slides) else None) != (old.get("slides", [])[i] if i < len(old.get("slides", [])) else None)]
                        (self.dir / "report.pptx").write_bytes(r.content)  # 💾 власна копія останньої версії
                        self.report_file().write_text(json.dumps({"slides": slides, "t": time.time(), "status": [f"{u.split('/')[2]}: ✅"]}, ensure_ascii=False))
                        return changed if old else []
                    status.append(f"{u.split('/')[2]}: HTTP {r.status_code}")
                except Exception as exc:  # noqa: BLE001
                    status.append(f"{u.split('/')[2]}: {type(exc).__name__}")
        d = self.report_data()
        d["status"] = status
        self.report_file().write_text(json.dumps(d, ensure_ascii=False))
        return []

    async def report_watch(self):
        await asyncio.sleep(120)
        while True:
            try:
                ch = await self.report_fetch()
                if ch:
                    await self.host.notify_admins(f"📊 <b>Звіт змінився</b>: слайди {', '.join(map(str, ch[:20]))}.",
                                                  [[("📊 Звіт", self.cb("rep"))]])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1800)

    def report_screen(self, chat, slide=None):
        d = self.report_data()
        slides = d.get("slides") or []
        rows = []
        if slide is not None and str(slide).isdigit() and 0 <= int(slide) < len(slides):
            k = int(slide)
            text = f"📊 <b>Звіт · слайд {k + 1} з {len(slides)}</b>\n\n" + ("\n".join(esc(x) for x in slides[k]) or "<i>На слайді немає тексту.</i>")
            rows.append(([("◀️", self.cb("rep", k - 1))] if k else []) + ([("▶️", self.cb("rep", k + 1))] if k + 1 < len(slides) else []))
            rows.append([("📊 Усі слайди", self.cb("rep"))])
        else:
            t = d.get("t")
            text = ("📊 <b>Звіт</b>\n\nБот лише читає звіт і щопівгодини перевіряє, чи він змінився (копія останньої версії зберігається в боті). "
                    "Нічого в нього не вносить — правки лише людьми.\n\n"
                    + (f"Слайдів: {len(slides)} · оновлено {dt.datetime.fromtimestamp(t, self.tz()).strftime('%d.%m %H:%M')}" if t else "Ще не завантажено.")
                    + ("\n" + "\n".join(esc(x) for x in d.get("status", [])) if d.get("status") and not slides else ""))
            btns = [(f"{k + 1}. {(sl[0] if sl else '—')[:26]}", self.cb("rep", k)) for k, sl in enumerate(slides[:40])]
            rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
            rows.append([("🔄 Оновити звіт", self.cb("repr"))])
        rows.append([("📄 Відкрити оригінал", "url:" + self.REPORT_URL)])
        rows.append([("« Редакція", self.cb())])
        from footnotes import html_cut
        return html_cut(text, 4000), [r for r in rows if r]

    # ---------------------------------------------------------------- маршрути
    async def route(self, update, ctx, chat, args):
        a = args[0] if args else ""
        x = args[1] if len(args) > 1 else None
        y = args[2] if len(args) > 2 else None
        if a == "l":
            return None, self.list_screen(chat, x or "task", y or 0, args[3] if len(args) > 3 else None)
        if a == "me":
            return None, self.me_screen(chat)
        if a == "mef" and x in UDEF and isinstance(UDEF[x], bool):
            self.set_up(chat, **{x: not self.up(chat)[x]})
            return "Збережено", self.me_screen(chat)
        if a == "men" and x in NEW_MODES:
            self.set_up(chat, notify_new=x)
            return "📥 " + NEW_MODES[x], self.me_screen(chat)
        if a == "met":
            self.ask(chat, "remind", None)
            return None, ("⏰ О котрій нагадувати про ваші редакційні завдання? Напишіть час, напр. <code>09:30</code>, "
                          "або <code>0</code>, щоб вимкнути.", [[("✖️ Скасувати", self.cb("me"))]])
        if a == "i":
            return None, self.item_screen(chat, x)
        if a == "who":
            return None, self.who_screen(chat, x)
        if a == "set":
            name = "" if y == "-" else (self.people()[int(y)] if y and y.isdigit() and int(y) < len(self.people()) else "")
            msg = self.set_who(x, name)
            if name and not msg.startswith("⚠️"):
                await self.notify_assigned(x, name, by=chat)
            return msg, self.item_screen(chat, x)
        if a == "whoask":
            self.ask(chat, "who", int(x))
            return None, ("✏️ Напишіть ім'я відповідального.", [[("« Назад", self.cb("i", x))]])
        if a == "st":
            self.set_status(x, y)
            if y == "review":  # 👀 на перевірку — повідомляємо адмінів редакції
                with closing(self.db()) as con:
                    r = con.execute("SELECT * FROM items WHERE id=?", (int(x),)).fetchone()
                if r:
                    await self.host.notify_admins(f"👀 <b>На перевірці</b>: {esc(self.task_line(dict(r)))}" + (f" · 👤 {esc(r['who'])}" if r["who"] else ""),
                                                  [[("Відкрити", self.cb("i", x))]])
            return STATUS.get(y, ""), self.item_screen(chat, x)
        if a == "nt":
            with closing(self.db()) as con, con:
                r = con.execute("SELECT * FROM items WHERE id=?", (int(x),)).fetchone()
                if r:
                    nets = self.nets_of(dict(r))
                    nets[y] = NET_STEP.get(nets.get(y, "new"), "work")
                    con.execute("UPDATE items SET nets=? WHERE id=?", (json.dumps(nets), int(x)))
                    if all(v == "done" for v in nets.values()):
                        con.execute("UPDATE items SET status='done', done=1 WHERE id=?", (int(x),))
                    elif r["done"]:
                        con.execute("UPDATE items SET done=0, status='work' WHERE id=?", (int(x),))
            return None, self.item_screen(chat, x)
        if a == "dl":
            self.ask(chat, "dl", int(x))
            return None, ("📅 <b>Дедлайн</b>: напишіть дату — <code>30.11</code>, <code>завтра</code>, <code>+3</code> (днів). «-» — без дедлайну.",
                          [[("« Назад", self.cb("i", x))]])
        if a == "wn":
            news = self.news_plugin()
            with closing(self.db()) as con:
                r = con.execute("SELECT * FROM items WHERE id=?", (int(x),)).fetchone()
            if not news or not r:
                return "Новинарня недоступна", self.item_screen(chat, x)
            topic = r["text"] + (f"\n\nКонтекст: {r['reason']}" if r["reason"] and r["reason"] != r["text"] else "")
            iid, state, _orig = news.store.add_item("editorial", topic, source_name="завдання редакції", requested_by=chat)
            news._enqueue(iid)
            with closing(self.db()) as con, con:
                con.execute("UPDATE items SET news=?, status=CASE WHEN status IN ('', 'new') THEN 'work' ELSE status END WHERE id=?", (iid, int(x)))
            return "📰 Пишу новину", self.item_screen(chat, x, "📰 Новинарня пише новину на цю тему — чернетка прийде окремим повідомленням.")
        if a == "done":
            with closing(self.db()) as con, con:
                con.execute("UPDATE items SET done=1-done, status=CASE WHEN done=0 THEN 'done' ELSE 'work' END WHERE id=?", (int(x),))
            return None, self.item_screen(chat, x)
        if a == "hide":
            with closing(self.db()) as con, con:
                r = con.execute("SELECT kind FROM items WHERE id=?", (int(x),)).fetchone()
                con.execute("UPDATE items SET hidden=1 WHERE id=?", (int(x),))
            return "Прибрано", self.list_screen(chat, r["kind"] if r else "task")
        if a == "ed":
            self.ask(chat, "ed", int(x))
            return None, ("✏️ Надішліть новий текст пункту.", [[("« Назад", self.cb("i", x))]])
        if a == "kd":
            order = list(KINDS)
            with closing(self.db()) as con, con:
                r = con.execute("SELECT kind FROM items WHERE id=?", (int(x),)).fetchone()
                if r:
                    con.execute("UPDATE items SET kind=? WHERE id=?", (order[(order.index(r["kind"]) + 1) % len(order)], int(x)))
            return None, self.item_screen(chat, x)
        if a == "ch":
            return None, self.chats_screen(chat)
        if a in ("chon", "choff"):
            with closing(self.db()) as con, con:
                con.execute("INSERT OR IGNORE INTO chats(id,title,username,on_,seen) VALUES(?,?,?,0,?)", (int(x), "", "", time.time()))
                con.execute("UPDATE chats SET on_=? WHERE id=?", (1 if a == "chon" else 0, int(x)))
            return ("✅ Збираю з цього чату (у нього нічого не пишу)" if a == "chon" else "Більше не збираю"), self.chats_screen(chat)
        if a == "pp":
            return None, self.people_screen(chat)
        if a == "ppadd":
            self.ask(chat, "pp")
            return None, ("➕ Напишіть ім'я (можна кілька через кому).", [[("« Люди", self.cb("pp"))]])
        if a == "ppdel":
            ppl = self.people()
            if x and x.isdigit() and int(x) < len(ppl):
                with closing(self.db()) as con, con:
                    con.execute("INSERT INTO people(name,src,added) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET src='removed'",
                                (ppl[int(x)], "removed", time.time()))
            return None, self.people_screen(chat)
        if a == "pc":
            return None, self.person_screen(chat, x)
        if a in ("pme", "pnick", "punl"):
            ppl = self.people()
            if not (x and x.isdigit() and int(x) < len(ppl)):
                return None, self.people_screen(chat)
            n = ppl[int(x)]
            if a == "pme":
                return ("🔗 Прив'язано до вас" if self.add_account(n, uid=int(chat)) else "Уже прив'язано"), self.person_screen(chat, x)
            if a == "punl":
                with closing(self.db()) as con, con:
                    con.execute("DELETE FROM accounts WHERE id=? AND name=?", (int(y or 0), n))
                return "Відв'язано", self.person_screen(chat, x)
            self.ask(chat, "nick", int(x))
            return None, (f"✏️ Надішліть @нікнейм людини в Telegram для «{esc(n)}».", [[("« Назад", self.cb("pc", x))]])
        if a == "po":
            self._pd = getattr(self, "_pd", {})
            self._pd.pop(chat, None)
            return None, self.posts_screen(chat)
        if a == "ponew":
            if not self.host.is_admin(chat):
                return "Планувати повідомлення в редакційні чати можуть адміністратори", None
            self.ask(chat, "potext")
            return None, ("📤 Напишіть текст повідомлення для редакційних чатів.", [[("✖️ Скасувати", self.cb("po"))]])
        if a == "pochat":
            d = getattr(self, "_pd", {}).setdefault(chat, {"chats": []})
            cid = int(x)
            d["chats"] = [c for c in d["chats"] if c != cid] if cid in d["chats"] else d["chats"] + [cid]
            return None, self.draft_screen(chat)
        if a == "powhen":
            d = getattr(self, "_pd", {}).get(chat)
            if not d or not d.get("chats"):
                return "Спершу оберіть хоча б один чат", self.draft_screen(chat) if d else self.posts_screen(chat)
            d["mode"] = x
            if x == "now":
                return None, self.confirm_screen(chat)
            self.ask(chat, "powhen", x)
            return None, (("🕐 Коли надіслати? Напр. «21.11 18:00»." if x == "once" else "🔁 О котрій щодня? Напр. «09:00»."),
                          [[("✖️ Скасувати", self.cb("po"))]])
        if a == "pook":
            d = getattr(self, "_pd", {}).pop(chat, None)
            if not d:
                return None, self.posts_screen(chat)
            with closing(self.db()) as con, con:
                pid = con.execute("INSERT INTO posts(chats,text,mode,at,hm,on_,by,created) VALUES(?,?,?,?,?,1,?,?)",
                                  (json.dumps(d["chats"]), d["text"], d["mode"], d.get("at"), d.get("hm"), chat, time.time())).lastrowid
            if d["mode"] == "now":
                with closing(self.db()) as con:
                    p = dict(con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())
                await self.send_post(p)
                with closing(self.db()) as con, con:
                    con.execute("UPDATE posts SET last=?, on_=0 WHERE id=?", (time.time(), pid))
                return "📤 Надіслано", self.posts_screen(chat)
            return "✅ Заплановано", self.posts_screen(chat)
        if a in ("poon", "podel"):
            with closing(self.db()) as con, con:
                if a == "poon":
                    con.execute("UPDATE posts SET on_=1-on_ WHERE id=?", (int(x),))
                else:
                    con.execute("DELETE FROM posts WHERE id=?", (int(x),))
            return None, self.posts_screen(chat)
        if a == "rep":
            return None, self.report_screen(chat, x)
        if a == "repr":
            ch = await self.report_fetch()
            return (f"Змінено слайди: {', '.join(map(str, ch))}" if ch else "Оновлено"), self.report_screen(chat)
        return None, self.home(chat)

    async def on_text(self, update, ctx, chat, key, data, text):
        t = text.strip()
        if key == "remind":
            m = re.search(r"(\d{1,2})[:.](\d{2})", t)
            if t in ("0", "-", "ні", "вимкнути"):
                self.set_up(chat, remind="")
                return "⏰ Нагадування вимкнено", self.me_screen(chat)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                self.ask(chat, "remind", None)
                return "Потрібен час, напр. 09:30", None
            self.set_up(chat, remind=f"{int(m.group(1)):02d}:{m.group(2)}")
            return "⏰ Збережено", self.me_screen(chat)
        if key == "who":
            msg = self.set_who(data, t[:60])
            if not msg.startswith("⚠️"):
                await self.notify_assigned(data, t[:60], by=chat)
            return msg, self.item_screen(chat, data)
        if key == "dl":
            day = parse_day(t, dt.date.today())
            if day is False:
                return "Не зрозумів дату", self.item_screen(chat, data)
            with closing(self.db()) as con, con:
                con.execute("UPDATE items SET day=? WHERE id=?", (day or "", int(data)))
            return "📅 Збережено", self.item_screen(chat, data)
        if key == "ed":
            with closing(self.db()) as con, con:
                con.execute("UPDATE items SET text=? WHERE id=?", (t[:2000], int(data)))
            return "Збережено", self.item_screen(chat, data)
        if key == "potext":
            self._pd = getattr(self, "_pd", {})
            self._pd[chat] = {"text": t[:4000], "chats": []}
            return None, self.draft_screen(chat)
        if key == "powhen":
            d = getattr(self, "_pd", {}).get(chat)
            if not d:
                return None, self.posts_screen(chat)
            if data == "daily":
                m = re.search(r"(\d{1,2})[:.](\d{2})", t)
                if not m or int(m.group(1)) > 23:
                    self.ask(chat, "powhen", data)
                    return "Потрібен час, напр. 09:00", None
                d["hm"] = f"{int(m.group(1)):02d}:{m.group(2)}"
            else:
                m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s+(\d{1,2})[:.](\d{2})", t)
                if not m:
                    self.ask(chat, "powhen", data)
                    return "Потрібні дата й час, напр. 21.11 18:00", None
                now = dt.datetime.now(self.tz())
                y = int(m.group(3)) if m.group(3) else now.year
                y = y + 2000 if y < 100 else y
                try:
                    at = dt.datetime(y, int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5)), tzinfo=self.tz())
                except ValueError:
                    self.ask(chat, "powhen", data)
                    return "Такої дати немає", None
                d["at"] = at.timestamp()
            return None, self.confirm_screen(chat)
        if key == "nick":
            ppl = self.people()
            m = re.search(r"@?([A-Za-z][\w]{3,31})", t)
            if m and data is not None and int(data) < len(ppl):
                ok = self.add_account(ppl[int(data)], username=m.group(1))
                return ("🔗 Прив'язано" if ok else "Уже прив'язано"), self.person_screen(chat, data)
            return "Це не схоже на нікнейм Telegram", self.person_screen(chat, data)
        if key == "pp":
            with closing(self.db()) as con, con:
                for n in [x.strip() for x in t.split(",") if x.strip()][:20]:
                    con.execute("INSERT INTO people(name,src,added) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET src='manual'", (n[:60], "manual", time.time()))
            return "Додано", self.people_screen(chat)
        return None, self.home(chat)
