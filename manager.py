"""🧭 Головний бот: керує всіма вашими ботами (Telegram, WhatsApp, Instagram та будь-якими іншими через HTTP).

Що вміє:
- реєстр ботів: додати за токеном, переглянути стан, змінити назву, опис і команди, написати від імені бота;
- моніторинг: перевіряє ботів за розкладом і пише, коли бот упав, гальмує або знову працює;
- 📥 скринька: будь-який ваш бот (або скрипт, сайт, сервіс) надсилає сюди текст через HTTP,
  Telegram-боти — ще й напряму (режим bot-to-bot), а ви — пересиланням вручну;
- 🔀 пересилання між ботами: правила «від кого → кому», вручну одним натисканням, із захистом від зациклень;
- той самий конструктор меню (групи, перейменування, «Назад/Додому/Вперед»), резервні копії у хмари.

Запуск: python manager.py (змінні — у .env.example, розділ «Головний бот»).
"""

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import backups as bkui
import platforms as P
from hubclient import WEEKDAYS, plan_state, plan_text, schedule_due, schedule_text
from backups import CLOUDS, TARGET_TITLES, BackupError, Backups, human_size
from uilayout import Layout, handle_editor, handle_editor_text
from community import Community
from priority import Ranks
from cluster import MODES as RUN_MODES, WEEK as RUN_WEEK, Node, cluster_route, cluster_screen

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("manager")


def env(name, default=""):
    return os.environ.get(name, default).strip()


TOKEN = env("MANAGER_TOKEN")
SECRET = env("MANAGER_SECRET")
ALLOWED = [int(x) for x in env("ALLOWED_USERS").replace(" ", "").split(",") if x]
DB_PATH = env("MANAGER_DB_PATH", "data/manager.db")
PUBLIC_URL = env("MANAGER_PUBLIC_URL", env("PUBLIC_URL")).rstrip("/")
PORT = int(env("PORT", "8080"))
TZ = ZoneInfo(env("BOT_TZ", "Europe/Kyiv"))
CHECK_EVERY = int(env("MONITOR_EVERY", "300"))      # як часто перевіряти ботів, с
HB_TIMEOUT = int(env("HEARTBEAT_TIMEOUT", "900"))   # «мовчить» довше — вважаємо, що впав
MAX_HOP = int(env("RELAY_MAX_HOPS", "3"))           # скільки разів одне повідомлення можна передати далі
RATE_PER_MIN = int(env("RELAY_RATE_PER_MIN", "30"))  # більше від одного бота за хвилину — пауза
TRASH_DAYS = 30
NO_PREVIEW = {"is_disabled": True}


def esc(s):
    return html.escape(str(s or ""), quote=False)


def short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def ts(t, fmt="%d.%m %H:%M"):
    return datetime.fromtimestamp(t, TZ).strftime(fmt) if t else "—"


def pairs(items, cols=2):
    return [items[i : i + cols] for i in range(0, len(items), cols)]


def seg(options, current):
    return [(("● " if v == current else "") + label, v) for v, label in options]


def owner_id():
    return ALLOWED[0] if ALLOWED else 0


# ---------------------------------------------------------------- база


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    _init_tables()
    with closing(db()) as con, con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(bots)")]
        if "sched" not in cols:
            con.execute("ALTER TABLE bots ADD COLUMN sched TEXT")
        for col in ("sleep_until REAL", "sleep_note TEXT", "plan TEXT", "held TEXT"):  # 😴 бот попередив, що спить
            if col.split()[0] not in cols:
                con.execute(f"ALTER TABLE bots ADD COLUMN {col}")


def _init_tables():
    with closing(db()) as con, con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL, username TEXT, ext_id TEXT,
                secret TEXT, inbox_key TEXT NOT NULL, cmd_url TEXT, via_tg INTEGER DEFAULT 0, monitor INTEGER DEFAULT 1,
                alerts TEXT DEFAULT 'all', status TEXT DEFAULT 'unknown', status_detail TEXT, status_since REAL,
                last_seen REAL, last_check REAL, created REAL NOT NULL, state TEXT DEFAULT 'active', trashed_at REAL,
                pinned_at REAL, note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, type TEXT NOT NULL, text TEXT NOT NULL,
                source TEXT NOT NULL, created REAL NOT NULL, read INTEGER DEFAULT 0, pinned_at REAL, hop INTEGER DEFAULT 0,
                hash TEXT, state TEXT DEFAULT 'active', trashed_at REAL
            );
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT, from_bot INTEGER, type TEXT, contains TEXT, to_bot INTEGER NOT NULL,
                enabled INTEGER DEFAULT 1, created REAL NOT NULL, hits INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER, to_bot INTEGER, ok INTEGER, detail TEXT, created REAL
            );
            """
        )


def kv_get(key):
    with closing(db()) as con:
        r = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def kv_set(key, value):
    with closing(db()) as con, con:
        con.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


PREF_DEFAULTS = {"bots_sort": "status", "inbox_filter": "all", "inbox_sort": "new"}


def prefs(chat):
    p = dict(PREF_DEFAULTS)
    raw = kv_get(f"prefs:{chat}")
    if raw:
        p.update(json.loads(raw))
    return p


def set_pref(chat, key, value):
    p = prefs(chat)
    if value is None:
        p.pop(key, None)
    else:
        p[key] = value
    kv_set(f"prefs:{chat}", json.dumps(p, ensure_ascii=False))


# ---------------------------------------------------------------- шифрування токенів
# Токени ботів у базі зашифровані ключем з MANAGER_SECRET: копія бази без нього марна.


def _key():
    return hashlib.sha256((SECRET + ":tokens").encode()).digest()


def seal(obj):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(_key()).encrypt(nonce, json.dumps(obj).encode(), None)).decode()


def unseal(blob):
    if not blob:
        return {}
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(blob)
    try:
        return json.loads(AESGCM(_key()).decrypt(raw[:12], raw[12:], None))
    except Exception:  # noqa: BLE001
        log.error("не вдалося розшифрувати токен — MANAGER_SECRET змінився?")
        return {}


# ---------------------------------------------------------------- боти


def bot_add(kind, name, username=None, ext_id=None, secret=None, cmd_url=None):
    key = secrets.token_urlsafe(24)
    with closing(db()) as con, con:
        cur = con.execute(
            "INSERT INTO bots (kind, name, username, ext_id, secret, inbox_key, cmd_url, created) VALUES (?,?,?,?,?,?,?,?)",
            (kind, name, username, str(ext_id) if ext_id else None, seal(secret) if secret else None, key, cmd_url, time.time()),
        )
        return cur.lastrowid


def bot_get(bid):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone()
    return dict(r) if r else None


def bot_by_key(key):
    if not key:
        return None
    with closing(db()) as con:
        r = con.execute("SELECT * FROM bots WHERE inbox_key=? AND state='active'", (key,)).fetchone()
    return dict(r) if r else None


def bot_by_tg(user_id=None, username=None):
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM bots WHERE kind='telegram' AND state='active'").fetchall()
    for r in rows:
        if (user_id and r["ext_id"] == str(user_id)) or (username and (r["username"] or "").lower() == username.lower()):
            return dict(r)
    return None


def bots_list(state="active"):
    with closing(db()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM bots WHERE state=? ORDER BY id", (state,))]


def bot_update(bid, **f):
    with closing(db()) as con, con:
        con.execute(f"UPDATE bots SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), bid))


def secrets_of(b):
    return unseal(b.get("secret"))


STATUS_ICON = {"up": "🟢", "warn": "🟡", "down": "🔴", "unknown": "⚪️", "paused": "⏸", "sleep": "😴"}


def bplan(b):
    try:
        return json.loads(b.get("plan") or "null")
    except ValueError:
        return None


def sleeping(b):
    """Чи спить бот зараз — за його планом сну (обмін лише при змінах) або разовим попередженням.
    None — не спить; 0 — спить, доки не покличуть; інакше — спить до цього часу (unix)."""
    p = bplan(b)
    if p and p.get("mode") not in (None, "always"):
        st, until = plan_state(p, last_seen=b.get("last_seen"))
        return None if st == "awake" else (until or 0)
    if b.get("sleep_until") is None:
        return None
    if b["sleep_until"] and time.time() > b["sleep_until"] + HB_TIMEOUT:  # обіцяв прокинутись і не прокинувся
        return None
    return b["sleep_until"]


def sleep_text(b):
    u = sleeping(b)
    if u is None:
        return ""
    return "😴 спить" + (f" до {datetime.fromtimestamp(u, TZ).strftime('%d.%m %H:%M')}" if u else " до свого наступного сигналу")


def bot_icon(b):
    if b["monitor"] and sleeping(b) is not None:
        return STATUS_ICON["sleep"]
    return STATUS_ICON["paused"] if not b["monitor"] else STATUS_ICON.get(b["status"], "⚪️")


def bot_label(b):
    return f"{P.KINDS[b['kind']][0]} {b['name']}" + (f" @{b['username']}" if b.get("username") and b["kind"] == "telegram" else "")


# ---------------------------------------------------------------- події (скринька)

TYPES = {"sleep": "😴 Сон", "alert": "🚨 Тривога", "info": "ℹ️ Інформація", "report": "📊 Звіт", "command": "⚙️ Команда", "reply": "↩️ Відповідь"}
SOURCES = {"api": "через HTTP", "telegram": "напряму в Telegram", "forward": "переслано вами", "monitor": "моніторинг", "manager": "з головного бота"}
RATE = {}       # bot_id -> [час повідомлень за хвилину]
MUTED = {}      # bot_id -> до коли пауза через флуд


def event_add(bot_id, type_, text, source, hop=0):
    """Зберігає подію. Повертає (id, причина відмови) — причина є, якщо спрацював захист від флуду чи повтору."""
    text = str(text or "").strip()[:4000]
    if not text:
        return None, "порожній текст"
    now_ = time.time()
    auto = source in ("api", "telegram")  # захист від флуду — лише для того, що бот надсилає сам;
    # сигнали моніторингу й ваші ручні пересилання проходять завжди
    if bot_id and auto and MUTED.get(bot_id, 0) > now_:
        return None, "бот на паузі через надто часті повідомлення"
    if bot_id and auto:
        win = [t for t in RATE.get(bot_id, []) if now_ - t < 60] + [now_]
        RATE[bot_id] = win
        if len(win) > RATE_PER_MIN:
            MUTED[bot_id] = now_ + 300
            return None, f"понад {RATE_PER_MIN} повідомлень за хвилину — пауза 5 хв"
    h = hashlib.sha1(f"{bot_id}:{type_}:{text}".encode()).hexdigest()
    with closing(db()) as con, con:
        dup = con.execute("SELECT id FROM events WHERE hash=? AND created>?", (h, now_ - 120)).fetchone()
        if dup:
            return None, "такий самий текст уже надходив менш ніж 2 хв тому"
        cur = con.execute("INSERT INTO events (bot_id, type, text, source, created, hop, hash) VALUES (?,?,?,?,?,?,?)",
                          (bot_id, type_ if type_ in TYPES else "info", text, source, now_, hop, h))
        return cur.lastrowid, None


def event_get(eid):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    return dict(r) if r else None


def events_list(filt="all", bot_id=None, sort="new", state="active"):
    q, args = "SELECT * FROM events WHERE state=?", [state]
    if bot_id:
        q += " AND bot_id=?"
        args.append(bot_id)
    if filt == "unread":
        q += " AND read=0"
    elif filt == "alert":
        q += " AND type='alert'"
    elif filt == "pin":
        q += " AND pinned_at IS NOT NULL"
    q += " ORDER BY " + {"old": "id", "bot": "bot_id, id DESC", "type": "type, id DESC"}.get(sort, "id DESC")
    with closing(db()) as con:
        return [dict(r) for r in con.execute(q, args)]


def event_update(eid, **f):
    with closing(db()) as con, con:
        con.execute(f"UPDATE events SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), eid))


def unread_count():
    with closing(db()) as con:
        return con.execute("SELECT COUNT(*) FROM events WHERE read=0 AND state='active'").fetchone()[0]


# ---------------------------------------------------------------- доставка ботам


async def deliver(event, to_bot, replies=None):
    """Передає подію іншому боту: через його адресу команд (HTTP) або напряму в Telegram (bot-to-bot)."""
    if event["hop"] >= MAX_HOP:
        return False, f"повідомлення вже пройшло {event['hop']} ботів — далі не передаю, щоб не зациклитись"
    src = bot_get(event["bot_id"]) if event["bot_id"] else None
    if src and src["id"] == to_bot["id"]:
        return False, "не пересилаю бота самому собі"
    if sleeping(to_bot) is not None and event["type"] != "alert" and not event.get("_force"):
        held = json.loads(to_bot.get("held") or "[]")
        if event["id"] not in held:
            held.append(event["id"])
            bot_update(to_bot["id"], held=json.dumps(held[-100:]))
        return True, "⏳ бот спить за своїм планом — передам, щойно прокинеться (тривоги будять одразу)"
    payload = {"key": to_bot["inbox_key"], "text": event["text"], "type": event["type"], "event_id": event["id"],
               "hop": event["hop"] + 1, "from": src["name"] if src else "головний бот",
               "prio": RK.score("bots", src["id"]) if src else 0,  # 🏆 наскільки важливий той, хто питає
               **({"from_link": bot_link(src)} if src and bot_link(src) else {})}
    ok, detail = False, "у бота не вказано, як приймати повідомлення (адреса команд або bot-to-bot)"
    if to_bot.get("cmd_url"):
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(to_bot["cmd_url"], json=payload)
            ok = r.status_code == 200
            detail = "" if ok else f"бот відповів {r.status_code}"
            if ok:
                try:
                    rj = r.json()
                    reply = rj.get("reply")
                except ValueError:
                    rj, reply = {}, None
                if rj.get("queued"):  # бот зайнятий важливішим: це не відмова — чекаємо, не повторюємо й не скасовуємо
                    detail = "⏸ у черзі в бота (виконує пріоритетніше) — відповідь прийде пізніше"
                    reply = reply and f"⏸ {reply}"
                if reply:
                    eid, _ = event_add(to_bot["id"], "reply", reply, "api", hop=event["hop"] + 1)
                    if eid and replies is not None:
                        replies.append(eid)  # покажемо після самої події
                    elif eid:
                        await notify_event(event_get(eid))
        except httpx.HTTPError as e:
            detail = f"немає зв'язку: {e.__class__.__name__}"
    elif to_bot["kind"] == "telegram" and to_bot["via_tg"] and to_bot.get("ext_id") and app:
        text = f"[hop:{event['hop'] + 1}] {payload['from']}: {event['text']}"
        try:
            await app.bot.send_message(int(to_bot["ext_id"]), text[:4096])
            ok, detail = True, ""
        except Exception as e:  # noqa: BLE001
            detail = f"Telegram не доставив: {e} (у обох ботів має бути ввімкнено Bot-to-Bot Communication у @BotFather)"
    with closing(db()) as con, con:
        con.execute("INSERT INTO deliveries (event_id, to_bot, ok, detail, created) VALUES (?,?,?,?,?)", (event["id"], to_bot["id"], int(ok), detail, time.time()))
    return ok, detail


def rules_list():
    with closing(db()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM rules ORDER BY id")]


def rule_matches(r, e):
    if not r["enabled"]:
        return False
    if r["from_bot"] and r["from_bot"] != e["bot_id"]:
        return False
    if r["type"] and r["type"] != e["type"]:
        return False
    if r["contains"] and r["contains"].lower() not in e["text"].lower():
        return False
    return r["to_bot"] != e["bot_id"]


async def apply_rules(event, replies=None):
    out = []
    for r in sorted(rules_list(), key=lambda r: -RK.score("bots", r["to_bot"])):  # 🏆 важливішим ботам — першими
        if rule_matches(r, event):
            to = bot_get(r["to_bot"])
            if not to or to["state"] != "active":
                continue
            ok, detail = await deliver(event, to, replies)
            with closing(db()) as con, con:
                con.execute("UPDATE rules SET hits=hits+1 WHERE id=?", (r["id"],))
            out.append((to, ok, detail))
    return out


async def notify_event(e, delivered=None):
    """Показує подію власнику з урахуванням налаштування сповіщень бота."""
    b = bot_get(e["bot_id"]) if e["bot_id"] else None
    level = b["alerts"] if b else "all"
    if level == "off" or (level == "alerts" and e["type"] != "alert"):
        return
    who = bot_label(b) if b else "Невідоме джерело"
    lines = [f"<b>{TYPES.get(e['type'], 'ℹ️')}</b> · {esc(who)}", "", esc(e["text"][:3500])]
    for to, ok, detail in delivered or []:
        lines.append(f"{'✅' if ok else '⚠️'} → {esc(to['name'])}" + ("" if ok else f": {esc(detail)}"))
    rows = [[("↪️ Переслати боту", f"m:efw:{e['id']}"), ("📌", f"m:epin:{e['id']}"), ("✅", f"m:erd:{e['id']}")]]
    try:
        await app.bot.send_message(owner_id(), "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb(rows), link_preview_options=NO_PREVIEW)
    except Exception as e2:  # noqa: BLE001
        log.warning("не вдалося сповістити: %s", e2)


async def ingest(bot_row, type_, text, source, hop=0, until=None):
    """Повний шлях події: зберегти → правила → сповістити. type «sleep» — бот попереджає, що спатиме: без сповіщення й тривог."""
    if bot_row and type_ == "plan":  # план сну бота: зберігаємо мовчки, без сповіщень
        return None, None
    if bot_row and type_ == "sleep":
        u = int(until or 0)
        bot_update(bot_row["id"], sleep_until=u, sleep_note=text[:200], last_seen=time.time())
        event_add(bot_row["id"], "sleep", text + (f" (до {datetime.fromtimestamp(u, TZ).strftime('%d.%m %H:%M')})" if u else " (до наступного сигналу)"), source, hop)
        return None, None
    if bot_row and bot_row.get("sleep_until") is not None:
        bot_update(bot_row["id"], sleep_until=None, sleep_note=None)  # подав голос — отже, прокинувся
    eid, why = event_add(bot_row["id"] if bot_row else None, type_, text, source, hop)
    if not eid:
        if why and "пауза" in why and bot_row:
            await notify_text(f"🛑 {esc(bot_row['name'])}: {why}. Перевірте, чи бот не зациклився.")
        return None, why
    if bot_row:
        bot_update(bot_row["id"], last_seen=time.time())
    e = event_get(eid)
    replies = []
    delivered = await apply_rules(e, replies)
    await notify_event(e, delivered)
    for r in replies:
        await notify_event(event_get(r))
    return eid, None


async def notify_text(text, rows=None):
    if not app or not ALLOWED:
        return
    try:
        await app.bot.send_message(owner_id(), text, parse_mode=ParseMode.HTML, reply_markup=kb(rows) if rows else None, link_preview_options=NO_PREVIEW)
    except Exception as e:  # noqa: BLE001
        log.warning("сповіщення не надіслано: %s", e)


# ---------------------------------------------------------------- моніторинг


async def ping_bot(b):
    """«Ти живий?» на адресу команд бота. Відповідь містить його план сну — оновлюємо."""
    if not b.get("cmd_url"):
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(b["cmd_url"], json={"key": b["inbox_key"], "type": "ping", "text": "ping", "from": "головний бот", "plan": manager_plan()})
        if r.status_code != 200:
            return False
        p = (r.json() or {}).get("plan")
        if isinstance(p, dict):
            bot_update(b["id"], plan=json.dumps(p, ensure_ascii=False))
        bot_update(b["id"], last_seen=time.time())
        return True
    except (httpx.HTTPError, ValueError):
        return False


async def check_bot(b, announce=True, wake=False):
    if b["monitor"] and sleeping(b) is not None and not wake:  # спить за своїм планом — не будимо й не тривожимось
        bot_update(b["id"], last_check=time.time())
        return b["status"], sleep_text(b)
    p = bplan(b)
    if (wake or (p and p.get("mode") not in (None, "always"))) and b.get("cmd_url"):  # бот не шле «я живий» — питаємо самі
        res = await ping_bot(b)
        if res is not None:
            st, detail = ("up", "") if res else ("down", "не відповідає на перевірку")
            changed = st != b["status"]
            bot_update(b["id"], status=st, status_detail=detail, last_check=time.time(), **({"status_since": time.time()} if changed else {}))
            if announce and changed and st == "down":
                await ingest(b, "alert", f"🔴 Бот не працює: {detail} (мав не спати за своїм планом)", "monitor")
            elif announce and changed and st == "up" and b["status"] == "down":
                await ingest(b, "info", "🟢 Знову працює", "monitor")
            return st, detail
    if b["kind"] == "http" or not b["secret"]:
        if not b["monitor"]:
            return b["status"], ""
        if b["last_seen"] and time.time() - b["last_seen"] > HB_TIMEOUT:
            st, detail = "down", f"мовчить уже {int((time.time() - b['last_seen']) // 60)} хв (немає сигналу «я живий»)"
        elif b["last_seen"]:
            st, detail = "up", ""
        else:
            st, detail = "unknown", "ще не надсилав сигналу «я живий»"
    else:
        st, detail, info = await P.health(b["kind"], secrets_of(b))
        if info.get("username") and b["kind"] == "telegram" and info.get("id"):
            bot_update(b["id"], username=info["username"], ext_id=str(info["id"]))
    changed = st != b["status"]
    bot_update(b["id"], status=st, status_detail=detail, last_check=time.time(), **({"status_since": time.time()} if changed else {}))
    if announce and changed and b["monitor"] and b["status"] != "unknown" or (announce and changed and st == "down"):
        if st == "down":
            await ingest(b, "alert", f"🔴 Бот не працює: {detail}", "monitor")
        elif st == "warn":
            await ingest(b, "alert", f"🟡 Проблема: {detail}", "monitor")
        elif st == "up" and b["status"] in ("down", "warn"):
            since = time.time() - (b["status_since"] or time.time())
            await ingest(b, "info", f"🟢 Знову працює (проблема тривала {int(since // 60)} хв)", "monitor")
    return st, detail


# ---------------------------------------------------------------- ⏱ розклад перевірок
INTERVALS = [(60, "1 хв"), (300, "5 хв"), (900, "15 хв"), (1800, "30 хв"), (3600, "1 год"), (21600, "6 год")]
PAUSES = [("1h", "1 год"), ("3h", "3 год"), ("morning", "до ранку"), ("1d", "1 день"), ("1w", "тиждень")]


SCHEMES = {
    "always": ("🟢 Постійно", "кожні N хвилин, цілодобово"),
    "times": ("🗓 За розкладом", "у задані години (і дні тижня)"),
    "window": ("⏱ Таймер", "кожні N хвилин, але лише в обрані години; поза ними — пауза"),
    "manual": ("👆 За наказом", "лише коли натиснете «Перевірити зараз» або надішлете /check"),
}


def gsched():
    return json.loads(kv_get("monitor_global") or "{}") or {"every": CHECK_EVERY}


def gscheme():
    g = gsched()
    return g.get("scheme") or ("window" if g.get("window") else "always")


def global_rule():
    """Загальний розклад перевірок за обраною схемою."""
    g = gsched()
    sc = gscheme()
    if sc == "manual":
        return {"manual": True}
    if sc == "times":
        return {"times": g.get("times") or [{"t": "09:00", "d": []}]}
    out = {"every": g.get("every", CHECK_EVERY)}
    if sc == "window" and g.get("window"):
        out["window"] = g["window"]
    return out


def set_gsched(s):
    kv_set("monitor_global", json.dumps(s))


def bsched(b):
    try:
        return json.loads(b.get("sched") or "{}")
    except ValueError:
        return {}


def set_bsched(bid, s):
    bot_update(bid, sched=json.dumps(s))


def pause_ts(code):
    n = datetime.now(TZ)
    if code == "1h":
        return time.time() + 3600
    if code == "3h":
        return time.time() + 3 * 3600
    if code == "1d":
        return time.time() + 86400
    if code == "1w":
        return time.time() + 7 * 86400
    t = n.replace(hour=8, minute=0, second=0, microsecond=0)  # до ранку — найближча 08:00
    return (t if t > n else t + timedelta(days=1)).timestamp()


def effective_sched(b):
    own = bsched(b)
    s = {k: v for k, v in global_rule().items()}
    if any(k in own for k in ("every", "daily", "weekly", "times", "manual")):  # бот має власний розклад — він важливіший
        s = {k: v for k, v in s.items() if k == "window"}
    s.update(own)
    return s


def sched_line(b):
    if not b["monitor"]:
        return "⏸ стеження вимкнено"
    return schedule_text(effective_sched(b), gsched().get("every", CHECK_EVERY))


def scr_sched(chat, bid):
    b = bot_get(bid)
    s = bsched(b)
    cur = s.get("every") if not (s.get("daily") or s.get("weekly")) else None
    lines = [f"<b>⏱ Розклад перевірок: {esc(b['name'])}</b>", "", f"Зараз: <b>{esc(sched_line(b))}</b>", "",
             "Як часто головний бот перевіряє цього бота (і, якщо бот приймає команди, — як часто той робить свою роботу)."]
    rows = [[(("● " if cur == sec else "") + label, f"m:bse:{bid}:{sec}") for sec, label in INTERVALS[:3]],
            [(("● " if cur == sec else "") + label, f"m:bse:{bid}:{sec}") for sec, label in INTERVALS[3:]],
            [(("● " if s.get("daily") else "") + "📅 Раз на день", f"m:bsd:{bid}"), (("● " if s.get("weekly") else "") + "🗓 Раз на тиждень", f"m:bsw:{bid}")],
            [("🌙 Лише в певні години", f"m:bswin:{bid}")] + ([("✖️ Цілодобово", f"m:bswinx:{bid}")] if s.get("window") else [])]
    if s.get("pause_until") and s["pause_until"] > time.time():
        rows.append([("▶️ Відновити зараз", f"m:bsres:{bid}")])
    else:
        rows.append([(f"⏸ {label}", f"m:bsp:{bid}:{code}") for code, label in PAUSES[:3]])
        rows.append([(f"⏸ {label}", f"m:bsp:{bid}:{code}") for code, label in PAUSES[3:]] + [("⏸ Безстроково", f"m:bmon:{bid}")])
    if not s and b["monitor"]:
        pass
    rows.append([("↺ Як для всіх", f"m:bsx:{bid}")] + ([("📤 Передати розклад боту", f"m:bspush:{bid}")] if b["cmd_url"] else []))
    return "\n".join(lines), rows


async def push_sched(b):
    """Боти з адресою команд отримують розклад і самі виконують свою роботу за ним (hubclient: take_schedule / should_run)."""
    if not b.get("cmd_url"):
        return None
    payload = {"key": b["inbox_key"], "type": "schedule", "schedule": {**effective_sched(b), **({"paused": True} if not b["monitor"] else {})},
               "text": "розклад: " + sched_line(b), "from": "головний бот"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(b["cmd_url"], json=payload)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def monitor_tick():
    """Раз на хвилину: кого пора перевірити за його розкладом; автоматичне відновлення після паузи."""
    g = gsched()
    if g.get("pause_until") and time.time() >= g["pause_until"]:
        g.pop("pause_until")
        set_gsched(g)
        await notify_text("▶️ Моніторинг усіх ботів відновлено за таймером.")
    if g.get("pause_until"):
        return
    for b in bots_list():
        if not b["monitor"]:
            continue
        s = bsched(b)
        if s.get("pause_until") and time.time() >= s["pause_until"]:
            s.pop("pause_until")
            set_bsched(b["id"], s)
            await notify_text(f"▶️ Моніторинг «{esc(b['name'])}» відновлено за таймером.")
            b = bot_get(b["id"])
        if schedule_due(effective_sched(b), b["last_check"], tz=TZ):
            await check_bot(b)
        await flush_held(bot_get(b["id"]))


def scr_monitor(chat):
    bots = bots_list()
    g = gsched()
    paused = g.get("pause_until") and g["pause_until"] > time.time()
    sc = gscheme()
    lines = ["<b>📡 Моніторинг</b>", "",
             f"Схема: <b>{SCHEMES[sc][0]}</b> — {SCHEMES[sc][1]}.",
             ("⏸ <b>Усе на паузі</b> до " + datetime.fromtimestamp(g["pause_until"], TZ).strftime("%d.%m %H:%M") if paused else
              f"Зараз: <b>{esc(schedule_text(global_rule()))}</b>") +
             f". Бот без токена вважається впалим, якщо мовчить довше {HB_TIMEOUT // 60} хв. Бот, що попередив про сон (😴), не турбую.", ""]
    for b in sorted(bots, key=lambda b: b["name"].lower()):
        st = sleep_text(b)
        lines.append(f"{bot_icon(b)} <b>{esc(b['name'])}</b> · {esc(sched_line(b))} · перевірено {ts(b['last_check'], '%H:%M')}"
                     + (f"\n    {esc(st)}" if st else (f"\n    {esc(short(b['status_detail'], 90))}" if b["status_detail"] else "")))
    if not bots:
        lines.append("<i>Немає ботів.</i>")
    cur = g.get("every", CHECK_EVERY)
    rows = [[("🔄 Перевірити всіх зараз", "m:monrun")],
            [(("● " if k == sc else "") + SCHEMES[k][0], f"m:msc:{k}") for k in ("always", "times")],
            [(("● " if k == sc else "") + SCHEMES[k][0], f"m:msc:{k}") for k in ("window", "manual")]]
    if sc in ("always", "window"):
        rows += [[(("● " if cur == sec else "") + label, f"m:mge:{sec}") for sec, label in INTERVALS[:3]],
                 [(("● " if cur == sec else "") + label, f"m:mge:{sec}") for sec, label in INTERVALS[3:]]]
    if sc == "times":
        lines.append("\n🗓 Години перевірок: " + esc(", ".join(x["t"] + (f" ({','.join(RUN_WEEK[d] for d in x['d'])})" if x.get("d") else "") for x in global_rule()["times"])))
        rows.append([("✏️ Години перевірок", "m:mst")])
    if sc == "window":
        w = g.get("window")
        lines.append("\n⏱ Години роботи: " + (f"{w[0]}–{w[1]}" if w else "не задано (поки — цілодобово)"))
        rows.append([("✏️ Години роботи", "m:msw")])
    rows.append([("▶️ Відновити все", "m:mgr")] if paused else [(f"⏸ Усе: {label}", f"m:mgp:{code}") for code, label in PAUSES[:3]])
    rows += [[(f"⏱ {short(b['name'], 26)}", f"m:bsch:{b['id']}")] for b in bots[:10]]
    return "\n".join(lines), rows


LAST_CLUSTER = [0.0]


async def cluster_tick(force=False):
    """🛰 Резервні сервери головного бота (MANAGER_CLUSTER_NODES): хто з них працює."""
    if not NODE.enabled:
        return
    if not force and (NODE.mode(DEFAULT_MODE) != "always" or time.time() - LAST_CLUSTER[0] < 60):
        return
    LAST_CLUSTER[0] = time.time()
    try:
        await NODE.tick(on_activate, on_deactivate, notify_text)
    except Exception:  # noqa: BLE001
        log.exception("перевірка кластера")


async def on_activate():
    await apply_receiving()
    await push_all_plans(force=True)  # боти мають дізнатися нову адресу головного


async def on_deactivate():
    await apply_receiving()


async def monitor_loop():
    while True:
        await cluster_tick()
        if not NODE.active:  # у резерві: нічого не перевіряю й не надсилаю — лише копіюю базу
            await asyncio.sleep(60)
            continue
        try:
            await monitor_tick()
            await CM.tick(app.bot if app else None)
            purge_trash()
            due = BK.due_targets()
            if due:
                await bk_run(app.bot, owner_id(), auto=True, only=due)
        except Exception:  # noqa: BLE001
            log.exception("помилка моніторингу")
        await asyncio.sleep(60)  # раз на хвилину дивимось, кого пора перевірити за розкладом


def purge_trash():
    lim = time.time() - TRASH_DAYS * 86400
    with closing(db()) as con, con:
        con.execute("DELETE FROM bots WHERE state='trash' AND trashed_at<?", (lim,))
        con.execute("DELETE FROM events WHERE state='trash' AND trashed_at<?", (lim,))
        con.execute("DELETE FROM events WHERE state='active' AND pinned_at IS NULL AND created<?", (time.time() - 90 * 86400,))


# ---------------------------------------------------------------- оформлення

CM = Community(db, lambda: ALLOWED[:1], tz=TZ)
RK = Ranks(kv_get, kv_set)  # 🏆 пріоритет ботів: вище — важливіше


def bot_link(b):
    u = (b.get("username") or "").lstrip("@")
    if not u:
        return None
    if b["kind"] == "telegram":
        return f"https://t.me/{u}"
    if b["kind"] == "instagram":
        return f"https://instagram.com/{u}"
    if b["kind"] == "whatsapp":
        digits = "".join(ch for ch in u if ch.isdigit())
        return f"https://wa.me/{digits}" if digits else None
    return u if u.startswith("http") else None


def bots_ranked():
    return sorted(bots_list(), key=lambda b: -RK.score("bots", b["id"]))


def scr_bot_table(chat):
    bots = bots_ranked()
    by = {}
    for b in bots:
        by[b["kind"]] = by.get(b["kind"], 0) + 1
    lines = [f"📋 <b>Таблиця ботів</b> · усього <b>{len(bots)}</b> · " + " · ".join(f"{' '.join(P.KINDS[k])}: {n}" for k, n in by.items()), ""]
    for i, b in enumerate(bots, 1):
        sc = RK.score("bots", b["id"])
        link = bot_link(b)
        name = f'<a href="{esc(link)}">{esc(b["name"])}</a>' if link else esc(b["name"])
        how = ("HTTP" if b.get("cmd_url") else "") + (" · bot-to-bot" if b.get("via_tg") else "")
        lines.append(f"{i}. {'🔺' if sc > 0 else '🔻' if sc < 0 else ''}{bot_icon(b)} {name}" + (f" · @{esc(b['username'])}" if b.get("username") and b["kind"] != "http" else "")
                     + f" · {' '.join(P.KINDS[b['kind']])}" + (f" · {how.strip(' ·')}" if how else "") + f" · перевірено {ts(b['last_check'], '%d.%m %H:%M')}")
    if not bots:
        lines.append("Ботів ще немає.")
    else:
        lines.append("\n🏆 Порядок = пріоритет: вище — важливіше. Запити важливіших ботів пересилаю першими й передаю їхній пріоритет "
                     "отримувачу: зайнятий бот ставить менш важливе на паузу (не відхиляє), а той, хто питав, просто чекає.")
    rows = [[(f"{i}. {short(b['name'], 20)}", f"m:b:{b['id']}"), ("⬆️", f"m:btm:{b['id']}:up"), ("⬇️", f"m:btm:{b['id']}:down")] for i, b in enumerate(bots[:12], 1)]
    rows.append([("📄 Файл (Excel/CSV)", "m:btf"), ("🏘 Спільноти", "m:cm:tb")])
    return "\n".join(lines)[:4000], rows


def bot_table_csv():
    import csv
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "Пріоритет", "Назва", "Тип", "@ім'я", "Посилання", "Стан", "Стежу", "Приймає команди", "Додано", "Перевірено"])
    for i, b in enumerate(bots_ranked(), 1):
        sc = RK.score("bots", b["id"])
        w.writerow([i, "вищий" if sc > 0 else "нижчий" if sc < 0 else "звичайний", b["name"], " ".join(P.KINDS[b["kind"]]), b.get("username") or "",
                    bot_link(b) or "", b["status"], "так" if b["monitor"] else "ні", "так" if can_receive(b) else "ні",
                    ts(b["created"], "%d.%m.%Y"), ts(b["last_check"], "%d.%m.%Y %H:%M")])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")

ITEMS = {
    "bots": ("🤖 Боти", "m:bl"),
    "btable": ("📋 Таблиця ботів", "m:bt"),
    "inbox": ("📥 Вхідні", "m:in"),
    "send": ("📨 Надіслати", "m:snd"),
    "menu": ("☰ Меню", "m:home"),
    "mon": ("📡 Моніторинг", "m:mon"),
    "rules": ("🔀 Пересилання", "m:rl"),
    "pins": ("📌 Закріплені", "m:in:0:pin"),
    "trash": ("🗑 Кошик", "m:tr"),
    "api": ("🔌 Підключення", "m:api"),
    "backup": ("💾 Резервні копії", "m:bk"),
    "btns": ("🎛 Кнопки", "m:btn"),
    "diag": ("🩺 Діагностика", "m:diag"),
    "runmode": ("⚡ Режим роботи", "m:rm"),
    "comm": ("🏘 Спільноти", "m:cm"),
    "reset": ("🏭 Скидання", "m:fr"),
    "help": ("📖 Довідка", "help:main"),
}
DEFAULT_GROUPS = {"tools": "🧰 Інструменти"}
DEFAULT_LAYOUT = [["bots", "kb"], ["inbox", "kb"], ["send", "kb"], ["menu", "kb"],
                  ["mon", "home"], ["rules", "home"], ["btable", "home"], ["comm", "home"], ["pins", "home"], ["g:tools", "home"], ["help", "home"],
                  ["api", "g:tools"], ["trash", "g:tools"], ["backup", "g:tools"], ["btns", "g:tools"], ["runmode", "g:tools"], ["diag", "g:tools"], ["reset", "g:tools"]]
L = Layout(ITEMS, DEFAULT_LAYOUT, DEFAULT_GROUPS, prefs, set_pref, locked={"bots"})
AWAIT = {}
CANCEL_ROW = [("✖️ Скасувати", "m:cancel")]


def _button(t, d):
    if d.startswith("url:"):
        return InlineKeyboardButton(t, url=d[4:])
    if d.startswith("copy:"):
        from telegram import CopyTextButton

        return InlineKeyboardButton(t, copy_text=CopyTextButton(d[5:][:256]))
    return InlineKeyboardButton(t, callback_data=d[:64])


def kb(rows):
    return InlineKeyboardMarkup([[_button(t, d) for t, d in row] for row in rows])


def menu_kb(chat):
    rows = [[L.relabel(chat, t) for t in row] for row in L.reply_rows(chat)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Перешліть повідомлення бота або оберіть розділ")


async def show(update, text, rows, edit=True, nav=True):
    chat = update.effective_chat.id
    rows = L.decorate(chat, rows, nav=nav, current=L.current(chat))
    markup = kb(rows) if rows else None
    q = update.callback_query
    if q and edit:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NO_PREVIEW)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NO_PREVIEW)


def ask(chat, action, data=None):
    AWAIT[chat] = (action, data)


def inbox_url():
    return f"{PUBLIC_URL}/{SECRET}/in" if PUBLIC_URL else f"https://<адреса-бота>/{SECRET}/in"


# ---------------------------------------------------------------- екрани


def scr_home(chat):
    bots = bots_list()
    up = sum(b["status"] == "up" for b in bots)
    bad = [b for b in bots if b["status"] in ("down", "warn") and b["monitor"]]
    lines = ["<b>🧭 Головний бот</b>", "",
             f"🤖 Ботів: <b>{len(bots)}</b> · 🟢 працюють: <b>{up}</b>" + (f" · ⚠️ проблеми: <b>{len(bad)}</b>" if bad else ""),
             f"📥 Непрочитаних подій: <b>{unread_count()}</b>"]
    for b in bad[:5]:
        lines.append(f"{bot_icon(b)} {esc(b['name'])}: {esc(short(b['status_detail'], 80))}")
    unread = unread_count()
    badges = {"inbox": f" · {unread}"} if unread else {}
    return "\n".join(lines), L.place_rows(chat, "home", badges)


BOT_SORTS = [("status", "🚦 Стан"), ("az", "🔤 А–Я"), ("kind", "🗂 Тип"), ("new", "🆕 Нові")]


def scr_bots(chat):
    mode = prefs(chat).get("bots_sort", "status")
    bots = bots_list()
    order = {"down": 0, "warn": 1, "unknown": 2, "up": 3}
    key = {"az": lambda b: b["name"].lower(), "kind": lambda b: (b["kind"], b["name"].lower()), "new": lambda b: -b["created"],
           "status": lambda b: (order.get(b["status"], 2), b["name"].lower())}[mode]
    bots.sort(key=lambda b: (b["pinned_at"] is None, key(b)))
    lines = [f"<b>🤖 Ваші боти</b> · {len(bots)}", ""]
    if not bots:
        lines.append("<i>Ще немає жодного. Додайте першого — це займе хвилину.</i>")
    head = None
    for b in bots:
        if mode == "kind" and head != b["kind"]:
            head = b["kind"]
            lines.append(f"\n<b>{' '.join(P.KINDS[head])}</b>")
        lines.append(f"{bot_icon(b)} {'📌 ' if b['pinned_at'] else ''}{esc(bot_label(b))}" + (f" — {esc(short(b['status_detail'], 50))}" if b["status_detail"] else ""))
    rows = [[(label, f"m:bsort:{v}") for label, v in seg(BOT_SORTS, mode)]]
    rows += pairs([(f"{bot_icon(b)} {short(b['name'], 22)}", f"m:b:{b['id']}") for b in bots])
    rows.append([("➕ Додати бота", "m:badd"), ("🔄 Перевірити всіх", "m:monrun")])
    rows.append([("📋 Таблиця й пріоритети", "m:bt")])
    return "\n".join(lines), rows


def scr_bot(chat, bid):
    b = bot_get(bid)
    if not b or b["state"] != "active":
        return scr_bots(chat)
    icon, kname = P.KINDS[b["kind"]]
    lines = [f"<b>{icon} {esc(b['name'])}</b>" + (f" · @{esc(b['username'])}" if b["username"] and b["kind"] == "telegram" else ""),
             f"Тип: {kname}" + (f" · {esc(b['username'])}" if b["username"] and b["kind"] != "telegram" else ""),
             f"Стан: {bot_icon(b)} " + {"up": "працює", "warn": "є проблема", "down": "не працює", "unknown": "ще не перевірявся"}.get(b["status"], "?")
             + (f" — {esc(b['status_detail'])}" if b["status_detail"] else "") + (" · моніторинг вимкнено" if not b["monitor"] else ""),
             f"Перевірено: {ts(b['last_check'])} · останній сигнал: {ts(b['last_seen'])}",
             f"Сповіщення: {ALERT_LEVELS[b['alerts']]}",
             f"⏱ Перевірка: {esc(sched_line(b))}",
             f"📅 План сну: {esc(plan_text(bplan(b)))}" + (f" · {esc(sleep_text(b))}" if sleeping(b) is not None else ""),
             f"Як отримує повідомлення: " + (("HTTP ✅" if b["cmd_url"] else "") + (" · Telegram bot-to-bot ✅" if b["via_tg"] else "") or "▫️ не налаштовано")]
    if b["note"]:
        lines.append(f"📝 {esc(b['note'])}")
    n_ev = len(events_list(bot_id=bid))
    rows = []
    if b["kind"] in ("telegram", "whatsapp", "instagram") and b["secret"]:
        rows.append([("✏️ Профіль", f"m:bp:{bid}"), (P.COMMANDS_NAME[b["kind"]].split(" (")[0], f"m:bc:{bid}")])
    rows.append([("📨 Надіслати боту", f"m:bsend:{bid}"), (f"📥 Події · {n_ev}", f"m:in:0:bot{bid}")])
    rows.append([("🔌 Підключення", f"m:bconn:{bid}"), ("🔄 Перевірити", f"m:bchk:{bid}")])
    if b.get("cmd_url") or (bplan(b) or {}).get("wake_url"):
        rows.append([("⏰ Розбудити", f"m:bwake:{bid}")])
    rows.append([("⏱ Розклад і пауза", f"m:bsch:{bid}"), ("🔔 Сповіщення", f"m:balr:{bid}")] + ([] if b["monitor"] else [("▶️ Стежити", f"m:bmon:{bid}")]))
    if b["kind"] == "telegram" and b["secret"]:
        rows.append([("💬 Написати від імені бота", f"m:bas:{bid}")])
    rows.append([("📌 Відкріпити" if b["pinned_at"] else "📌 Закріпити", f"m:bpin:{bid}"), ("✏️ Назва й нотатка", f"m:bren:{bid}")])
    rows.append(([("🔑 Замінити токен", f"m:btok:{bid}")] if b["kind"] != "http" else []) + [("🗑 Видалити", f"m:bdel:{bid}")])
    return "\n".join(lines), rows


ALERT_LEVELS = {"all": "🔔 усі події", "alerts": "🚨 лише тривоги", "off": "🔕 без сповіщень (лише у «Вхідних»)"}


def scr_conn(chat, bid):
    b = bot_get(bid)
    url = inbox_url()
    curl = f"curl -X POST {url} -H 'Content-Type: application/json' -d '{{\"key\":\"{b['inbox_key']}\",\"type\":\"alert\",\"text\":\"Тест\"}}'"
    lines = [f"<b>🔌 Підключення: {esc(b['name'])}</b>", "",
             "<b>1. Бот → головний бот</b> (тривоги, звіти, будь-який текст).",
             f"Адреса скриньки:\n<code>{esc(url)}</code>", f"Ключ цього бота:\n<code>{esc(b['inbox_key'])}</code>", "",
             "Для Python-бота: покладіть поруч <code>hubclient.py</code> з архіву й задайте <code>HUB_URL</code> і <code>HUB_KEY</code>. "
             "ШІ-бот із цього архіву вже вміє: достатньо цих двох змінних.", "", f"Перевірка з терміналу:\n<code>{esc(curl)}</code>", "",
             "<b>2. Головний бот → бот</b> (команди, пересилання):",
             f"• HTTP-адреса команд: {('<code>' + esc(b['cmd_url']) + '</code>') if b['cmd_url'] else '▫️ не задано'} — бот, що має HUB_KEY, повідомляє її сам.",
             f"• Напряму в Telegram (bot-to-bot): {'✅ увімкнено' if b['via_tg'] else '▫️ вимкнено'} — потрібно ввімкнути «Bot-to-Bot Communication» у @BotFather для обох ботів."]
    rows = [[("📋 Ключ", "copy:" + b["inbox_key"]), ("📋 Адреса", "copy:" + url)],
            [("✍️ Задати адресу команд", f"m:bcmd:{bid}")] + ([("✖️ Прибрати", f"m:bcmdx:{bid}")] if b["cmd_url"] else [])]
    if b["kind"] == "telegram":
        rows.append([("✈️ Bot-to-bot: " + ("вимкнути" if b["via_tg"] else "увімкнути"), f"m:bvtg:{bid}")])
    rows.append([("🔁 Новий ключ", f"m:bkey:{bid}"), ("📨 Тест", f"m:btest:{bid}")])
    return "\n".join(lines), rows


async def scr_profile(chat, bid):
    b = bot_get(bid)
    try:
        i = await P.info(b["kind"], secrets_of(b))
    except Exception as e:  # noqa: BLE001
        return f"⚠️ Не вдалося прочитати профіль: {esc(e)}", []
    lines = [f"<b>✏️ Профіль: {esc(b['name'])}</b>", ""]
    fields = P.PROFILE_FIELDS[b["kind"]]
    for f, title in fields:
        lines.append(f"<b>{title}:</b>\n{esc(i.get(f) or '—')}\n")
    if not fields:
        lines.append("Instagram не дозволяє змінювати профіль через API. Змініть його в застосунку, а тут налаштуйте швидкі питання.")
    if b["kind"] == "telegram":
        lines.append("Аватар, вбудований режим і створення нових ботів доступні лише в @BotFather.")
    rows = [[(f"✏️ {title.split(' (')[0]}", f"m:bpe:{bid}:{f}")] for f, title in fields]
    rows.append([("🔗 Відкрити @BotFather", "url:https://t.me/BotFather")] if b["kind"] == "telegram" else [])
    return "\n".join(lines), [r for r in rows if r]


async def scr_commands(chat, bid):
    b = bot_get(bid)
    try:
        i = await P.info(b["kind"], secrets_of(b))
    except Exception as e:  # noqa: BLE001
        return f"⚠️ {esc(e)}", []
    cmds = i.get("commands") or []
    name = P.COMMANDS_NAME[b["kind"]]
    lines = [f"<b>⌨️ {name}: {esc(b['name'])}</b>", ""]
    lines += [f"/{esc(c)} — {esc(d)}" if b["kind"] != "instagram" else f"❔ {esc(d)}" for c, d in cmds] or ["<i>Порожньо.</i>"]
    rows = [[("✍️ Задати список", f"m:bce:{bid}"), ("🗑 Очистити", f"m:bcx:{bid}")]]
    return "\n".join(lines), rows


EV_FILTERS = [("all", "Усі"), ("unread", "🆕"), ("alert", "🚨"), ("pin", "📌")]
EV_SORTS = [("new", "🆕 Нові"), ("old", "📜 Старі"), ("bot", "🤖 Бот"), ("type", "🗂 Тип")]
PER_PAGE = 8


def scr_inbox(chat, page=0, filt=None):
    bot_id = None
    if filt and filt.startswith("bot"):
        bot_id, filt = int(filt[3:]), "all"
    filt = filt or prefs(chat).get("inbox_filter", "all")
    if not bot_id:
        set_pref(chat, "inbox_filter", filt)
    sort = prefs(chat).get("inbox_sort", "new")
    evs = events_list(filt, bot_id, sort)
    pages = max(1, (len(evs) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = evs[page * PER_PAGE : (page + 1) * PER_PAGE]
    names = {b["id"]: b["name"] for b in bots_list()}
    title = f"📥 Події: {esc(names.get(bot_id, '?'))}" if bot_id else "📥 Вхідні"
    lines = [f"<b>{title}</b> · {len(evs)}", ""]
    for e in chunk:
        lines.append(f"{'🆕' if not e['read'] else '▫️'}{'📌' if e['pinned_at'] else ''} {TYPES.get(e['type'], 'ℹ️')[:2]} <b>{esc(names.get(e['bot_id'], 'без бота'))}</b> · {ts(e['created'])}\n{esc(short(e['text'], 110))}")
    if not chunk:
        lines.append("<i>Порожньо.</i>")
    fkey = f"bot{bot_id}" if bot_id else None
    rows = []
    if not bot_id:
        rows.append([(label, f"m:in:0:{v}") for label, v in seg(EV_FILTERS, filt)])
    rows.append([(label, f"m:isort:{v}") for label, v in seg(EV_SORTS, sort)])
    rows += [[(f"{TYPES.get(e['type'], 'ℹ️')[:2]} {short(names.get(e['bot_id'], '—'), 12)}: {short(e['text'], 26)}", f"m:e:{e['id']}")] for e in chunk]
    if pages > 1:
        f = fkey or filt
        rows.append([("⏪", f"m:in:{page - 1}:{f}") if page else ("·", "m:noop"), (f"{page + 1}/{pages}", "m:noop"),
                     ("⏩", f"m:in:{page + 1}:{f}") if page < pages - 1 else ("·", "m:noop")])
    if chunk:
        rows.append([("✅ Усе прочитано", "m:iread")])
    return "\n".join(lines), rows


def scr_event(chat, eid):
    e = event_get(eid)
    if not e:
        return scr_inbox(chat)
    if not e["read"]:
        event_update(eid, read=1)
    b = bot_get(e["bot_id"]) if e["bot_id"] else None
    with closing(db()) as con:
        dl = con.execute("SELECT d.*, b.name FROM deliveries d LEFT JOIN bots b ON b.id=d.to_bot WHERE event_id=? ORDER BY d.id", (eid,)).fetchall()
    lines = [f"<b>{TYPES.get(e['type'], 'ℹ️')}</b> · {esc(bot_label(b) if b else 'без бота')}",
             f"{ts(e['created'], '%d.%m.%Y %H:%M')} · {SOURCES.get(e['source'], e['source'])}" + (f" · пройшло ботів: {e['hop']}" if e["hop"] else ""), "",
             esc(e["text"])]
    for d in dl:
        lines.append(f"{'✅' if d['ok'] else '⚠️'} → {esc(d['name'] or '?')} {ts(d['created'])}" + ("" if d["ok"] else f": {esc(d['detail'])}"))
    rows = [[("↪️ Переслати боту", f"m:efw:{eid}"), ("📋 Текст", "copy:" + e["text"][:256])],
            [("📌 Відкріпити" if e["pinned_at"] else "📌 Закріпити", f"m:epin:{eid}"), ("🗑 У кошик", f"m:edel:{eid}")]]
    if b:
        rows.append([(f"🤖 {short(b['name'], 20)}", f"m:b:{b['id']}")])
    return "\n".join(lines), rows


def scr_pick_bot(title, prefix, exclude=None, extra=None):
    bots = [b for b in bots_list() if b["id"] != exclude]
    rows = pairs([(f"{P.KINDS[b['kind']][0]} {short(b['name'], 22)}", f"{prefix}:{b['id']}") for b in bots])
    if extra:
        rows.insert(0, [extra])
    return f"<b>{title}</b>\n\nОберіть бота:" + ("" if bots else "\n\n<i>Спершу додайте ботів.</i>"), rows


def scr_rules(chat):
    rs = rules_list()
    names = {b["id"]: b["name"] for b in bots_list()}
    lines = ["<b>🔀 Автоматичне пересилання</b>", "",
             "Правило: <i>від кого → кому</i>, за типом або словом. Наприклад: усі 🚨 тривоги від монітора → у ШІ-бота, щоб він пояснив, що сталося.", "",
             f"🛡 Захист від зациклень: не більше {MAX_HOP} передач поспіль, повтори за 2 хв відкидаються, понад {RATE_PER_MIN} повідомлень/хв від бота — пауза 5 хв.", ""]
    for r in rs:
        lines.append(f"{'✅' if r['enabled'] else '⏸'} #{r['id']} {esc(names.get(r['from_bot'], 'будь-який бот'))}"
                     f"{' · ' + TYPES[r['type']] if r['type'] else ''}{' · «' + esc(r['contains']) + '»' if r['contains'] else ''} → <b>{esc(names.get(r['to_bot'], '?'))}</b> · спрацювало {r['hits']}")
    if not rs:
        lines.append("<i>Правил ще немає.</i>")
    rows = [[(f"{'⏸' if r['enabled'] else '▶️'} #{r['id']}", f"m:rtog:{r['id']}"), (f"🗑 #{r['id']}", f"m:rdel:{r['id']}")] for r in rs]
    rows.append([("➕ Нове правило", "m:rnew")])
    return "\n".join(lines), rows


def scr_api(chat):
    return ("<b>🔌 Як боти підключаються до головного</b>\n\n"
            f"Скринька (HTTP POST, JSON):\n<code>{esc(inbox_url())}</code>\n"
            "<code>{\"key\": \"ключ бота\", \"type\": \"alert|info|report\", \"text\": \"…\"}</code>\n\n"
            f"Сигнал «я живий»: <code>{esc(inbox_url()[:-3])}/hb</code> з <code>{{\"key\": …, \"cmd_url\": …}}</code>\n"
            "Простий варіант для будь-якого сервісу (GET):\n"
            f"<code>{esc(inbox_url())}?key=…&amp;text=…</code>\n\n"
            "Ключ кожного бота — у його картці → «🔌 Підключення».\n"
            "Вручну: просто перешліть мені повідомлення від будь-якого бота.\n"
            "Напряму в Telegram: увімкніть «Bot-to-Bot Communication» у @BotFather для обох ботів — тоді бот може писати мені сам."), [[("📖 Детально", "help:g:api")]]


def scr_trash(chat):
    bots = bots_list("trash")
    evs = events_list(state="trash")
    lines = [f"<b>🗑 Кошик</b> · видаляється через {TRASH_DAYS} днів", ""]
    lines += [f"🤖 {esc(b['name'])} · ще {max(0, TRASH_DAYS - int((time.time() - b['trashed_at']) // 86400))} дн" for b in bots]
    lines += [f"📥 {esc(short(e['text'], 60))}" for e in evs[:10]]
    if not bots and not evs:
        lines.append("<i>Порожньо.</i>")
    rows = [[(f"↩️ {short(b['name'], 24)}", f"m:brs:{b['id']}")] for b in bots]
    rows += [[(f"↩️ {short(e['text'], 24)}", f"m:ers:{e['id']}")] for e in evs[:10]]
    if bots or evs:
        rows.append([("🧹 Очистити зараз", "m:trempty")])
    return "\n".join(lines), rows


# ---------------------------------------------------------------- довідка (без ШІ)

HELP = {
    "start": ("🚀 З чого почати", "1. «🤖 Боти» → «➕ Додати бота»: Telegram — токен від @BotFather; WhatsApp — ID номера й постійний токен Meta; Instagram — токен Instagram API; «Інший» — будь-який сервіс чи скрипт.\n2. Токен я перевіряю й одразу видаляю ваше повідомлення з ним із чату. У базі токени зашифровані.\n3. У картці бота → «🔌 Підключення» — адреса скриньки й ключ: з ними бот може писати мені сам.\n4. «🔀 Пересилання» — правила, хто кому передає повідомлення."),
    "api": ("🔌 Скринька й команди", "<b>Бот → головний:</b> POST на адресу скриньки з JSON <code>{key, type, text}</code>. Типи: alert (тривога — завжди зі сповіщенням), info, report.\n<b>Сигнал «я живий»:</b> POST …/hb з <code>{key}</code> раз на кілька хвилин. Якщо бот мовчить довше за HEARTBEAT_TIMEOUT — я напишу, що він упав. Разом із сигналом бот може повідомити свою адресу команд <code>cmd_url</code>.\n<b>Головний → бот:</b> я надсилаю POST на cmd_url з <code>{key, text, type, from, hop}</code>. Якщо бот відповідає <code>{\"reply\": \"…\"}</code>, відповідь з'являється у «Вхідних».\n<b>Без коду:</b> перешліть мені будь-яке повідомлення бота — воно стане подією цього бота.\n<b>Bot-to-bot:</b> з травня 2026 Telegram дозволяє ботам писати одне одному в особисті, якщо в обох увімкнено режим у @BotFather."),
    "rules": ("🔀 Пересилання й захист", "Правило передає подію іншому боту автоматично. Вручну — кнопка «↪️ Переслати боту» під будь-якою подією.\nЗахист від зациклень:\n• кожна передача рахується; після RELAY_MAX_HOPS (3) далі не йде;\n• однаковий текст від того самого бота за 2 хв відкидається;\n• понад RELAY_RATE_PER_MIN (30) повідомлень за хвилину від бота — пауза 5 хв і сповіщення вам."),
    "monitor": ("📡 Моніторинг", "<b>Схеми перевірок</b> (📡 Моніторинг): 🟢 Постійно — кожні N хв; 🗓 За розкладом — у задані години й дні; ⏱ Таймер — кожні N хв лише в обрані години; 👆 За наказом — лише кнопкою «Перевірити зараз» або /check. Кожному боту можна задати свій розклад (⏱ біля бота).\nНовий графік я одразу передаю ботам, що приймають команди: вони підлаштовують сигнали «я живий» і свої пробудження.\n<b>😴 Плани сну:</b> боти обмінюються планами (не спить / спить, доки не покличуть / прокидається о …) лише при змінах. Сплячого бота я не перевіряю й не викликаю, повідомлення для нього тримаю до пробудження (тривоги — одразу). «⏰ Розбудити» — у картці бота й у 🩺 → 🔗 Зв'язки.\n<b>⚡ Режим роботи</b> (🧰 Інструменти): як у ШІ-бота — завжди активний, сон між повідомленнями або сон + будильник; у «⏰» я прокидаюсь саме на перевірки за розкладом.\nЩо перевіряю: Telegram — getMe і getWebhookInfo; WhatsApp — стан номера; Instagram — токен; інші — сигнал «я живий». Пишу лише при зміні стану."),

    "manage": ("✏️ Що можна міняти", "Telegram: назву, опис, короткий опис, список команд, писати від імені бота. Лише в @BotFather: аватар, inline-режим, створення й видалення ботів, передача власності.\nWhatsApp: опис, статус «Про мене», команди «/». Назву номера змінює Meta після перевірки.\nInstagram: швидкі питання (Ice Breakers). Профіль — лише в застосунку."),
    "safety": ("🛡 Безпека", "• Головний бот відповідає лише власнику (ALLOWED_USERS).\n• Токени в базі зашифровані ключем з MANAGER_SECRET. Не змінюйте його, інакше токени доведеться ввести знову.\n• Якщо токен міг потрапити до чужих рук: @BotFather → /revoke → новий токен → у картці бота «🔑 Замінити токен».\n• Увімкніть двоетапну перевірку в Telegram — це захищає і акаунт, і всіх ботів.\n• Резервні копії — у «🧰 Інструменти»."),
    "buttons": ("🎛 Кнопки", "Усі розділи можна переставляти між меню внизу, «Меню» і групами, перейменовувати, ховати, створювати свої групи. Будь-яку кнопку бота можна перейменувати: «Старий = Новий». У кожному меню є «◀️ Назад», «🏠 Додому» і «Вперед ▶️»."),
}
HELP["community"] = ('🏘 Групи, канали, спільноти', "<b>🏘 Бот у групах, каналах і спільнотах</b> (у меню → «🏘 Спільноти»)\n\n<b>Як додати (Telegram)</b>: група/канал → Додати учасника → бот → зробіть <b>адміністратором</b> (видаляти повідомлення, блокувати, запрошувати; у каналі — публікувати). Бот сам з'явиться в списку й напише вам. Типово додавати бота може лише власник — інакше він сам вийде з чату (перемикач 🔒).\n\n<b>🛡 Правила</b> — заборони для учасників: посилання, пересилання, фото/файли, стікери й GIF, голосові, згадки @, заборонені слова, КАПС, флуд, тиша для новачків, нічна тиша. Адмінів чату не стосуються.\n<b>⚖️ Покарання</b>: видалити · видалити й попередити (після N — тиша на добу) · заглушити на час · вигнати.\n<b>👤 Хто що може</b> — окремі дозволи й заборони понад загальні правила: для <b>всіх</b>, для <b>новачків</b>, для <b>групи учасників</b> або для <b>конкретної людини</b>. Кожне правило: ▫️ як загалом → ✅ дозволено → ⛔ заборонено; є «дозволити/заборонити все» й правило «🤐 Писати взагалі» (лише читання).\nДе діє: лише в цій спільноті, «🌐 у всіх спільнотах» або «📍 скопіювати в інші» — одну, кілька чи всі.\nПріоритет: людина › група › новачки › усі; налаштування спільноти важливіші за загальні; якщо людина в кількох групах і одна забороняє — забороняє.\n<b>🏷 Групи учасників</b> («Довірені», «VIP», «Під наглядом»…) — люди з будь-яких спільнот: додати зі списку тих, хто писав, за @ім'ям/ID (кількох одразу), або в чаті у відповідь на повідомлення <code>/group VIP</code>, прибрати — <code>/ungroup VIP</code>. Кілька @імен в «👤 Учаснику…» — бот сам створить з них групу-вибірку.\n<b>🙋 Дозволи за запитом</b>: під попередженням є кнопка «Попросити дозвіл» (або команда /request у чаті). Вам приходить запит → ✅ на 1 год / день / 7 днів / назавжди або ❌. Так само — «Попросити розблокувати». Дозволи видно й можна забрати в «🙋 Запити».\n<b>🚪 Вступ</b>: заявки — приймати всіх, питати вас або не чіпати; привітання новачків ({name} — ім'я); нове посилання-запрошення.\n<b>📣 Публікації</b>: допис зараз, через годину, у певний час, щодня, щотижня (дописи від ШІ — у ШІ-боті).\n<b>👁 Моніторинг</b>: слова-тригери або «пересилати все» — вам у приват (і головному боту).\n<b>📜 Журнал</b> дій і цифри (також у 🩺 Діагностиці).\n<b>📋 Таблиця «де бот»</b>: скільки груп, каналів і спільнот, з посиланнями, @іменами, кількістю учасників і чи бот адмін; «📄 Файл» — та сама таблиця для Excel/Google Таблиць. Для приватних чатів бот створює власне посилання-запрошення (основне посилання чату не чіпає).\n\n<b>Команди в групі</b>: усім — /rules, /request; адмінам чату у відповідь на повідомлення — /warn, /mute 30, /unmute, /ban, /unban, /allow links.\n\n<b>WhatsApp</b>: групи вашого номера через Groups API (лише офіційні бізнес-акаунти, до 8 учасників): публікації, заявки на вступ, видалення порушника з групи. Чужі повідомлення WhatsApp видаляти не дозволяє — замість цього попередження (кожне рахується в 1000 безкоштовних/міс).\n<b>Instagram</b>: груп в API немає; бот модерує коментарі під вашими дописами (сховати/видалити за правилами) і публікує дописи з картинкою.")
HELP["priority"] = ("🏆 Пріоритети", "<b>🏆 Пріоритети</b> (ШІ-бот: «🧰 Інструменти → 🏆 Пріоритети»; тут — «📋 Таблиця ботів»)\n\nРейтинг для <b>розмов і проєктів</b>, <b>відкладених та запланованих завдань</b>, <b>ботів і ШІ</b>, з якими працює бот, і <b>спільнот</b> (каналів, груп). ⬆️/⬇️ — вище чи нижче; у картці елемента — «на самий верх», «у самий низ», «звичайний». Те саме — кнопка «🏆 Пріоритет» на екрані розмови й завдання.\nТри зони: 🔺 важливіші · ▫️ звичайні (усе нове) · 🔻 менш важливі.\n\n<b>Як це працює</b>\n• Одночасно до ШІ йде LLM_PARALLEL запитів (типово 2). Коли всі місця зайняті й приходить важливіший запит — менш важливий <b>не відхиляється</b>, а стає на ⏸ паузу і продовжиться сам, щойно звільниться місце.\n• Менш важливий новий запит стає в 🕒 чергу (за пріоритетом).\n• Той, хто питав, одразу отримує сповіщення: розмова — повідомлення в чат, бот — повідомлення через головного бота, ШІ через конектор — статус «queued» з номером.\n• Пауза — не відмова: у сповіщенні й у відповіді є прапорці <code>queued / keep_waiting / do_not_retry / do_not_cancel</code>. Боти й моделі не скасовують і не надсилають завдання знову — просто чекають своєї черги. Повтор того самого запиту не створює дубля.\n• Відкладені завдання бот виконує від важливіших до менш важливих.\n• Після паузи модель відповідає на запит заново (проміжного тексту в безкоштовних API немає), але запит, історія й пам'ять проєкту зберігаються повністю.\n\n<b>🤝 Боти й ШІ</b> — таблиця всіх, хто звертався до бота (головний бот, боти через нього, ШІ через конектор): посилання, скільки запитів, коли востаннє, звідки. Їхній порядок — це їхній пріоритет.\n<b>🏘 Спільноти</b> — таблиця «де бот» теж сортується за пріоритетом; дописи для важливіших спільнот виходять першими.\nУ 🧭 головному боті — «📋 Таблиця ботів»: посилання, тип, стан, пріоритет (⬆️⬇️) і файл для Excel. Запити важливіших ботів він пересилає першими й передає отримувачу їхній пріоритет.")
HELP_ORDER = ["start", "api", "rules", "monitor", "priority", "community", "manage", "safety", "buttons"]


def help_screen(data):
    parts = data.split(":")
    if len(parts) > 2 and parts[1] == "g" and parts[2] in HELP:
        t, body = HELP[parts[2]]
        return f"<b>{t}</b>\n\n{body}", []
    return "<b>📖 Довідка головного бота</b>\n\nПрацює без ШІ. Оберіть розділ:", pairs([(HELP[k][0], f"help:g:{k}") for k in HELP_ORDER])


# ---------------------------------------------------------------- 💾 копії

BK = Backups(DB_PATH, kv_get, kv_set, PUBLIC_URL, name="manager", project_root=os.path.dirname(os.path.abspath(__file__)))
PENDING_RESTORE = {}


# ---------------------------------------------------------------- 🩺 діагностика
DIAG_TABS = [("sum", "🩺 Огляд"), ("state", "✅ Стан"), ("links", "🔗 Зв'язки"), ("nums", "📊 Цифри"), ("tips", "💡 Поради")]


def links_report(chat):
    """🔗 Зв'язки: усі підключені боти — хто зараз не спить, хто спить і до котрої (за їхніми планами, без зайвих запитів)."""
    me = manager_plan()
    lines = ["<b>🔗 Пов'язані боти</b>", f"Я (головний): {esc(plan_text(me))}" + (" · адреса пробудження є" if me.get("wake_url") else ""),
             "Плани сну боти пересилають лише при змінах. Поки бот спить, я його не викликаю, а повідомлення для нього тримаю до пробудження (тривоги будять одразу).", ""]
    rows = []
    for b in bots_list():
        p = bplan(b)
        sl = sleeping(b)
        if sl is None:
            state = {"up": "🟢 працює", "down": "🔴 не відповідає", "warn": "🟡 проблема"}.get(b["status"], "⚪️ ще не перевірявся")
        else:
            state = "😴 спить" + (f" до {datetime.fromtimestamp(sl, TZ).strftime('%d.%m %H:%M')}" if sl else ", доки не покличуть")
        held = len(json.loads(b.get("held") or "[]"))
        lines.append(f"{bot_icon(b)} <b>{esc(b['name'])}</b> — {state}" + (f" · чекає повідомлень: {held}" if held else ""))
        lines.append(f"    план: {esc(plan_text(p)) if p else 'не надсилав (вважаю, що не спить)'} · сигнал: {ts(b['last_seen'])}"
                     + ("" if (b.get("cmd_url") or b["kind"] != "http") else " · ⚠️ немає адреси команд"))
        if b.get("cmd_url") or (p or {}).get("wake_url"):
            rows.append([(f"⏰ Розбудити «{short(b['name'], 18)}»", f"m:bwake:{b['id']}")])
    if not bots_list():
        lines.append("Ботів ще немає.")
    if NODE.enabled:
        lines += ["", "<b>🛰 Сервери головного бота</b>"]
        for n, _ in NODE.nodes:
            p = NODE.peers.get(n) or {}
            lines.append(f"• {esc(n)} — " + (("цей, " + ("працює" if NODE.active else "у резерві")) if n == NODE.name else
                                              (("✅ живий" + (" · працює" if p.get("status", {}).get("active") else "")) if p.get("ok") else "❌ недоступний")))
        rows.append([("🛰 Сервери", "m:cl")])
    cc = CM.counts()
    lines += ["", "<b>🏘 Чати, групи, канали</b>",
              (f"Усього {cc['chats']}; бот не адмін у: {esc(', '.join(cc['noadmin'])) if cc['noadmin'] else 'жодному'}" if cc["chats"] else "Головного бота не додано в групи чи канали.")]
    tg = [TARGET_TITLES[t][1] for t in ("file", "channel", *CLOUDS) if BK.enabled(t)]
    lines += ["", "<b>🧩 Ресурси</b>", "Копії: " + esc(", ".join(tg) or "—") + (" · автокопії ✅" if BK.cfg().get("auto_enabled") else " · автокопії вимкнені"),
              "Будильник: " + ("cron-job.org ✅" if NODE.cron_key() else "не підключено") + f" · адреса: {esc(WEBHOOK_BASE or 'не задана')}"]
    rows.append([("📤 Розіслати мій план", "m:lkplan"), ("🔄 Перевірити всіх", "m:monrun")])
    return lines, rows


def can_receive(b):
    return bool(b.get("cmd_url") or (b["kind"] == "telegram" and b["via_tg"]))


def diag_collect(chat):
    ok, bad, tips, conflicts, good = [], [], [], [], []
    bots, trash = bots_list(), bots_list("trash")
    by_id = {b["id"]: b for b in bots + trash}
    up = [b for b in bots if b["status"] == "up" and b["monitor"]]
    for b in bots:
        if not b["monitor"]:
            continue
        if b["status"] == "down":
            bad.append(f"🔴 {b['name']}: {b['status_detail'] or 'не працює'}")
        elif b["status"] == "warn":
            bad.append(f"🟡 {b['name']}: {b['status_detail']}")
        elif b["status"] == "unknown":
            bad.append(f"⚪️ {b['name']}: ще жодного разу не перевірено / не надсилав «я живий»")
    if up:
        ok.append(f"🟢 Працюють: {', '.join(b['name'] for b in up[:8])}" + (" …" if len(up) > 8 else ""))
    if not bots:
        tips.append("Додайте першого бота: «🤖 Боти → ➕ Додати бота»")
    if not PUBLIC_URL:
        bad.append("🌐 MANAGER_PUBLIC_URL не задано — скринька й вхід у хмари не працюватимуть")
    else:
        ok.append("📥 Скринька для ботів доступна")
    g = gsched()
    if g.get("pause_until") and g["pause_until"] > time.time():
        bad.append("⏸ Моніторинг усіх ботів на паузі до " + datetime.fromtimestamp(g["pause_until"], TZ).strftime("%d.%m %H:%M"))
        down = [b["name"] for b in bots if b["status"] == "down"]
        if down:
            conflicts.append(f"Моніторинг на паузі, а {', '.join(down)} не працює — ви не дізнаєтесь, коли він повернеться")
    off = [b["name"] for b in bots if not b["monitor"]]
    if off:
        tips.append(f"Не стежу за: {', '.join(off)}. Якщо це тимчасово — краще «⏱ Розклад і пауза» з таймером, тоді стеження повернеться само")
    silent = [b["name"] for b in bots if b["alerts"] == "off" and b["monitor"]]
    if silent:
        conflicts.append(f"За {', '.join(silent)} стежу, але сповіщення вимкнено — про падіння дізнаєтесь лише з «Вхідних»")
    muted = [by_id[i]["name"] for i, t in MUTED.items() if t > time.time() and i in by_id]
    if muted:
        bad.append(f"🛑 На паузі через флуд: {', '.join(muted)}")
    nohb = [b["name"] for b in bots if b["kind"] == "http" and not b["last_seen"]]
    if nohb:
        tips.append(f"Ще не надсилали «я живий»: {', '.join(nohb)} — додайте hubclient (HUB_URL, HUB_KEY), інакше стан невідомий")
    # правила
    with closing(db()) as con:
        rules = [dict(r) for r in con.execute("SELECT * FROM rules")]
        ev = {r[0]: r[1] for r in con.execute("SELECT type, COUNT(*) FROM events WHERE state='active' GROUP BY type")}
        src = {r[0]: r[1] for r in con.execute("SELECT source, COUNT(*) FROM events GROUP BY source")}
        unread = con.execute("SELECT COUNT(*) FROM events WHERE state='active' AND read=0").fetchone()[0]
        dl_ok, dl_bad = con.execute("SELECT COALESCE(SUM(ok),0), COALESCE(SUM(1-ok),0) FROM deliveries").fetchone()
    pairs_ = set()
    seen = set()
    for r in rules:
        to = by_id.get(r["to_bot"])
        name = f"правило #{r['id']}"
        if not to:
            conflicts.append(f"{name} передає неіснуючому боту")
            continue
        if to["state"] == "trash":
            conflicts.append(f"{name} передає боту «{to['name']}», який лежить у кошику")
        elif not can_receive(to):
            conflicts.append(f"{name} передає «{to['name']}», але той не вміє приймати (немає адреси команд чи bot-to-bot) — «🔌 Підключення»")
        if r["from_bot"] and r["from_bot"] == r["to_bot"]:
            conflicts.append(f"{name}: бот пересилає сам собі")
        key = (r["from_bot"], r["type"], r["contains"], r["to_bot"])
        if key in seen:
            conflicts.append(f"{name} повторює інше правило — події йтимуть двічі")
        seen.add(key)
        if r["enabled"] and r["from_bot"]:
            pairs_.add((r["from_bot"], r["to_bot"]))
    for a_, b_ in pairs_:
        if (b_, a_) in pairs_ and a_ < b_:
            conflicts.append(f"Правила ганяють події по колу між «{by_id[a_]['name']}» і «{by_id[b_]['name']}». Захист від зациклень зупинить це після {MAX_HOP} передач, але краще прибрати одне з правил")
    if rules and not conflicts:
        good.append(f"{len(rules)} правил(а) пересилання без суперечностей; захист від зациклень увімкнено")
    if unread > 50:
        tips.append(f"Непрочитаних подій {unread} — «📥 Вхідні → ✅ Усе прочитано» або фільтр «🚨»")
    # копії
    c = BK.cfg()
    last = c.get("last")
    (ok if last and time.time() - last < 8 * 86400 else bad).append("💾 Остання копія: " + (f"{int((time.time() - last) // 86400)} дн тому" if last else "ще не робилася"))
    if not any(BK.enabled(t) for t in ("channel", *CLOUDS)):
        tips.append("Копії лише файлом у чат — підключіть хмару або канал, щоб не втратити список ботів і правила")
    if env("BACKUP_PASSWORD"):
        good.append("Копії шифруються паролем; токени ботів у базі теж зашифровані")
    elif any(BK.enabled(t) for t in ("channel", *CLOUDS)):
        conflicts.append("Копії йдуть у хмару без BACKUP_PASSWORD")
    if bots and len(up) == len([b for b in bots if b["monitor"]]) and up:
        good.append("Усі боти, за якими стежу, працюють")
    cc = CM.counts()
    if cc["chats"]:
        ok.append(f"🏘 Спільнот: {cc['chats']} · правил: {cc['rules_on']} · дописів у розкладі: {cc['posts']}")
        good.append(f"🏘 У спільнотах: побачено {cc['stats']['seen']}, прибрано {cc['stats']['deleted'] + cc['stats']['hidden']}, "
                    f"попереджень {cc['stats']['warned']}, дописів {cc['stats']['posted']}, дозволів {cc['stats']['permits']}")
    for t in cc["noadmin"]:
        bad.append(f"🏘 У «{t}» бот не адміністратор — не бачить усіх повідомлень і не може видаляти порушення")
    if cc["reqs"]:
        tips.append(f"🙋 {cc['reqs']} запит(ів) дозволу/вступу чекають рішення — 🏘 Спільноти")
    counts, mconf, mgood = L.report(chat)
    conflicts += mconf
    good += mgood
    kinds = {}
    for b in bots:
        kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
    nums = {"bots": len(bots), "trash": len(trash), "kinds": kinds, "up": len(up), "monitored": sum(1 for b in bots if b["monitor"]),
            "events": sum(ev.values()), "ev": ev, "src": src, "unread": unread, "rules": len(rules), "rules_on": sum(r["enabled"] for r in rules),
            "hits": sum(r["hits"] for r in rules), "dl_ok": dl_ok, "dl_bad": dl_bad, "counts": counts}
    score = round(100 * (len(ok) + len(good)) / max(1, len(ok) + len(bad) + len(good) + len(conflicts)))
    return {"ok": ok, "bad": bad, "tips": tips, "conflicts": conflicts, "good": good, "nums": nums, "score": score}


def scr_diag(chat, tab="sum"):
    d = diag_collect(chat)
    for k in ("ok", "bad", "tips", "conflicts", "good"):
        d[k] = [esc(x) for x in d[k]]
    n = d["nums"]
    if tab == "state":
        body = ["<b>✅ Працює</b>", *(d["ok"] or ["—"]), "", "<b>❌ Не працює або обмежено</b>", *(d["bad"] or ["нічого — усе гаразд"])]
    elif tab == "nums":
        body = ["<b>📊 Скільки всього</b>",
                f"🤖 Ботів: {n['bots']} (" + ", ".join(f"{P.KINDS[k][1]}: {v}" for k, v in n["kinds"].items()) + f") · у кошику {n['trash']}",
                f"📡 Під наглядом: {n['monitored']} · працюють: {n['up']}",
                f"📥 Подій: {n['events']} · непрочитаних {n['unread']}",
                *(["   " + " · ".join(f"{TYPES.get(k, k)}: {v}" for k, v in n["ev"].items())] if n["ev"] else []),
                f"🔀 Правил: {n['rules']} (увімкнено {n['rules_on']}) · спрацювали {n['hits']} раз(ів)",
                f"🎛 Кнопок: унизу {n['counts']['kb']}, у «Меню» {n['counts']['home']}, у групах {n['counts']['group']}, приховано {n['counts']['off']}",
                "", "<b>📈 Пророблено роботи</b>",
                f"Передано ботам: {n['dl_ok']} · не вдалося: {n['dl_bad']}",
                *[f"• {SOURCES.get(k, k)}: {v}" for k, v in n["src"].items()]]
    elif tab == "tips":
        body = ["<b>💡 Що краще увімкнути чи змінити</b>", *(f"• {t}" for t in d["tips"] or ["Порад немає — налаштовано добре."]),
                "", "<b>⚠️ Що суперечить одне одному</b>", *(f"• {c}" for c in d["conflicts"] or ["нічого"]),
                "", "<b>🤝 Що працює гармонійно</b>", *(f"• {g}" for g in d["good"] or ["—"])]
    else:
        bar = "▰" * round(d["score"] / 12.5) + "▱" * (8 - round(d["score"] / 12.5))
        body = [f"Загальний стан: <b>{d['score']}%</b> {bar}", "",
                f"✅ Працює: <b>{len(d['ok'])}</b> · ❌ проблем: <b>{len(d['bad'])}</b>",
                f"⚠️ Суперечностей: <b>{len(d['conflicts'])}</b> · 💡 порад: <b>{len(d['tips'])}</b> · 🤝 гармонійно: <b>{len(d['good'])}</b>",
                f"🤖 {n['bots']} ботів · 🟢 {n['up']} працюють · 📥 {n['unread']} непрочитаних · 🔀 {n['rules_on']} правил"]
        if d["bad"] or d["conflicts"]:
            body += ["", "<b>Головне зараз:</b>", *(d["bad"] + d["conflicts"])[:3]]
    tabs = [[(("● " if t == tab else "") + label, f"m:diag:{t}") for t, label in DIAG_TABS[:3]], [(("● " if t == tab else "") + label, f"m:diag:{t}") for t, label in DIAG_TABS[3:]]]
    if tab == "links":
        body, extra = links_report(chat)
        return "<b>🩺 Діагностика</b>\n\n" + "\n".join(body), tabs + extra
    rows = tabs + [[("🤖 Боти", "m:bl"), ("📡 Моніторинг", "m:mon"), ("🔀 Правила", "m:rl")], [("💾 Копії", "m:bk"), ("🎛 Кнопки", "m:btn")]]
    return "<b>🩺 Діагностика</b>\n\n" + "\n".join(body), rows


# ---------------------------------------------------------------- 🏭 заводські налаштування
# Боти, їхні токени, ключі скриньки, адреси команд і bot-to-bot НЕ видаляються ніколи — лише те, що позначите.
RESET_PARTS = [
    ("settings", "⚙️ Налаштування (сортування, фільтри)", True),
    ("buttons", "🎛 Кнопки й меню (розташування, групи, назви)", True),
    ("watch", "📡 Стеження за ботами: знову «стежити» й «усі події» для всіх", False),
    ("events", "📥 Вхідні (усі події)", False),
    ("rules", "🔀 Правила пересилання", False),
    ("trash", "🗑 Кошик (події; боти з кошика лишаються)", False),
    ("community", "🏘 Спільноти: правила, дозволи, дописи, журнал (бот лишається в чатах)", False),
]
BUTTON_PREFS = ("layout", "groups", "labels", "relabel", "kb_cols", "home_cols")


def reset_sel(chat):
    raw = kv_get(f"reset_sel:{chat}")
    return set(json.loads(raw)) if raw else {k for k, _, on in RESET_PARTS if on}


def scr_reset(chat):
    sel = reset_sel(chat)
    text = ("<b>🏭 Скидання до заводських налаштувань</b>\n\nПозначте, що скинути або стерти. <b>Не зачіпається ніколи:</b> "
            "ваші боти, їхні токени, ключі скриньки, адреси команд і bot-to-bot, підключені хмари для копій.\n\n"
            "Перед скиданням я надішлю вам резервну копію.")
    rows = [[(("✅ " if k in sel else "▫️ ") + label, f"m:frt:{k}")] for k, label, _ in RESET_PARTS]
    rows.append([("☑️ Усе", "m:frt:all"), ("▫️ Лише налаштування", "m:frt:min")])
    rows.append([("🏭 Скинути вибране", "m:frgo")])
    return text, rows


def factory_reset(chat, sel):
    done = []
    p = json.loads(kv_get(f"prefs:{chat}") or "{}")
    if "settings" in sel:
        p = {k: v for k, v in p.items() if k in BUTTON_PREFS}
        done.append("налаштування")
    if "buttons" in sel:
        p = {k: v for k, v in p.items() if k not in BUTTON_PREFS}
        L.hist.pop(chat, None)
        done.append("кнопки й меню")
    kv_set(f"prefs:{chat}", json.dumps(p, ensure_ascii=False))
    with closing(db()) as con, con:
        if "watch" in sel:
            con.execute("UPDATE bots SET monitor=1, alerts='all'")
            done.append("стеження за ботами")
        if "events" in sel:
            con.execute("DELETE FROM deliveries")
            con.execute("DELETE FROM events")
            done.append("вхідні")
        if "rules" in sel:
            con.execute("DELETE FROM rules")
            done.append("правила")
        if "trash" in sel:
            con.execute("DELETE FROM events WHERE state='trash'")
            done.append("кошик")
    if "community" in sel:
        CM.reset()
        done.append("налаштування спільнот")
    return done


async def bk_run(bot, chat, auto=False, only=None):
    async def send_file(name, data):
        await bot.send_document(chat, InputFile(io.BytesIO(data), filename=name), caption="💾 Копія головного бота. Токени в ній зашифровані MANAGER_SECRET.")

    async def send_channel(ch, name, data):
        m = await bot.send_document(ch, InputFile(io.BytesIO(data), filename=name), caption=f"💾 {name}")
        return m.document.file_id

    if auto and not only:
        only = [t for t in ("channel", *CLOUDS) if BK.enabled(t)] or ["file"]
    name, size, res = await BK.run(send_file, send_channel, only=only)
    lines = [f"💾 <b>{esc(name)}</b> · {human_size(size)}"] + [f"{'✅' if ok else '⚠️'} {' '.join(TARGET_TITLES[t])}" + ((f" · {esc(d)}" if d else "") if ok else f": {esc(d)}") for t, ok, d in res]
    if not (auto and all(ok for _, ok, _ in res)):
        await bot.send_message(chat, "\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- маршрутизатор

EDITOR_ACTIONS = {"btn", "bi", "bmv", "bup", "bdn", "bset", "lren", "lunren", "bgnew", "bgdel", "brl", "brldel", "breset"}
VIEW = {"home", "bl", "bt", "rm", "rw", "cl", "b", "bp", "bc", "bconn", "in", "e", "rl", "mon", "api", "tr", "bk", "bkt", "btn", "bi", "brl", "grp", "snd", "fr", "bsch", "bks", "bkls", "bkrs", "bkfmt", "bkscope", "diag"}


def is_view(data):
    if data.startswith("help:"):
        return True
    p = data.split(":")
    if len(p) > 1 and p[1] == "cm":
        return CM.is_view(p)
    return len(p) > 1 and p[1] in VIEW


async def open_data(update, ctx, data, edit=True, record=True):
    chat = update.effective_chat.id
    if data.startswith("help:"):
        if record:
            L.visit(chat, data)
        return await show(update, *help_screen(data), edit=edit)
    return await route(update, ctx, data, edit=edit, record=record)


async def route(update, ctx, data, edit=True, record=True):
    chat = update.effective_chat.id
    q = update.callback_query
    parts = data.split(":")
    a = parts[1] if len(parts) > 1 else "home"
    arg = parts[2] if len(parts) > 2 else None
    screen, toast = None, None
    if a == "noop":
        return await q.answer() if q else None
    if a == "nav":
        target = "m:home" if arg == "home" else (L.back(chat) if arg == "back" else L.fwd(chat))
        if arg == "home":
            L.visit(chat, "m:home")
        if q:
            await q.answer()
        return await open_data(update, ctx, target, edit=edit, record=False)
    if record and is_view(data):
        L.visit(chat, data)
    if a == "cm":  # 🏘 Спільноти
        toast, screen = await CM.route(chat, parts)
        if q:
            try:
                await q.answer(toast[:190] if toast else None)
            except Exception:  # noqa: BLE001
                pass
        return await show(update, *screen, edit=edit)

    if a in ("home", "cancel"):
        AWAIT.pop(chat, None)
        screen = scr_home(chat)
    elif a == "diag":
        screen = scr_diag(chat, arg or "sum")
    elif a == "fr":
        screen = scr_reset(chat)
    elif a == "frt":
        sel = reset_sel(chat)
        sel = {k for k, _, _ in RESET_PARTS} if arg == "all" else {"settings", "buttons"} if arg == "min" else sel ^ {arg}
        kv_set(f"reset_sel:{chat}", json.dumps(sorted(sel)))
        screen = scr_reset(chat)
    elif a == "frgo":
        sel = reset_sel(chat)
        names = [label.split(" (")[0].split(":")[0] for k, label, _ in RESET_PARTS if k in sel]
        screen = (("<b>🏭 Точно скинути?</b>\n\n" + "\n".join(names) + "\n\nБоти й усі підключення лишаються. Спершу я надішлю резервну копію.",
                   [[("✅ Так, скинути", "m:fryes"), ("✖️ Ні", "m:fr")]]) if sel else ("Нічого не позначено.", [[("« Назад", "m:fr")]]))
    elif a == "fryes":
        if q:
            await q.answer("Роблю копію й скидаю…")
        try:
            await bk_run(ctx.bot, chat, only=["file"])
        except Exception as e:  # noqa: BLE001
            log.warning("копія перед скиданням не вдалася: %s", e)
        done = factory_reset(chat, reset_sel(chat))
        await ctx.bot.send_message(chat, "🏭 Скинуто: " + ", ".join(done) + ".\nПовернути все можна, надіславши мені файл копії вище.", reply_markup=menu_kb(chat))
        L.visit(chat, "m:home")
        return await show(update, *scr_home(chat), edit=False)
    elif a == "grp":
        screen = L.scr_group(chat, arg)
    elif a in EDITOR_ACTIONS:
        prompts = []

        def ask_(action, d, prompt):
            ask(chat, action, d)
            if prompt:
                prompts.append(prompt)

        screen, toast = handle_editor(L, chat, parts, ask_)
        if prompts:
            if q:
                await q.answer()
            return await ctx.bot.send_message(chat, prompts[0], parse_mode=ParseMode.HTML, reply_markup=kb([CANCEL_ROW]))
        if a in ("bmv", "bset", "breset", "bgdel", "lunren"):
            await ctx.bot.send_message(chat, "⌨️ Меню оновлено", reply_markup=menu_kb(chat))
    # боти
    elif a == "bt":
        screen = scr_bot_table(chat)
    elif a == "btm":
        RK.move("bots", parts[2], parts[3])
        screen = scr_bot_table(chat)
    elif a == "btf":
        await ctx.bot.send_document(chat, InputFile(io.BytesIO(bot_table_csv()), filename="боти-" + datetime.now(TZ).strftime("%Y-%m-%d") + ".csv"),
                                    caption="📋 Усі боти з посиланнями й пріоритетом. Відкривається в Excel, Google Таблицях, Numbers.")
        toast, screen = "Файл надіслано", scr_bot_table(chat)
    elif a == "bl":
        screen = scr_bots(chat)
    elif a == "bsort":
        set_pref(chat, "bots_sort", arg)
        screen = scr_bots(chat)
    elif a == "b":
        screen = scr_bot(chat, int(arg))
    elif a == "badd":
        screen = ("<b>➕ Новий бот</b>\n\nЯкий бот?", [[(f"{i} {n}", f"m:badd2:{k}")] for k, (i, n) in P.KINDS.items()])
    elif a == "badd2":
        ask(chat, f"add_{arg}", {})
        prompt = {
            "telegram": "✈️ Надішліть <b>токен</b> бота з @BotFather (виглядає як <code>123456:ABC…</code>). Повідомлення з токеном я одразу видалю.",
            "whatsapp": "🟩 Надішліть <b>ID номера</b> (Phone number ID) і <b>постійний токен</b> через пробіл:\n<code>123456789012345 EAAG…</code>\nДе їх узяти — в інструкції WhatsApp з архіву.",
            "instagram": "📸 Надішліть <b>токен Instagram API</b> (Instagram Login, з правами instagram_business_manage_messages).",
            "http": "🔌 Як назвати цей бот чи сервіс? Наприклад: «Монітор сайту».",
        }[arg]
        screen = (prompt, [CANCEL_ROW])
    elif a == "bchk":
        if q:
            await q.answer("Перевіряю…")
        st, detail = await check_bot(bot_get(int(arg)))
        toast = {"up": "🟢 Працює", "warn": "🟡 " + detail, "down": "🔴 " + detail}.get(st, detail or "Перевірено")[:190]
        screen = scr_bot(chat, int(arg))
    elif a == "bmon":
        b = bot_get(int(arg))
        bot_update(b["id"], monitor=0 if b["monitor"] else 1)
        await push_sched(bot_get(b["id"]))
        toast = "⏸ Стеження вимкнено, доки не ввімкнете" if b["monitor"] else "▶️ Стеження ввімкнено"
        screen = scr_bot(chat, b["id"])
    elif a in ("bsch", "bse", "bsd", "bsw", "bsw2", "bswin", "bswinx", "bsp", "bsres", "bsx", "bspush"):
        bid = int(arg)
        b = bot_get(bid)
        s_ = bsched(b)
        if a == "bse":
            s_.pop("daily", None)
            s_.pop("weekly", None)
            s_["every"] = int(parts[3])
            toast = "⏱ Збережено"
        elif a == "bsd":
            ask(chat, "bsd", bid)
            screen = ("📅 О котрій перевіряти щодня? Надішліть час, наприклад <code>09:00</code>.", [CANCEL_ROW])
        elif a == "bsw":
            screen = ("🗓 У який день тижня?", [[(d, f"m:bsw2:{bid}:{n}") for n, d in enumerate(WEEKDAYS)]])
        elif a == "bsw2":
            ask(chat, "bsw", (bid, int(parts[3])))
            screen = ("🗓 О котрій? Надішліть час, наприклад <code>09:00</code>.", [CANCEL_ROW])
        elif a == "bswin":
            ask(chat, "bswin", bid)
            screen = ("🌙 У які години перевіряти? Наприклад <code>08:00-23:00</code> (вночі — тиша) або <code>22:00-06:00</code>.", [CANCEL_ROW])
        elif a == "bswinx":
            s_.pop("window", None)
            toast = "Цілодобово"
        elif a == "bsp":
            s_["pause_until"] = pause_ts(parts[3])
            toast = "⏸ Пауза до " + datetime.fromtimestamp(s_["pause_until"], TZ).strftime("%d.%m %H:%M") + " — потім відновлю сам"
        elif a == "bsres":
            s_.pop("pause_until", None)
            toast = "▶️ Відновлено"
        elif a == "bsx":
            s_ = {}
            toast = "↺ Як для всіх"
        if a in ("bse", "bswinx", "bsp", "bsres", "bsx"):
            set_bsched(bid, s_)
            pushed = await push_sched(bot_get(bid))
            if pushed is False:
                toast = (toast or "") + " · бот не прийняв розклад"
        elif a == "bspush":
            pushed = await push_sched(b)
            toast = "📤 Розклад передано боту" if pushed else "⚠️ Бот не відповів"
        if not screen:
            screen = scr_sched(chat, bid)
    elif a == "mge":
        g = gsched()
        g["every"] = int(arg)
        set_gsched(g)
        screen = scr_monitor(chat)
    elif a == "mgp":
        g = gsched()
        g["pause_until"] = pause_ts(arg)
        set_gsched(g)
        toast, screen = "⏸ Моніторинг усіх ботів на паузі — потім відновлю сам", scr_monitor(chat)
    elif a == "mgr":
        g = gsched()
        g.pop("pause_until", None)
        set_gsched(g)
        toast, screen = "▶️ Відновлено", scr_monitor(chat)
    elif a == "balr":
        b = bot_get(int(arg))
        order = list(ALERT_LEVELS)
        bot_update(b["id"], alerts=order[(order.index(b["alerts"]) + 1) % len(order)])
        toast = ALERT_LEVELS[bot_get(b["id"])["alerts"]]
        screen = scr_bot(chat, b["id"])
    elif a == "bpin":
        b = bot_get(int(arg))
        bot_update(b["id"], pinned_at=None if b["pinned_at"] else time.time())
        screen = scr_bot(chat, b["id"])
    elif a == "bren":
        ask(chat, "bren", int(arg))
        screen = ("✏️ Надішліть нову назву. Можна додати нотатку з нового рядка — наприклад, для чого цей бот.", [CANCEL_ROW])
    elif a == "bdel":
        b = bot_get(int(arg))
        screen = (f"🗑 Перенести «{esc(b['name'])}» у кошик? Сам бот у Telegram продовжить працювати — я лише перестану ним керувати й стежити.",
                  [[("🗑 Так", f"m:bdely:{b['id']}"), ("✖️ Ні", f"m:b:{b['id']}")]])
    elif a == "bdely":
        bot_update(int(arg), state="trash", trashed_at=time.time())
        toast, screen = f"🗑 У кошику: видалиться через {TRASH_DAYS} днів", scr_bots(chat)
    elif a == "brs":
        bot_update(int(arg), state="active", trashed_at=None)
        toast, screen = "↩️ Відновлено", scr_trash(chat)
    elif a == "btok":
        b = bot_get(int(arg))
        ask(chat, "btok", b["id"])
        tip = ("Спершу в @BotFather: /mybots → бот → API Token → Revoke current token. Старий токен перестане працювати.\n\n" if b["kind"] == "telegram" else "")
        screen = (f"🔑 <b>Новий токен для «{esc(b['name'])}»</b>\n\n{tip}Надішліть новий токен — я перевірю його й видалю повідомлення.", [CANCEL_ROW])
    elif a == "bconn":
        screen = scr_conn(chat, int(arg))
    elif a == "bkey":
        bot_update(int(arg), inbox_key=secrets.token_urlsafe(24))
        toast = "Новий ключ. Оновіть HUB_KEY у цього бота."
        screen = scr_conn(chat, int(arg))
    elif a == "bcmd":
        ask(chat, "bcmd", int(arg))
        screen = ("✍️ Надішліть HTTP-адресу, на яку я надсилатиму команди цьому боту (POST JSON).", [CANCEL_ROW])
    elif a == "bcmdx":
        bot_update(int(arg), cmd_url=None)
        screen = scr_conn(chat, int(arg))
    elif a == "bvtg":
        b = bot_get(int(arg))
        bot_update(b["id"], via_tg=0 if b["via_tg"] else 1)
        screen = scr_conn(chat, b["id"])
    elif a == "btest":
        b = bot_get(int(arg))
        eid, why = event_add(None, "command", "🔔 Тестове повідомлення від головного бота", "manager")
        ok, detail = await deliver(event_get(eid), b) if eid else (False, why)
        toast = "✅ Доставлено" if ok else f"⚠️ {detail}"[:190]
        screen = scr_conn(chat, b["id"])
    elif a == "bp":
        if q:
            await q.answer()
        screen = await scr_profile(chat, int(arg))
    elif a == "bpe":
        ask(chat, "bpe", (int(arg), parts[3]))
        screen = ("✏️ Надішліть новий текст.", [CANCEL_ROW])
    elif a == "bc":
        if q:
            await q.answer()
        screen = await scr_commands(chat, int(arg))
    elif a == "bce":
        b = bot_get(int(arg))
        ask(chat, "bce", b["id"])
        ex = "<code>питання про ціни\nЯк замовити?\nДе ви знаходитесь?</code>" if b["kind"] == "instagram" else "<code>start - Почати\nhelp - Довідка\nstatus - Стан</code>"
        screen = (f"✍️ Надішліть список, кожен пункт з нового рядка:\n{ex}", [CANCEL_ROW])
    elif a == "bcx":
        b = bot_get(int(arg))
        try:
            await P.set_commands(b["kind"], secrets_of(b), [])
            toast = "Очищено"
        except Exception as e:  # noqa: BLE001
            toast = f"⚠️ {e}"[:190]
        screen = await scr_commands(chat, b["id"])
    elif a == "bas":
        ask(chat, "bas", int(arg))
        screen = ("💬 Надішліть текст — бот напише його вам (якщо ви вже відкривали чат із цим ботом).", [CANCEL_ROW])
    elif a in ("bsend", "snd"):
        if a == "snd" and not arg:
            screen = scr_pick_bot("📨 Надіслати боту", "m:bsend")
        else:
            ask(chat, "bsend", int(arg))
            screen = (f"📨 Що передати боту «{esc(bot_get(int(arg))['name'])}»? Він отримає це як команду від головного бота.", [CANCEL_ROW])
    # події
    elif a == "in":
        screen = scr_inbox(chat, int(arg or 0), parts[3] if len(parts) > 3 else None)
    elif a == "isort":
        set_pref(chat, "inbox_sort", arg)
        screen = scr_inbox(chat)
    elif a == "iread":
        with closing(db()) as con, con:
            con.execute("UPDATE events SET read=1")
        toast, screen = "Усе прочитано", scr_inbox(chat)
    elif a == "e":
        screen = scr_event(chat, int(arg))
    elif a == "erd":
        event_update(int(arg), read=1)
        return await (q.answer("✅ Прочитано") if q else asyncio.sleep(0))
    elif a == "epin":
        e = event_get(int(arg))
        event_update(e["id"], pinned_at=None if e["pinned_at"] else time.time())
        toast = "Відкріплено" if e["pinned_at"] else "📌 Закріплено"
        if q and q.message and q.message.text and not q.message.text.startswith(TYPES.get(e["type"], "")[:2]):
            screen = scr_event(chat, e["id"])
        else:
            return await (q.answer(toast) if q else asyncio.sleep(0))
    elif a == "edel":
        event_update(int(arg), state="trash", trashed_at=time.time())
        toast, screen = "🗑 У кошику", scr_inbox(chat)
    elif a == "ers":
        event_update(int(arg), state="active", trashed_at=None)
        toast, screen = "↩️ Відновлено", scr_trash(chat)
    elif a == "efw":
        e = event_get(int(arg))
        screen = scr_pick_bot("↪️ Переслати подію", f"m:efw2:{e['id']}", exclude=e["bot_id"])
    elif a == "efw2":
        e, to = event_get(int(arg)), bot_get(int(parts[3]))
        if q:
            await q.answer("Передаю…")
        ok, detail = await deliver(e, to)
        screen = (("✅ Передано боту «" + esc(to["name"]) + "»") if ok else f"⚠️ Не вдалося: {esc(detail)}", [[("📥 До події", f"m:e:{e['id']}")]])
    # правила
    elif a == "rl":
        screen = scr_rules(chat)
    elif a == "rnew":
        screen = scr_pick_bot("🔀 Нове правило · 1/4 · від кого", "m:rn1", extra=("🌐 Будь-який бот", "m:rn1:0"))
    elif a == "rn1":
        AWAIT[chat] = ("rule", {"from": int(arg) or None})
        screen = ("<b>🔀 2/4 · Які події</b>", [[("Усі", "m:rn2:any")]] + pairs([(t, f"m:rn2:{k}") for k, t in TYPES.items() if k != "reply"]))
    elif a == "rn2":
        st = AWAIT.get(chat, (None, {}))[1]
        st["type"] = None if arg == "any" else arg
        AWAIT[chat] = ("rule_contains", st)
        screen = ("<b>🔀 3/4 · Слово-фільтр</b>\n\nНадішліть слово, яке має бути в тексті, або натисніть «Будь-який текст».", [[("Будь-який текст", "m:rn3")], CANCEL_ROW])
    elif a == "rn3":
        st = AWAIT.get(chat, (None, {}))[1]
        AWAIT[chat] = ("rule", st)
        screen = scr_pick_bot("🔀 4/4 · кому передавати", "m:rn4", exclude=st.get("from"))
    elif a == "rn4":
        st = AWAIT.pop(chat, (None, {}))[1]
        with closing(db()) as con, con:
            con.execute("INSERT INTO rules (from_bot, type, contains, to_bot, created) VALUES (?,?,?,?,?)", (st.get("from"), st.get("type"), st.get("contains"), int(arg), time.time()))
        toast, screen = "✅ Правило створено", scr_rules(chat)
    elif a == "rtog":
        with closing(db()) as con, con:
            con.execute("UPDATE rules SET enabled=1-enabled WHERE id=?", (int(arg),))
        screen = scr_rules(chat)
    elif a == "rdel":
        with closing(db()) as con, con:
            con.execute("DELETE FROM rules WHERE id=?", (int(arg),))
        toast, screen = "Видалено", scr_rules(chat)
    # інше
    elif a == "mon":
        screen = scr_monitor(chat)
    elif a == "bwake":
        b = bot_get(int(arg))
        if q:
            await q.answer("Буджу…")
        ok = await wake_bot(b) if b else False
        n = await flush_held(bot_get(int(arg)), force=True) if ok else 0
        await update.effective_chat.send_message((f"⏰ «{b['name']}» розбуджено" + (f", передав {n} повідомлень, що чекали" if n else "") + ".") if ok
                                                 else "⚠️ Не вдалося розбудити — бот не відповідає на адресу пробудження чи команд.")
        if ok:
            await check_bot(bot_get(int(arg)), wake=True)
        screen = scr_bot(chat, int(arg)) if L.current(chat, ) and L.current(chat).startswith("m:b:") else scr_diag(chat, "links")
    elif a == "lkplan":
        n = await push_all_plans(force=True)
        toast, screen = f"План розіслано ботам: {n}", scr_diag(chat, "links")
    elif a == "msc":
        g = gsched()
        g["scheme"] = arg if arg in SCHEMES else "always"
        set_gsched(g)
        await sync_manager_wakes()
        await push_all_scheds()
        toast = SCHEMES[g["scheme"]][0]
        if g["scheme"] == "times" and not g.get("times"):
            ask(chat, "mst")
            return await update.effective_chat.send_message("🗓 О котрій перевіряти? Наприклад: <code>09:00, 13:00, 21:00</code> або <code>08:00 пн,ср,пт</code>",
                                                            parse_mode=ParseMode.HTML, reply_markup=kb([CANCEL_ROW]))
        if g["scheme"] == "window" and not g.get("window"):
            ask(chat, "msw")
            return await update.effective_chat.send_message("⏱ У які години перевіряти? Наприклад: <code>08:00-23:00</code>", parse_mode=ParseMode.HTML, reply_markup=kb([CANCEL_ROW]))
        screen = scr_monitor(chat)
    elif a in ("mst", "msw"):
        ask(chat, a)
        tip = ("🗓 О котрій перевіряти? Наприклад: <code>09:00, 13:00, 21:00</code> або <code>08:00 пн,ср,пт</code>" if a == "mst"
               else "⏱ У які години перевіряти? Наприклад: <code>08:00-23:00</code> (можна через північ: <code>22:00-06:00</code>)")
        return await update.effective_chat.send_message(tip, parse_mode=ParseMode.HTML, reply_markup=kb([CANCEL_ROW]))
    elif a in ("rm", "rmset", "rw", "rwadd", "rwdel", "rwt", "rwsync", "cl", "clrun", "clpref", "clsel", "clpool"):
        toast, screen = await route_runmode(update, chat, a, arg)
    elif a == "monrun":
        if q:
            await q.answer("Перевіряю всіх…")
        for b in bots_list():
            await check_bot(b)
        screen = scr_monitor(chat)
    elif a == "api":
        screen = scr_api(chat)
    elif a == "tr":
        screen = scr_trash(chat)
    elif a == "trempty":
        with closing(db()) as con, con:
            con.execute("DELETE FROM bots WHERE state='trash'")
            con.execute("DELETE FROM events WHERE state='trash'")
        toast, screen = "Кошик очищено", scr_trash(chat)
    # копії
    elif a == "bk":
        AWAIT.pop(chat, None)
        screen = bkui.scr_backups(BK, TZ)
    elif a == "bkfmt":
        screen = bkui.scr_backup_formats(BK)
    elif a == "bkfmtog":
        if not BK.toggle_format(arg):
            toast = "Потрібно залишити хоча б один формат"
        else:
            toast = "Формати оновлено"
        screen = bkui.scr_backup_formats(BK)
    elif a == "bkscope":
        screen = bkui.scr_backup_scope(BK)
    elif a == "bkscopev":
        BK.set_scope(arg)
        toast = "Вміст копії змінено"
        screen = bkui.scr_backup_scope(BK)
    elif a in ("bks", "bkse", "bksd", "bksw", "bksw2", "bkspause", "bkstop", "bkstart", "bksx"):
        prompts = []

        def ask_bk(action, d, prompt):
            ask(chat, action, d)
            prompts.append(prompt)

        res = bkui.handle_bk_sched(BK, parts, ask_bk)
        if prompts:
            if q:
                await q.answer()
            return await ctx.bot.send_message(chat, prompts[0], parse_mode=ParseMode.HTML, reply_markup=kb([CANCEL_ROW]))
        if res:
            screen, toast = res
    elif a == "bkgo":
        if q:
            await q.answer("Готую копію…")
        return await bk_run(ctx.bot, chat, only=[arg] if arg else None)
    elif a == "bkt":
        screen = bkui.scr_backup_target(BK, arg, lambda: ask(chat, "bkch"))
    elif a == "bktog":
        BK.toggle(arg)
        screen = bkui.scr_backup_target(BK, arg, lambda: None)
    elif a == "bkdays":
        BK.set_days(int(arg))
        screen = bkui.scr_backups(BK, TZ)
    elif a == "bkdis":
        BK.disconnect(arg)
        screen = bkui.scr_backup_target(BK, arg, lambda: None)
    elif a == "bkchx":
        BK.set_channel(None, None)
        screen = bkui.scr_backup_target(BK, "channel", lambda: ask(chat, "bkch"))
    elif a == "bkls":
        screen = await bkui.scr_backup_list(BK, chat, arg)
    elif a == "bkrs":
        screen = bkui.scr_restore(BK)
    elif a == "bkrc":
        try:
            if arg == "channel":
                f = BK.cfg().get("channel_files", [])[int(parts[3])]
                blob = bytes(await (await ctx.bot.get_file(f["file_id"])).download_as_bytearray())
            else:
                blob = await BK.download(arg, json.loads(kv_get(f"bkls:{chat}:{arg}") or "[]")[int(parts[3])])
        except Exception as e:  # noqa: BLE001
            return await ctx.bot.send_message(chat, f"⚠️ {e}")
        return await ask_restore(ctx.bot, chat, blob)
    elif a == "bkyes":
        blob = PENDING_RESTORE.pop(chat, None)
        keep = kv_get("backup_cfg")
        try:
            BK.restore(blob)
            kv_set("backup_cfg", keep or "")
            screen = ("✅ Базу головного бота відновлено.", [])
        except (BackupError, TypeError) as e:
            screen = (f"⚠️ {esc(e)}", [])
    elif a == "bkno":
        PENDING_RESTORE.pop(chat, None)
        screen = bkui.scr_backups(BK, TZ)

    if q:
        try:
            await q.answer(toast[:190] if toast else None)
        except Exception:  # noqa: BLE001
            pass
    if screen:
        await show(update, *screen, edit=edit)


async def ask_restore(bot, chat, blob):
    try:
        tmp, tables = BK.unpack(blob)
        os.remove(tmp)
    except BackupError as e:
        return await bot.send_message(chat, f"⚠️ {e}")
    PENDING_RESTORE[chat] = blob
    await bot.send_message(chat, "♻️ Відновити базу головного бота з цієї копії? Поточну я збережу поруч.", reply_markup=kb([[("✅ Так", "m:bkyes"), ("✖️ Ні", "m:bkno")]]))


# ---------------------------------------------------------------- текст від власника


async def add_bot_from_text(update, ctx, kind, text):
    chat = update.effective_chat.id
    msg = update.message
    if kind in ("telegram", "whatsapp", "instagram"):
        try:
            await msg.delete()  # токен не лишається в чаті
        except Exception:  # noqa: BLE001
            pass
    try:
        if kind == "telegram":
            token = text.strip()
            i = await P.tg_info(token)
            if bot_by_tg(user_id=i["id"]):
                return await msg.reply_text("Цей бот уже додано.")
            bid = bot_add("telegram", i["name"] or i["username"], i["username"], i["id"], {"token": token})
        elif kind == "whatsapp":
            pid, token = text.split()[:2]
            i = await P.wa_info(token, pid)
            bid = bot_add("whatsapp", i.get("name") or "WhatsApp", i.get("username"), pid, {"token": token, "phone_id": pid})
        elif kind == "instagram":
            token = text.strip()
            i = await P.ig_info(token, None)
            bid = bot_add("instagram", i.get("name") or i.get("username") or "Instagram", i.get("username"), None, {"token": token})
        else:
            bid = bot_add("http", text.strip()[:60])
    except (P.PlatformError, ValueError) as e:
        ask(chat, f"add_{kind}", {})
        return await update.effective_chat.send_message(f"⚠️ Не вийшло: {e}\nНадішліть ще раз або натисніть «Скасувати».", reply_markup=kb([CANCEL_ROW]))
    AWAIT.pop(chat, None)
    await check_bot(bot_get(bid), announce=False)
    await update.effective_chat.send_message("✅ Бота додано." + (" Повідомлення з токеном видалено з чату." if kind != "http" else
                                             " Далі відкрийте «🔌 Підключення»: там ключ і адреса для цього бота."))
    L.visit(chat, f"m:b:{bid}")
    await show(update, *scr_bot(chat, bid), edit=False)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    msg = update.message
    user = update.effective_user
    # 1) повідомлення від іншого бота напряму (Telegram bot-to-bot)
    if user.is_bot:
        b = bot_by_tg(user_id=user.id, username=user.username)
        if b and msg.text:
            hop = 0
            text = msg.text
            if text.startswith("[hop:"):
                try:
                    hop, text = int(text[5 : text.index("]")]), text[text.index("]") + 1 :].strip()
                except ValueError:
                    pass
            type_ = "alert" if text.lstrip().startswith(("🚨", "🔴", "ALERT", "alert:")) else "info"
            await ingest(b, type_, text, "telegram", hop)
        return
    if user.id not in ALLOWED:
        return await msg.reply_text(f"Це приватний бот. Ваш ID: {user.id}")
    chat = update.effective_chat.id
    # 2) пересилання від власника
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        if AWAIT.get(chat, (None,))[0] == "bkch" and getattr(origin, "chat", None) is not None and origin.chat.type == "channel":
            AWAIT.pop(chat, None)
            BK.set_channel(origin.chat.id, origin.chat.title or str(origin.chat.id))
            return await show(update, *bkui.scr_backup_target(BK, "channel", lambda: None), edit=False)
        sender = getattr(origin, "sender_user", None)
        text = msg.text or msg.caption or ""
        if sender is not None and sender.is_bot:
            b = bot_by_tg(user_id=sender.id, username=sender.username)
            if not b:
                return await msg.reply_text(f"Бот @{sender.username} ще не доданий. «🤖 Боти» → «➕ Додати бота», потім перешліть знову.")
            eid, why = await ingest(b, "info", text, "forward")
            return await msg.reply_text("📥 Додано до подій бота." if eid else f"Не додано: {why}")
        return await msg.reply_text("Пересилайте сюди повідомлення від ботів — я додам їх до «📥 Вхідних».")
    text = (msg.text or "").strip()
    routes = L.reply_routes(chat)
    pressed = L.unrelabel(chat, text)
    if pressed in routes:
        AWAIT.pop(chat, None)
        return await open_data(update, ctx, routes[pressed], edit=False)
    if text and CM.wants_text(chat):
        toast, scr = await CM.on_text(chat, text)
        if toast:
            await msg.reply_text("✅ " + toast)
        return await show(update, *scr, edit=False)
    if chat in AWAIT:
        return await on_awaited(update, ctx, text)
    await show(update, *scr_home(chat), edit=False)


async def on_awaited(update, ctx, text):
    chat = update.effective_chat.id
    act, data = AWAIT.get(chat)
    if act in ("lren", "bgnew", "brl"):
        AWAIT.pop(chat, None)
        screen, tip = handle_editor_text(L, chat, act, data, text)
        await update.message.reply_text(tip, reply_markup=menu_kb(chat))
        return await show(update, *screen, edit=False)
    if act.startswith("add_"):
        return await add_bot_from_text(update, ctx, act[4:], text)
    AWAIT.pop(chat, None)
    if act in ("mst", "msw", "rwadd"):
        try:
            if act == "msw":
                m = re.fullmatch(r"\s*(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})\s*", text)
                if not m:
                    raise ValueError(text)
                g = gsched()
                g["window"] = [f"{int(x.replace('.', ':').split(':')[0]):02d}:{x.replace('.', ':').split(':')[1]}" for x in m.groups()]
                set_gsched(g)
            else:
                times = parse_times(text)
                if act == "mst":
                    g = gsched()
                    g["times"] = times
                    set_gsched(g)
                else:
                    uniq = {(x["t"], tuple(x["d"])): x for x in NODE.wake_times() + times}
                    NODE.set("wake", sorted(uniq.values(), key=lambda x: x["t"]))
        except ValueError as e:
            ask(chat, act)
            return await update.message.reply_text(f"Не зрозумів «{e}». Спробуйте ще раз або «Скасувати».", reply_markup=kb([CANCEL_ROW]))
        await sync_manager_wakes()
        if act != "rwadd":
            await push_all_scheds()
        return await show(update, *(scr_wake(chat) if act == "rwadd" else scr_monitor(chat)), edit=False)
    if act in ("bsd", "bsw", "bswin", "bkd", "bkw"):
        import re as _re

        times = _re.findall(r"(\d{1,2})[:.](\d{2})", text)
        if not times or (act == "bswin" and len(times) < 2):
            AWAIT[chat] = (act, data)
            return await update.message.reply_text("Не зрозумів час. Приклад: 09:00" + (" або 08:00-23:00" if act == "bswin" else ""))
        hm = [f"{int(h):02d}:{m}" for h, m in times]
        if act in ("bkd", "bkw"):
            target = data if act == "bkd" else data[0]
            BK.set_sched(target, {"daily": hm[0]} if act == "bkd" else {"weekly": [data[1], hm[0]]})
            return await show(update, *bkui.scr_bk_sched(BK, target), edit=False)
        bid = data if act != "bsw" else data[0]
        s_ = bsched(bot_get(bid))
        if act == "bsd":
            s_.pop("weekly", None)
            s_.pop("every", None)
            s_["daily"] = hm[0]
        elif act == "bsw":
            s_.pop("daily", None)
            s_.pop("every", None)
            s_["weekly"] = [data[1], hm[0]]
        else:
            s_["window"] = hm[:2]
        set_bsched(bid, s_)
        await push_sched(bot_get(bid))
        return await show(update, *scr_sched(chat, bid), edit=False)
    if act == "bren":
        name, _, note = text.partition("\n")
        bot_update(data, name=name.strip()[:60], note=note.strip()[:300])
        return await show(update, *scr_bot(chat, data), edit=False)
    if act == "btok":
        b = bot_get(data)
        try:
            await update.message.delete()
        except Exception:  # noqa: BLE001
            pass
        s = secrets_of(b)
        try:
            if b["kind"] == "telegram":
                i = await P.tg_info(text)
                if b["ext_id"] and str(i["id"]) != b["ext_id"]:
                    return await update.effective_chat.send_message("⚠️ Це токен іншого бота.")
            s["token"] = text.strip()
            await P.info(b["kind"], s)
        except P.PlatformError as e:
            return await update.effective_chat.send_message(f"⚠️ Токен не працює: {e}")
        bot_update(b["id"], secret=seal(s))
        await update.effective_chat.send_message("✅ Токен замінено, повідомлення з ним видалено.")
        return await show(update, *scr_bot(chat, b["id"]), edit=False)
    if act == "bcmd":
        if not text.startswith("http"):
            return await update.message.reply_text("Адреса має починатися з http(s)://")
        bot_update(data, cmd_url=text)
        return await show(update, *scr_conn(chat, data), edit=False)
    if act == "bpe":
        bid, field = data
        b = bot_get(bid)
        try:
            await P.set_profile(b["kind"], secrets_of(b), field, text)
            await update.message.reply_text("✅ Змінено")
        except P.PlatformError as e:
            await update.message.reply_text(f"⚠️ {e}")
        return await show(update, *(await scr_profile(chat, bid)), edit=False)
    if act == "bce":
        b = bot_get(data)
        items = []
        for line in text.splitlines():
            if not line.strip():
                continue
            if b["kind"] == "instagram":
                items.append((line.strip()[:40], line.strip()))
            else:
                c, _, d = line.replace("—", "-").partition("-")
                items.append((c.strip().lstrip("/"), d.strip() or c.strip()))
        try:
            await P.set_commands(b["kind"], secrets_of(b), items)
            await update.message.reply_text(f"✅ Збережено: {len(items)}")
        except P.PlatformError as e:
            await update.message.reply_text(f"⚠️ {e}")
        return await show(update, *(await scr_commands(chat, b["id"])), edit=False)
    if act == "bas":
        b = bot_get(data)
        try:
            await P.send_as(b["kind"], secrets_of(b), chat, text)
            return await update.message.reply_text(f"✅ Бот «{b['name']}» надіслав вам повідомлення.")
        except P.PlatformError as e:
            return await update.message.reply_text(f"⚠️ {e}\nВідкрийте чат із цим ботом і натисніть «Старт», тоді він зможе вам писати.")
    if act == "bsend":
        to = bot_get(data)
        eid, why = event_add(None, "command", text, "manager")
        if not eid:
            return await update.message.reply_text(f"⚠️ {why}")
        ok, detail = await deliver(event_get(eid), to)
        return await update.message.reply_text(f"✅ Передано боту «{to['name']}»" if ok else f"⚠️ {detail}")
    if act == "rule_contains":
        data["contains"] = text[:80]
        AWAIT[chat] = ("rule", data)
        return await show(update, *scr_pick_bot("🔀 4/4 · кому передавати", "m:rn4", exclude=data.get("from")), edit=False)
    await show(update, *scr_home(chat), edit=False)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if update.effective_user.id not in ALLOWED:
        return await q.answer("Доступ закритий", show_alert=True)
    await open_data(update, ctx, q.data)


async def on_backup_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED:
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return await update.message.reply_text("Файл понад 20 МБ — відновіть із хмари.")
    blob = bytes(await (await doc.get_file()).download_as_bytearray())
    await ask_restore(ctx.bot, update.effective_chat.id, blob)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ALLOWED:
        return await update.message.reply_text(f"Це приватний бот. Ваш ID: {user.id}")
    chat = update.effective_chat.id
    await update.message.reply_text("🧭 Головний бот на зв'язку. Меню — внизу екрана.", reply_markup=menu_kb(chat))
    L.visit(chat, "m:home")
    await show(update, *scr_home(chat), edit=False)


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """👆 Перевірити всіх ботів зараз (для схеми «За наказом» — і для будь-якої іншої)."""
    if not update.effective_user or update.effective_user.id not in ALLOWED:
        return
    msg = await update.message.reply_text("🔄 Перевіряю всіх ботів…")
    for b in bots_list():
        await check_bot(b)
    L.visit(update.effective_chat.id, "m:mon")
    await show(update, *scr_monitor(update.effective_chat.id), edit=False)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED:
        return
    chat = update.effective_chat.id
    await update.message.reply_text("⌨️", reply_markup=menu_kb(chat))
    L.visit(chat, "m:home")
    await show(update, *scr_home(chat), edit=False)


# ---------------------------------------------------------------- HTTP: скринька, сигнал, OAuth


async def http_in(request: Request):
    if request.path_params["secret"] != SECRET:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if request.method == "GET":
        body = dict(request.query_params)
    else:
        try:
            body = await request.json()
        except ValueError:
            body = dict(await request.form())
    b = bot_by_key(body.get("key") or request.headers.get("X-Hub-Key"))
    if not b:
        return JSONResponse({"ok": False, "error": "невідомий ключ бота"}, status_code=403)
    NODE.touch()
    if body.get("type") == "plan" and isinstance(body.get("plan"), dict):
        NODE.touch()
        bot_update(b["id"], plan=json.dumps(body["plan"], ensure_ascii=False), last_seen=time.time(),
                   **({"cmd_url": body["plan"]["cmd_url"]} if str(body["plan"].get("cmd_url", "")).startswith("http") else {}))
        event_add(b["id"], "sleep", "📅 Новий план: " + plan_text(body["plan"]), "api", 0)
        return JSONResponse({"ok": True, "plan": manager_plan()})
    eid, why = await ingest(b, body.get("type", "info"), body.get("text", ""), "api", int(body.get("hop") or 0), until=body.get("until"))
    return JSONResponse({"ok": bool(eid) or body.get("type") == "sleep", "event_id": eid, "error": why, "plan": manager_plan()})


async def http_hb(request: Request):
    if request.path_params["secret"] != SECRET:
        return JSONResponse({"ok": False}, status_code=404)
    try:
        body = await request.json()
    except ValueError:
        body = dict(request.query_params)
    b = bot_by_key(body.get("key"))
    if not b:
        return JSONResponse({"ok": False, "error": "невідомий ключ"}, status_code=403)
    upd = {"last_seen": time.time(), "sleep_until": None, "sleep_note": None}  # сигнал «я живий» = прокинувся
    if body.get("cmd_url"):
        upd["cmd_url"] = body["cmd_url"]
    bot_update(b["id"], **upd)
    if b["status"] in ("down", "unknown") and b["kind"] == "http":
        await check_bot(bot_get(b["id"]))
    return JSONResponse({"ok": True})


async def http_backup_cb(request: Request):
    cloud = request.path_params["cloud"]
    ok, text = await BK.callback(cloud, request.query_params.get("code", ""), request.query_params.get("state", ""))
    await notify_text(("✅ " if ok else "⚠️ ") + esc(text))
    return HTMLResponse(f"<meta name=viewport content='width=device-width'><h2 style='font-family:sans-serif'>{esc(text)}</h2>")


async def http_root(request):
    return PlainTextResponse("Головний бот працює.")


routes = [
    Route("/", http_root),
    Route("/{secret}/in", http_in, methods=["GET", "POST"]),
    Route("/{secret}/hb", http_hb, methods=["GET", "POST"]),
    Route("/backup/{cloud}/callback", http_backup_cb),
]
# ---------------------------------------------------------------- ⚡ режим роботи головного бота (як у ШІ-бота)
NODE = Node(os.path.dirname(os.path.abspath(DB_PATH)) or ".", DB_PATH, TZ, fname="node-manager.json", prefix="MANAGER_")
WEBHOOK_BASE = (env("MANAGER_PUBLIC_URL") or PUBLIC_URL or (f"https://{env('RAILWAY_PUBLIC_DOMAIN')}" if env("RAILWAY_PUBLIC_DOMAIN") else "")).rstrip("/")
HOOK_SECRET = hashlib.sha256(("tgm:" + SECRET).encode()).hexdigest()[:40]
HOOK_PATH = f"/tg/{HOOK_SECRET[:16]}"
WAKE_PATH = f"/wake/{HOOK_SECRET[16:32]}"
DEFAULT_MODE = "sleep" if env("TG_WEBHOOK").lower() in ("1", "true", "yes") else "always"
HOOK_ON = {"on": False}


def parse_times(text):
    """«09:00, 18:30», «08:00 пн,ср,пт», «кожні 4 год» -> [{"t", "d"}]."""
    t = text.strip().lower()
    m = re.match(r"^кожн\w*\s+(\d{1,2})\s*(год|h)", t)
    if m:
        n = max(1, min(12, int(m.group(1))))
        return [{"t": f"{h:02d}:00", "d": []} for h in range(0, 24, n)]
    out = []
    for part in re.split(r"[;\n]+|,(?=\s*\d)", t):
        mm = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*([а-яіїє,\s]*)$", part)
        if not mm or int(mm.group(1)) > 23 or int(mm.group(2)) > 59:
            raise ValueError(part.strip() or text)
        days = [RUN_WEEK.index(d.strip()) for d in mm.group(3).split(",") if d.strip() in RUN_WEEK]
        out.append({"t": f"{int(mm.group(1)):02d}:{mm.group(2)}", "d": sorted(set(days))})
    return out


def wake_url():
    return WEBHOOK_BASE + WAKE_PATH if WEBHOOK_BASE else ""


def manager_wake_times():
    """Коли будити головного бота: власні години + години перевірок (якщо схема «🗓 За розкладом»)."""
    out = list(NODE.wake_times())
    if NODE.get("wake_checks", True) and gscheme() == "times":
        out += global_rule()["times"]
    uniq = {(x["t"], tuple(x.get("d", []))): x for x in out}
    return sorted(uniq.values(), key=lambda x: x["t"])


async def flush_held(b, force=False):
    """Віддати ботові повідомлення, що чекали, поки він спав."""
    held = json.loads(b.get("held") or "[]") if b else []
    if not held or (sleeping(b) is not None and not force):
        return 0
    bot_update(b["id"], held=None)
    n = 0
    for eid in held:
        e = event_get(eid)
        if e:
            e = dict(e, _force=True)
            await deliver(e, bot_get(b["id"]))
            n += 1
    return n


def manager_plan():
    """План сну головного бота — його бачать усі підключені боти."""
    mode = NODE.mode(DEFAULT_MODE)
    if mode != "always" and not HOOK_ON["on"]:
        mode = "always"
    p = {"name": "🧭 Головний бот", "mode": mode, "window": 15, "tz": "Europe/Kyiv",
         "wake": manager_wake_times() if mode == "timer" and NODE.cron_key() else [], "wake_url": wake_url(),
         "inbox": f"{WEBHOOK_BASE}/{SECRET}" if WEBHOOK_BASE else ""}  # куди писати головному (міняється, якщо він переїхав на інший сервер)
    p["v"] = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]
    return p


async def push_all_plans(force=False):
    """Мій план змінився — розсилаю всім ботам, що приймають команди (їхні плани приходять у відповідь)."""
    p = manager_plan()
    if not force and NODE.get("plan_sent") == p["v"]:
        return 0
    n = 0
    for b in bots_list():
        if b.get("cmd_url"):
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.post(b["cmd_url"], json={"key": b["inbox_key"], "type": "plan", "plan": p, "text": plan_text(p), "from": "головний бот"})
                bp = (r.json() or {}).get("plan") if r.status_code == 200 else None
                if isinstance(bp, dict):
                    bot_update(b["id"], plan=json.dumps(bp, ensure_ascii=False), last_seen=time.time())
                n += 1
            except (httpx.HTTPError, ValueError):
                pass
    NODE.set("plan_sent", p["v"])
    return n


async def wake_bot(b):
    """Різко розбудити бота: його адреса пробудження з плану, або «ти живий?» на адресу команд."""
    p = bplan(b) or {}
    if p.get("wake_url"):
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.get(p["wake_url"])
            if r.status_code == 200:
                bot_update(b["id"], last_seen=time.time())
                return True
        except httpx.HTTPError:
            pass
    return bool(await ping_bot(b))


async def push_all_scheds():
    """Новий графік перевірок — одразу всім ботам, що приймають команди: вони підлаштують свої «я живий» і пробудження."""
    for b in bots_list():
        if b.get("cmd_url") and b["monitor"]:
            await push_sched(b)


async def sync_manager_wakes(force=False):
    try:
        await push_all_plans()
    except Exception:  # noqa: BLE001
        log.exception("розсилка плану")
    if not NODE.cron_key() or not wake_url():
        return None
    sig = json.dumps([NODE.mode(DEFAULT_MODE), manager_wake_times()])
    if not force and sig == NODE.get("cron_sig"):
        return None
    saved = NODE.get("wake", [])
    try:
        NODE.set("wake", manager_wake_times())  # sync_cron бере NODE.wake_times()
        res = await NODE.sync_cron(wake_url(), [])
        NODE.set("cron_sig", sig)
        NODE.set("cron_error", None)
        return res
    except Exception as e:  # noqa: BLE001
        NODE.set("cron_error", str(e)[:200])
        return None
    finally:
        NODE.set("wake", saved)


async def apply_receiving():
    if not app:
        return
    up = app.updater
    if not NODE.active:
        if up and up.running:
            await up.stop()
        HOOK_ON["on"] = False
        return
    want_hook = NODE.mode(DEFAULT_MODE) in ("sleep", "timer") and bool(WEBHOOK_BASE)
    if want_hook:
        if up and up.running:
            await up.stop()
        await app.bot.set_webhook(WEBHOOK_BASE + HOOK_PATH, secret_token=HOOK_SECRET, allowed_updates=Update.ALL_TYPES, max_connections=10)
        HOOK_ON["on"] = True
    else:
        if up and not up.running:
            await app.bot.delete_webhook(drop_pending_updates=False)
            await up.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
        HOOK_ON["on"] = False


def scr_runmode(chat):
    raw, mode = NODE.raw_mode(DEFAULT_MODE), NODE.mode(DEFAULT_MODE)
    real = "вебхук (може спати)" if HOOK_ON["on"] else "опитування Telegram (не спить)"
    srv = "🆓 безкоштовний сервер" if NODE.cost() == "free" else "💳 сервер, платний за хвилини (Railway)"
    lines = ["⚡ <b>Режим роботи головного бота</b>", f"Зараз: <b>{RUN_MODES[raw][0]}" + (f" → {RUN_MODES[mode][0]}" if raw == "auto" else "") + f"</b> · {real}",
             f"Цей сервер: {srv}. Режим зберігається окремо для кожного сервера.", ""]
    for k, (title, desc) in RUN_MODES.items():
        lines.append(f"{'●' if k == raw else '○'} <b>{title}</b> — {esc(desc)}")
    lines.append("")
    sc = gscheme()
    lines.append(f"📡 Схема моніторингу: <b>{SCHEMES[sc][0]}</b> ({esc(schedule_text(global_rule()))}).")
    if mode != "always" and sc in ("always", "window") and gsched().get("every", CHECK_EVERY) < 900:
        lines.append("⚠️ Перевірки частіше ніж раз на 15 хв не дадуть головному боту заснути. Для сну оберіть схему «🗓 За розкладом» "
                     "або «👆 За наказом» — тоді в режимі «⏰ Сон + будильник» він прокидатиметься саме на перевірки.")
    if mode in ("sleep", "timer") and not WEBHOOK_BASE:
        lines.append("⚠️ Для сну потрібна адреса: MANAGER_PUBLIC_URL або Railway → Networking → Generate Domain.")
    lines.append("Railway: Serverless — " + ("<b>вимкнено</b>." if mode == "always" else "<b>увімкнено</b>."))
    rows = [[(("● " if k == raw else "") + RUN_MODES[k][0], f"m:rmset:{k}")] for k in RUN_MODES]
    if mode == "timer" or raw == "auto":
        rows.append([("⏰ Будильник", "m:rw")])
    rows.append([("📡 Схема моніторингу", "m:mon"), ("🛰 Сервери", "m:cl")])
    return "\n".join(lines), rows


def scr_wake(chat):
    w = NODE.wake_times()
    lines = ["⏰ <b>Будильник головного бота</b> (київський час)",
             "Прокинувшись, бот перевіряє тих, кого пора, приймає повідомлення від ботів, що накопичились, і знову засинає.",
             "\nСвої години: " + esc(NODE.wake_text()),
             f"🔗 Прокидатися на перевірки за розкладом: <b>{'так' if NODE.get('wake_checks', True) else 'ні'}</b>"
             + (" (схема зараз не «🗓 За розкладом»)" if gscheme() != "times" else "")]
    all_t = manager_wake_times()
    lines.append("Разом будитиму о: " + (", ".join(x["t"] + (f" ({','.join(RUN_WEEK[d] for d in x['d'])})" if x.get("d") else "") for x in all_t) or "—"))
    if NODE.cron_key():
        lines.append(f"\n✅ cron-job.org: пробуджень — {len(NODE.get('cron_jobs', {}))}" + (f"\n⚠️ {esc(NODE.get('cron_error'))}" if NODE.get("cron_error") else ""))
    else:
        lines.append("\nХто будитиме: cron-job.org — ключ <code>CRONJOB_API_KEY</code> (бот усе налаштує сам) або вручну завдання з адресою:\n"
                     f"<code>{esc(wake_url() or 'спершу адреса бота')}</code>")
    rows = [[(f"✖️ {x['t']}" + (f" {','.join(RUN_WEEK[d] for d in x['d'])}" if x.get("d") else ""), f"m:rwdel:{i}")] for i, x in enumerate(w)]
    rows.append([("➕ Додати час", "m:rwadd"), (("✅" if NODE.get("wake_checks", True) else "▫️") + " На перевірки", "m:rwt")])
    if NODE.cron_key():
        rows.append([("🔄 Синхронізувати", "m:rwsync")])
    rows.append([("« Режим роботи", "m:rm")])
    return "\n".join(lines), rows


def scr_cluster(chat):
    return cluster_screen(NODE, esc, TZ)


async def route_runmode(update, chat, a, arg):
    toast = None
    if a == "cl":
        return None, scr_cluster(chat)
    if a == "clrun":
        await cluster_tick(force=True)
        return "Перевірено", scr_cluster(chat)
    if a in ("clpref", "clsel", "clpool"):
        toast = await cluster_route(NODE, a, arg)
        await cluster_tick(force=True)
        return toast, scr_cluster(chat)
    if a == "rm":
        return None, scr_runmode(chat)
    if a == "rmset" and arg in RUN_MODES:
        NODE.set("mode", arg)
        try:
            await apply_receiving()
        except Exception as e:  # noqa: BLE001
            toast = f"Не вдалося перемкнути: {e}"[:190]
        await sync_manager_wakes(force=True)
        return toast or RUN_MODES[arg][0], scr_runmode(chat)
    if a == "rw":
        return None, scr_wake(chat)
    if a == "rwadd":
        ask(chat, "rwadd")
        await update.effective_chat.send_message("⏰ Коли будити? <code>09:00</code> · <code>9:00, 18:30</code> · <code>08:00 пн,ср,пт</code> · <code>кожні 4 год</code>",
                                                parse_mode=ParseMode.HTML, reply_markup=kb([CANCEL_ROW]))
        return None, None
    if a == "rwdel":
        w = NODE.wake_times()
        if arg and arg.isdigit() and int(arg) < len(w):
            w.pop(int(arg))
            NODE.set("wake", w)
            await sync_manager_wakes()
        return None, scr_wake(chat)
    if a == "rwt":
        NODE.set("wake_checks", not NODE.get("wake_checks", True))
        await sync_manager_wakes()
        return None, scr_wake(chat)
    if a == "rwsync":
        res = await sync_manager_wakes(force=True)
        return (f"Додано {res[0]}, прибрано {res[1]}" if res else (NODE.get("cron_error") or "Без змін"))[:190], scr_wake(chat)
    return None, scr_runmode(chat)


async def http_tg(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != HOOK_SECRET or not app:
        return PlainTextResponse("forbidden", status_code=403)
    try:
        upd = Update.de_json(await request.json(), app.bot)
    except ValueError:
        return PlainTextResponse("bad", status_code=400)
    t = asyncio.create_task(app.process_update(upd))
    BG.add(t)
    t.add_done_callback(BG.discard)
    return PlainTextResponse("ok")


async def http_wake(request: Request):
    NODE.touch()

    async def run():
        await cluster_tick(force=True)
        if NODE.active:
            await monitor_tick()

    t = asyncio.create_task(run())  # прокинувся за будильником — перевіряю, кого пора
    BG.add(t)
    t.add_done_callback(BG.discard)
    return PlainTextResponse("ok")


BG = set()

routes += [Route(HOOK_PATH, http_tg, methods=["POST"]), Route(WAKE_PATH, http_wake, methods=["GET", "POST", "HEAD"])]
routes += NODE.routes(lambda: on_deactivate())
web = Starlette(routes=routes)

app = None
if TOKEN:
    app = Application.builder().token(TOKEN).build()
    CM.install_tg(app, pass_bots=True)  # 🏘 групи й канали; повідомлення інших ботів ідуть далі — у «📥 Вхідні»
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("check", cmd_check))

    async def _touch(update, ctx):
        NODE.touch()

    from telegram.ext import TypeHandler as _TH
    app.add_handler(_TH(Update, _touch), group=-3)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.FileExtension("zip") | filters.Document.FileExtension("gz") | filters.Document.FileExtension("enc") | filters.Document.FileExtension("db"), on_backup_file))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_text))


async def main():
    init_db()
    if len(SECRET) < 16:
        raise SystemExit("Задайте MANAGER_SECRET — довгий випадковий рядок (від 16 символів).")
    if not ALLOWED:
        raise SystemExit("Задайте ALLOWED_USERS — ваш Telegram ID (його покаже бот у відповідь на будь-яке повідомлення).")
    server = uvicorn.Server(uvicorn.Config(web, host="0.0.0.0", port=PORT, log_level="info"))
    await app.initialize()
    await app.bot.set_my_commands([BotCommand("menu", "Меню"), BotCommand("start", "Почати")])
    await app.start()
    await cluster_tick(force=True)  # 🛰 спершу з'ясовую, чи цей сервер має працювати
    await apply_receiving()  # ⚡ режим: опитування або вебхук (може спати)
    await sync_manager_wakes()
    task = asyncio.create_task(monitor_loop())
    try:
        await server.serve()
    finally:
        task.cancel()
        if app.updater and app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
