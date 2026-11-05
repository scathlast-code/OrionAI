"""
Telegram-бот з кількома безкоштовними ШІ одночасно + конектор (MCP) для зовнішніх ШІ-чатів.

  - Telegram -> бот -> обрана модель (Gemini, Groq, Mistral, NVIDIA, Ollama Cloud,
    OpenRouter, Cloudflare, Cohere, ваша Ollama). Вибір кнопками (/model),
    разово (/ask groq текст) або «Авто» з перемиканням, коли модель недоступна.
  - Бот пояснює, чому модель не відповіла, рахує використання й залишки лімітів (/limits),
    ставить моделі на паузу після ліміту і сповіщає власника про проблеми.
  - Вбудована довідка (/help) працює без звернення до ШІ: тексти в help_texts.py.
  - Зовнішній ШІ (Le Chat, Gemini CLI тощо) -> конектор -> бот: переписка, повідомлення в Telegram, нотатки, питання моделям.
"""

import asyncio
import base64
import contextvars
import secrets
import hashlib
import html
import io
import json
import math
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from telegram import (
    BotCommand,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import canva
from services import NOT_SUPPORTED, SERVICES, ServiceError, Services
from hubclient import hub, plan_state, plan_text, schedule_text
from backups import CLOUDS, TARGET_TITLES, Backups, BackupError, human_size
import backups as bkui
from uilayout import Layout, handle_editor, handle_editor_text
import help_texts as H
import media
from community import Community
from priority import MARK, Ranks, WorkQueue
from cluster import MODES as RUN_MODES, WEEK as RUN_WEEK, Node, cluster_route, cluster_screen

env = os.environ.get
esc = html.escape

# ---------------------------------------------------------------- налаштування

TELEGRAM_TOKEN = env("TELEGRAM_TOKEN", "")
ALLOWED_USERS = [int(x) for x in env("ALLOWED_USERS", "").replace(" ", "").split(",") if x]
MCP_SECRET = env("MCP_SECRET", "")
DB_PATH = env("DB_PATH", "data/bot.db")
HISTORY_LIMIT = int(env("HISTORY_LIMIT", "30"))
MAX_TOKENS = int(env("MAX_TOKENS", "2000"))
REQUEST_TIMEOUT = float(env("REQUEST_TIMEOUT", "90"))
PORT = int(env("PORT", "8000"))
TZ = ZoneInfo(env("BOT_TZ", "Europe/Kyiv"))
ALERT_EVERY = 6 * 3600  # не частіше ніж раз на 6 год для кожної моделі й причини
SYSTEM_PROMPT = env(
    "SYSTEM_PROMPT",
    "Ти — особистий помічник у Telegram. Відповідай мовою користувача, коротко й по суті, "
    "без Markdown-розмітки (Telegram показує її як звичайний текст). "
    "Коли користувач просить щось запам'ятати або записати — збережи нотатку інструментом save_note. "
    "Коли питає про збережене — шукай інструментом search_notes. "
    "Коли просить намалювати чи згенерувати картинку — виклич generate_image з детальним описом англійською. "
    "Коли просить музику — generate_music. Коли просить зробити щось у Canva (дизайн, пост, презентацію, "
    "експорт) — користуйся інструментами з назвою canva__…; готові зображення й файли бот надішле сам.",
)
SYSTEM_PROMPT += (
    "\nУ цій розмові раніше могли відповідати інші моделі ШІ (бот перемикається між ними). "
    "Вважай усі попередні відповіді асистента своїми й продовжуй з того самого місця, нічого не повторюючи без потреби."
)
# Режим «спати, коли нічого не відбувається» (Railway Serverless): Telegram сам будить бота вебхуком,
# а бот не тримає постійного з'єднання з Telegram, тож через 10 хв тиші Railway його присипляє і не рахує гроші.
TG_WEBHOOK = env("TG_WEBHOOK", "").lower() in ("1", "true", "yes", "так")
WEBHOOK_BASE = (env("PUBLIC_URL", "") or (f"https://{env('RAILWAY_PUBLIC_DOMAIN')}" if env("RAILWAY_PUBLIC_DOMAIN", "") else "")).rstrip("/")
TASK_CHECK_EVERY = int(env("TASK_CHECK_EVERY", "60"))
HOOK_SECRET = hashlib.sha256(("tg:" + env("MCP_SECRET", "")).encode()).hexdigest()[:40]
HOOK_PATH = f"/tg/{HOOK_SECRET[:16]}"
WAKE_PATH = f"/wake/{HOOK_SECRET[16:32]}"  # як часто перевіряти відкладені завдання, с
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Каталог сервісів. Модель змінюється змінною <ID>_MODEL, адреса — <ID>_BASE_URL.
# limit: відомий безкоштовний ліміт запитів для оцінки залишку (змінюється <ID>_LIMIT_DAY / _LIMIT_MONTH).
CATALOG = {
    "gemini": {
        "title": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-2.5-flash",
        "free": True,
        "site": "aistudio.google.com",
        "models_url": "ai.google.dev/gemini-api/docs/models",
        "limit": ("day", 250),
    },
    "groq": {
        "title": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model": "openai/gpt-oss-120b",
        "free": True,
        "site": "console.groq.com",
        "models_url": "console.groq.com/docs/models",
        "limit": ("day", 1000),
    },
    "mistral": {
        "title": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "model": "mistral-small-latest",
        "free": True,
        "site": "console.mistral.ai",
        "models_url": "docs.mistral.ai/getting-started/models",
        "limit": None,
    },
    "nvidia": {
        "title": "NVIDIA",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_env": "NVIDIA_API_KEY",
        "model": "meta/llama-3.3-70b-instruct",
        "free": True,
        "site": "build.nvidia.com",
        "models_url": "build.nvidia.com/models",
        "limit": None,
    },
    "ollama_cloud": {
        "title": "Ollama Cloud",
        "base_url": "https://ollama.com/v1",
        "key_env": "OLLAMA_CLOUD_API_KEY",
        "model": "gpt-oss:120b",
        "free": True,
        "site": "ollama.com",
        "models_url": "ollama.com/search?c=cloud",
        "limit": None,
    },
    "openrouter": {
        "title": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "model": "openai/gpt-oss-20b:free",
        "free": True,
        "site": "openrouter.ai",
        "models_url": "openrouter.ai/models",
        "limit": ("day", 50),
    },
    "cloudflare": {
        "title": "Cloudflare",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/v1",
        "key_env": "CF_API_TOKEN",
        "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "free": True,
        "site": "dash.cloudflare.com",
        "models_url": "developers.cloudflare.com/workers-ai/models",
        "limit": None,
    },
    "cohere": {
        "title": "Cohere",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "key_env": "COHERE_API_KEY",
        "model": "command-a-03-2025",
        "free": True,
        "site": "dashboard.cohere.com",
        "models_url": "docs.cohere.com/docs/models",
        "limit": ("month", 1000),
    },
    "ollama_local": {
        "title": "Моя Ollama",
        "base_url": "{OLLAMA_URL}/v1",
        "key_env": "OLLAMA_URL",
        "model": "gemma3:4b",
        "free": True,
        "site": "ollama.com",
        "models_url": "ollama.com/search",
        "limit": None,
    },
}

DEFAULT_ORDER = "gemini,groq,mistral,nvidia,ollama_cloud,openrouter,cloudflare,cohere,ollama_local"


def build_providers():
    providers = {}
    for pid, c in CATALOG.items():
        if pid == "ollama_local":
            url = env("OLLAMA_URL", "").rstrip("/")
            if not url:
                continue
            base, key = c["base_url"].replace("{OLLAMA_URL}", url), env("OLLAMA_API_KEY", "ollama")
        elif pid == "cloudflare":
            key, acc = env("CF_API_TOKEN", ""), env("CF_ACCOUNT_ID", "")
            if not (key and acc):
                continue
            base = c["base_url"].replace("{CF_ACCOUNT_ID}", acc)
        else:
            key = env(c["key_env"], "")
            if not key:
                continue
            base = c["base_url"]
        limit = c["limit"]
        up = pid.upper()
        if env(f"{up}_LIMIT_DAY"):
            limit = ("day", int(env(f"{up}_LIMIT_DAY"))) if int(env(f"{up}_LIMIT_DAY")) > 0 else None
        elif env(f"{up}_LIMIT_MONTH"):
            limit = ("month", int(env(f"{up}_LIMIT_MONTH"))) if int(env(f"{up}_LIMIT_MONTH")) > 0 else None
        providers[pid] = {
            **c,
            "id": pid,
            "base_url": env(f"{up}_BASE_URL", base).rstrip("/"),
            "key": key,
            "model": env(f"{up}_MODEL", c["model"]),
            "limit": limit,
        }
    return providers


PROVIDERS = build_providers()
AUTO_ORDER = [
    p
    for p in env("FALLBACK_ORDER", DEFAULT_ORDER).replace(" ", "").split(",")
    if p in PROVIDERS
]

def custom_rows():
    try:
        with closing(db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM custom_providers ORDER BY created").fetchall()]
    except sqlite3.OperationalError:  # таблиці ще немає
        return []


def refresh_providers():
    """Перебудовує список моделей: вбудовані з ключів середовища + додані в боті («🧩 Мої моделі»)."""
    base = build_providers()
    customs = []
    for r in custom_rows():
        if not r["enabled"]:
            continue
        host = re.sub(r"^https?://", "", r["base_url"]).split("/")[0]
        base[r["id"]] = {
            "id": r["id"], "title": r["title"], "base_url": r["base_url"].rstrip("/"), "key": r["api_key"],
            "model": r["model"], "free": True, "site": host, "models_url": r["base_url"].rstrip("/") + "/models",
            "key_env": "ключ у боті (🤖 Моделі → 🧩 Мої моделі)", "limit": None, "custom": True,
        }
        customs.append(r["id"])
    PROVIDERS.clear()
    PROVIDERS.update(base)
    AUTO_ORDER[:] = [p for p in env("FALLBACK_ORDER", DEFAULT_ORDER).replace(" ", "").split(",") if p in PROVIDERS] + customs


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


def now():
    return datetime.now(TZ)


def today():
    return now().strftime("%Y-%m-%d")


def hhmm(ts):
    d = datetime.fromtimestamp(ts, TZ)
    return d.strftime("%H:%M") if d.date() == now().date() else d.strftime("%d.%m %H:%M")


def left(ts):
    """Скільки лишилось до моменту: «12 хв», «1 год 5 хв», «2 дн»."""
    s = max(0, int(ts - time.time()))
    if s < 60:
        return "менше хвилини"
    if s < 3600:
        return f"{s // 60} хв"
    if s < 86400:
        return f"{s // 3600} год {s % 3600 // 60} хв"
    return f"{s // 86400} дн {s % 86400 // 3600} год"


def ago(ts):
    s = int(time.time() - ts)
    if s < 60:
        return "щойно"
    if s < 3600:
        return f"{s // 60} хв тому"
    if s < 86400:
        return f"{s // 3600} год тому"
    return f"{s // 86400} дн тому"


# ---------------------------------------------------------------- база даних


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con, con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'telegram',
                provider TEXT,
                in_context INTEGER NOT NULL DEFAULT 1,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'auto'
            );
            CREATE TABLE IF NOT EXISTS usage (
                provider TEXT NOT NULL,
                day TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider, day)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                msg_id INTEGER NOT NULL,          -- повідомлення користувача в messages
                content_json TEXT NOT NULL,       -- те, що отримає модель (текст або текст+фото)
                history_text TEXT NOT NULL,       -- як питання виглядає в історії
                provider TEXT,                    -- NULL = «Авто»
                status TEXT NOT NULL,             -- choice | deferred | done | cancelled
                reason TEXT,
                created REAL NOT NULL,
                last_try REAL
            );
            CREATE TABLE IF NOT EXISTS pstate (
                provider TEXT PRIMARY KEY,
                last_ok REAL,
                err_kind TEXT,
                err_text TEXT,
                err_ts REAL,
                fails INTEGER NOT NULL DEFAULT 0,
                cooldown_until REAL,
                cooldown_kind TEXT,
                rl_json TEXT,
                rl_ts REAL
            );
            """
        )
        cols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
        if "pending" not in cols:  # оновлення бази зі старої версії бота
            con.execute("ALTER TABLE messages ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")
        # після перезапуску (наприклад, із новим ключем) знімаємо паузи, що не пов'язані з лімітами
        con.execute(
            "UPDATE pstate SET cooldown_until=NULL WHERE cooldown_kind NOT IN ('rate_minute','rate_day','rate_month','rate')"
        )
    migrate_db()
    refresh_providers()


def kv_get(key):
    with closing(db()) as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key, value):
    with closing(db()) as con, con:
        if value is None:
            con.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            con.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


canva.kv_get, canva.kv_set = kv_get, kv_set
media.IMAGE_PROVIDERS["canva"]["ready"] = lambda: canva.connected() and (CHAT.get() is None or CHAT.get() in ALLOWED_USERS)
media.CANVA_IMAGE = canva.generate_image


def migrate_db():
    """Розмови, теки, нові поля завдань (і перенесення старої історії в «Першу розмову»)."""
    with closing(db()) as con, con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dialogs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                folder_id INTEGER,
                title TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            """
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS media (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, kind TEXT NOT NULL, "
            "file_id TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '', created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'active', "
            "pinned_at REAL, archived_at REAL, trashed_at REAL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, dialog_id INTEGER, prompt TEXT NOT NULL, "
            "status TEXT NOT NULL, kind TEXT, task_id INTEGER, provider TEXT, attempts TEXT, result TEXT, created REAL NOT NULL, updated REAL)"
        )
        dcols = [r[1] for r in con.execute("PRAGMA table_info(dialogs)")]
        for col, typ in (("summary", "TEXT"), ("summary_upto", "INTEGER")):
            if col not in dcols:
                con.execute(f"ALTER TABLE dialogs ADD COLUMN {col} {typ}")
        mcols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
        if "dialog_id" not in mcols:
            con.execute("ALTER TABLE messages ADD COLUMN dialog_id INTEGER")
        tcols = [r[1] for r in con.execute("PRAGMA table_info(tasks)")]
        for col, typ in (("dialog_id", "INTEGER"), ("folder_id", "INTEGER"), ("run_at", "REAL"), ("kind", "TEXT"), ("start_mode", "TEXT"), ("started_at", "REAL")):
            if col not in tcols:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                status TEXT NOT NULL,          -- pending | approved | blocked
                invite_code TEXT,
                created REAL NOT NULL,
                approved_at REAL
            );
            CREATE TABLE IF NOT EXISTS custom_providers (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preinvites (
                key TEXT PRIMARY KEY,          -- '@username' (малими літерами) або ID
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invites (
                code TEXT PRIMARY KEY,
                uses_left INTEGER,             -- NULL = без обмеження
                expires REAL,
                created REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        mcols = [r[1] for r in con.execute("PRAGMA table_info(members)")]
        if "can_view" not in mcols:
            con.execute("ALTER TABLE members ADD COLUMN can_view INTEGER NOT NULL DEFAULT 0")
        ncols = [r[1] for r in con.execute("PRAGMA table_info(notes)")]
        if "chat_id" not in ncols:
            con.execute("ALTER TABLE notes ADD COLUMN chat_id INTEGER")
            con.execute("UPDATE notes SET chat_id=?", (owner_id(),))
        for table in ("dialogs", "folders", "notes", "tasks"):
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            for col, typ in (("state", "TEXT NOT NULL DEFAULT 'active'"), ("archived_at", "REAL"), ("trashed_at", "REAL"), ("pinned_at", "REAL")):
                if col not in cols:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        for (chat_id,) in con.execute("SELECT DISTINCT chat_id FROM messages WHERE dialog_id IS NULL").fetchall():
            did = con.execute(
                "INSERT INTO dialogs(chat_id, title, created, updated) VALUES (?,?,?,?)",
                (chat_id, "Перша розмова", time.time(), time.time()),
            ).lastrowid
            con.execute("UPDATE messages SET dialog_id=? WHERE chat_id=? AND dialog_id IS NULL", (did, chat_id))


# ---------------------------------------------------------------- налаштування користувача

PREF_DEFAULTS = {
    "task_start": "auto",  # заплановані завдання: auto — бот запускає сам; manual — нагадує й чекає натискання
    "memory": HISTORY_LIMIT,  # скільки останніх повідомлень пам'ятає розмова
    "length": "normal",  # short | normal | long
    "temp": 0.7,  # креативність
    "shape": "square",  # формат картинок
    "slide": 4,  # секунд на слайд у відео
    "alerts": True,  # окремі сповіщення про проблеми моделей
    "footer": True,  # підпис моделі під відповіддю
    "dialog": None,  # поточна розмова
}
LENGTHS = {
    "short": (600, "Відповідай дуже коротко: 1–3 речення, якщо не просять інакше."),
    "normal": (2000, ""),
    "long": (4000, "Відповідай розгорнуто й детально, з прикладами."),
}


def prefs(chat_id):
    raw = kv_get(f"prefs:{chat_id}")
    p = dict(PREF_DEFAULTS)
    if raw:
        p.update(json.loads(raw))
    return p


def set_pref(chat_id, key, value):
    p = prefs(chat_id)
    p[key] = value
    kv_set(f"prefs:{chat_id}", json.dumps(p))


# ---------------------------------------------------------------- розмови й теки


def dialog_create(chat_id, title="Нова розмова", folder_id=None):
    with closing(db()) as con, con:
        did = con.execute(
            "INSERT INTO dialogs(chat_id, folder_id, title, created, updated) VALUES (?,?,?,?,?)",
            (chat_id, folder_id, title, time.time(), time.time()),
        ).lastrowid
    set_pref(chat_id, "dialog", did)
    return did


def dialog_get(did):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM dialogs WHERE id=?", (did,)).fetchone()
    return dict(row) if row else None


def dialog_update(did, **fields):
    with closing(db()) as con, con:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE dialogs SET {sets} WHERE id=?", (*fields.values(), did))


def dialogs_list(chat_id, folder_id="any", limit=30):
    q, args = "SELECT d.*, (SELECT COUNT(*) FROM messages m WHERE m.dialog_id=d.id) AS n FROM dialogs d WHERE chat_id=? AND state='active'", [chat_id]
    if folder_id is None:
        q += " AND folder_id IS NULL"
    elif folder_id != "any":
        q += " AND folder_id=?"
        args.append(folder_id)
    with closing(db()) as con:
        return [dict(r) for r in con.execute(q + " ORDER BY pinned_at IS NULL, updated DESC LIMIT ?", (*args, limit)).fetchall()]


def dialog_delete(did):
    with closing(db()) as con, con:
        con.execute("DELETE FROM messages WHERE dialog_id=?", (did,))
        con.execute("DELETE FROM dialogs WHERE id=?", (did,))


def current_dialog(chat_id):
    did = prefs(chat_id).get("dialog")
    d = dialog_get(did) if did else None
    if d and d.get("state", "active") == "active":
        return did
    existing = dialogs_list(chat_id, limit=1)
    if existing:
        set_pref(chat_id, "dialog", existing[0]["id"])
        return existing[0]["id"]
    return dialog_create(chat_id)


def folder_create(chat_id, name):
    with closing(db()) as con, con:
        return con.execute(
            "INSERT INTO folders(chat_id, name, created) VALUES (?,?,?)", (chat_id, name, time.time())
        ).lastrowid


def folder_get(fid):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
    return dict(row) if row else None


def folders_list(chat_id):
    with closing(db()) as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT f.*, (SELECT COUNT(*) FROM dialogs d WHERE d.folder_id=f.id AND d.state='active') AS nd, "
                "(SELECT COUNT(*) FROM tasks t WHERE t.folder_id=f.id AND t.state='active' AND t.status IN ('choice','deferred','scheduled')) AS nt "
                "FROM folders f WHERE chat_id=? AND state='active' ORDER BY pinned_at IS NULL, name",
                (chat_id,),
            ).fetchall()
        ]


def folder_rename(fid, name):
    with closing(db()) as con, con:
        con.execute("UPDATE folders SET name=? WHERE id=?", (name, fid))


def folder_delete(fid):
    """Теку видаляємо, а її розмови й завдання переносимо в «Без теки»."""
    with closing(db()) as con, con:
        con.execute("UPDATE dialogs SET folder_id=NULL WHERE folder_id=?", (fid,))
        con.execute("UPDATE tasks SET folder_id=NULL WHERE folder_id=?", (fid,))
        con.execute("DELETE FROM folders WHERE id=?", (fid,))


# ---------------------------------------------------------------- закріплені


def toggle_pin(kind, item_id):
    it = item_get(kind, item_id)
    if not it:
        return False
    with closing(db()) as con, con:
        con.execute(f"UPDATE {KINDS[kind][0]} SET pinned_at=? WHERE id=?", (None if it.get("pinned_at") else time.time(), item_id))
    return not it.get("pinned_at")


def pinned_items(chat_id):
    out = []
    with closing(db()) as con:
        for kind, (table, _, _) in KINDS.items():
            extra = ", ts AS created" if kind == "n" else ""
            for r in con.execute(
                f"SELECT *{extra} FROM {table} WHERE chat_id=? AND state='active' AND pinned_at IS NOT NULL", (chat_id,)
            ).fetchall():
                out.append((kind, dict(r)))
    return out


def pin_button(kind, item_id):
    it = item_get(kind, item_id)
    return ("📍 Відкріпити", f"m:pin:{kind}:{item_id}") if it and it.get("pinned_at") else ("📌 Закріпити", f"m:pin:{kind}:{item_id}")


# ---------------------------------------------------------------- спільний доступ


def invite_create(single):
    code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    with closing(db()) as con, con:
        con.execute(
            "INSERT INTO invites(code, uses_left, expires, created) VALUES (?,?,?,?)",
            (code, 1 if single else None, time.time() + 7 * 86400, time.time()),
        )
    return code


def invites_active():
    with closing(db()) as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT * FROM invites WHERE revoked=0 AND expires>? AND (uses_left IS NULL OR uses_left>0) ORDER BY created DESC",
                (time.time(),),
            ).fetchall()
        ]


def invite_use(code):
    """Перевіряє запрошення й зменшує кількість використань. True — дійсне."""
    with closing(db()) as con, con:
        row = con.execute(
            "SELECT * FROM invites WHERE code=? AND revoked=0 AND expires>? AND (uses_left IS NULL OR uses_left>0)",
            (code, time.time()),
        ).fetchone()
        if not row:
            return False
        if row["uses_left"] is not None:
            con.execute("UPDATE invites SET uses_left=uses_left-1 WHERE code=?", (code,))
    return True


def invite_revoke(code):
    with closing(db()) as con, con:
        con.execute("UPDATE invites SET revoked=1 WHERE code=?", (code,))


def preinvite_add(raw):
    raw = raw.strip()
    key = raw.lower() if raw.startswith("@") else ("@" + raw.lower() if not raw.isdigit() else raw)
    with closing(db()) as con, con:
        con.execute("INSERT OR REPLACE INTO preinvites(key, created) VALUES (?,?)", (key, time.time()))
    return key


def preinvites_list():
    with closing(db()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM preinvites ORDER BY created DESC").fetchall()]


def preinvite_remove(key):
    with closing(db()) as con, con:
        con.execute("DELETE FROM preinvites WHERE key=?", (key,))


def preinvite_match(user):
    keys = {str(user.id)} | ({"@" + user.username.lower()} if user.username else set())
    with closing(db()) as con:
        for k in keys:
            if con.execute("SELECT 1 FROM preinvites WHERE key=?", (k,)).fetchone():
                return k
    return None


def member_upsert(user, status, code=None):
    with closing(db()) as con, con:
        con.execute(
            "INSERT INTO members(user_id, name, username, status, invite_code, created) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, name=excluded.name, username=excluded.username",
            (user.id, user.full_name, user.username, status, code, time.time()),
        )


def member_set(user_id, status):
    with closing(db()) as con, con:
        if status == "removed":
            con.execute("DELETE FROM members WHERE user_id=?", (user_id,))
        else:
            con.execute(
                "UPDATE members SET status=?, approved_at=CASE WHEN ?='approved' THEN ? ELSE approved_at END WHERE user_id=?",
                (status, status, time.time(), user_id),
            )


def members_list(status=None):
    with closing(db()) as con:
        if status:
            rows = con.execute("SELECT * FROM members WHERE status=? ORDER BY created DESC", (status,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM members ORDER BY status, created DESC").fetchall()
    return [dict(r) for r in rows]


def member_get(user_id):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM members WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- архів і кошик

TRASH_DAYS = int(env("TRASH_DAYS", "30"))
KINDS = {  # тип -> (таблиця, назва, іконка)
    "d": ("dialogs", "Розмови", "💬"),
    "f": ("folders", "Теки", "📁"),
    "t": ("tasks", "Завдання", "⏰"),
    "n": ("notes", "Нотатки", "📝"),
    "m": ("media", "Медіа", "🖼"),
}
MEDIA_ICONS = {"photo": "🖼", "video": "🎞", "audio": "🎵", "url": "🔗"}
MONTHS = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]


def set_state(kind, item_id, state):
    table = KINDS[kind][0]
    field = {"archived": ", archived_at=?", "trash": ", trashed_at=?"}.get(state, "")
    args = (state, time.time(), item_id) if field else (state, item_id)
    with closing(db()) as con, con:
        con.execute(f"UPDATE {table} SET state=?{field} WHERE id=?", args)
        if kind == "f" and state == "trash":  # вміст теки лишається, переходить у «Без теки»
            con.execute("UPDATE dialogs SET folder_id=NULL WHERE folder_id=?", (item_id,))
            con.execute("UPDATE tasks SET folder_id=NULL WHERE folder_id=?", (item_id,))
        if kind == "t" and state != "active":
            con.execute(
                "UPDATE tasks SET status='cancelled' WHERE id=? AND status IN ('choice','deferred','scheduled')", (item_id,)
            )


def item_title(kind, row):
    if kind == "d":
        return row["title"]
    if kind == "f":
        return row["name"]
    if kind == "t":
        return row["history_text"]
    if kind == "m":
        return f"{MEDIA_ICONS.get(row['kind'], '🖼')} {row['prompt'] or 'без опису'}"
    return row["text"]


def items_in(chat_id, kind, state):
    """Елементи архіву або кошика, від нових до старих. Виконані й скасовані завдання — теж в архіві."""
    table = KINDS[kind][0]
    order = "trashed_at" if state == "trash" else "archived_at"
    with closing(db()) as con:
        if kind == "n":
            rows = con.execute(
                f"SELECT *, ts AS created FROM notes WHERE chat_id=? AND state=? ORDER BY {order} DESC", (chat_id, state)
            ).fetchall()
        elif kind == "t" and state == "archived":
            rows = con.execute(
                "SELECT *, COALESCE(archived_at, last_try, created) AS archived_at FROM tasks WHERE chat_id=? AND "
                "(state='archived' OR (state='active' AND status IN ('done','cancelled'))) ORDER BY COALESCE(archived_at, last_try, created) DESC",
                (chat_id,),
            ).fetchall()
        else:
            rows = con.execute(f"SELECT * FROM {table} WHERE chat_id=? AND state=? ORDER BY {order} DESC", (chat_id, state)).fetchall()
    return [dict(r) for r in rows]


def item_get(kind, item_id):
    extra = ", ts AS created" if kind == "n" else ""
    with closing(db()) as con:
        row = con.execute(f"SELECT *{extra} FROM {KINDS[kind][0]} WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def purge(kind, item_id):
    with closing(db()) as con, con:
        if kind == "d":
            con.execute("DELETE FROM messages WHERE dialog_id=?", (item_id,))
        con.execute(f"DELETE FROM {KINDS[kind][0]} WHERE id=?", (item_id,))


def purge_old_trash():
    """Остаточно видаляє все, що лежить у кошику довше за TRASH_DAYS днів."""
    limit = time.time() - TRASH_DAYS * 86400
    n = 0
    for kind, (table, _, _) in KINDS.items():
        with closing(db()) as con:
            ids = [r[0] for r in con.execute(f"SELECT id FROM {table} WHERE state='trash' AND trashed_at < ?", (limit,))]
        for i in ids:
            purge(kind, i)
            n += 1
    return n


# ---------------------------------------------------------------- повідомлення


def save_message(chat_id, role, text, source="telegram", provider=None, dialog_id=None):
    did = dialog_id or current_dialog(chat_id)
    with closing(db()) as con, con:
        mid = con.execute(
            "INSERT INTO messages(chat_id, role, text, source, provider, ts, dialog_id) VALUES (?,?,?,?,?,?,?)",
            (chat_id, role, text, source, provider, time.time(), did),
        ).lastrowid
        con.execute("UPDATE dialogs SET updated=? WHERE id=?", (time.time(), did))
        if role == "user":  # перше питання стає назвою нової розмови
            con.execute(
                "UPDATE dialogs SET title=? WHERE id=? AND title='Нова розмова'",
                (text.replace("\n", " ")[:40] or "Нова розмова", did),
            )
    return mid


def set_msg(msg_id, **fields):
    with closing(db()) as con, con:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE messages SET {sets} WHERE id=?", (*fields.values(), msg_id))


def load_history(chat_id, before_id=None, dialog_id=None, limit=None):
    """Історія розмови для моделі. Однакова для всіх моделей: кожна бачить, що відповідали інші.
    Питання, що чекають у відкладених завданнях, сюди не потрапляють."""
    did = dialog_id or current_dialog(chat_id)
    with closing(db()) as con:
        rows = con.execute(
            "SELECT role, text FROM messages WHERE dialog_id=? AND in_context=1 AND pending=0 AND id<? "
            "ORDER BY id DESC LIMIT ?",
            (did, before_id or 2**62, limit or prefs(chat_id)["memory"]),
        ).fetchall()
    msgs = []
    for r in reversed(rows):
        if msgs and msgs[-1]["role"] == r["role"]:
            msgs[-1]["content"] += "\n\n" + r["text"]
        else:
            msgs.append({"role": r["role"], "content": r["text"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    with closing(db()) as con:
        first = con.execute("SELECT MIN(id) FROM (SELECT id FROM messages WHERE dialog_id=? AND in_context=1 AND pending=0 AND id<? ORDER BY id DESC LIMIT ?)",
                            (did, before_id or 2**62, limit or prefs(chat_id)["memory"])).fetchone()[0]
    note = project_context(did, first) if did else ""
    if note:
        msgs.insert(0, {"role": "system", "content": note})
    return msgs


def last_messages(did, n=6):
    with closing(db()) as con:
        rows = con.execute(
            "SELECT role, text, provider FROM messages WHERE dialog_id=? ORDER BY id DESC LIMIT ?", (did, n)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_choice(chat_id):
    with closing(db()) as con:
        row = con.execute("SELECT provider FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    choice = row["provider"] if row else "auto"
    return choice if choice == "auto" or choice in PROVIDERS else "auto"


def set_choice(chat_id, provider):
    with closing(db()) as con, con:
        con.execute(
            "INSERT INTO settings(chat_id, provider) VALUES (?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET provider=excluded.provider",
            (chat_id, provider),
        )


CHAT = contextvars.ContextVar("chat", default=None)  # чий запит зараз обробляється


def owner_id():
    return ALLOWED_USERS[0] if ALLOWED_USERS else 0


def is_admin_id(chat_id):
    """Власники бота — усі ID з ALLOWED_USERS (наприклад, основний і запасний акаунт)."""
    return chat_id in ALLOWED_USERS


def note_owner(chat_id=None):
    return chat_id or CHAT.get() or owner_id()


def add_note_db(text, tags, source, chat_id=None):
    with closing(db()) as con, con:
        nid = con.execute(
            "INSERT INTO notes(text, tags, source, ts, chat_id) VALUES (?,?,?,?,?)",
            (text, tags, source, time.time(), note_owner(chat_id)),
        ).lastrowid
    if is_admin_id(note_owner(chat_id)):
        svc_auto_note(text)
    return nid


def list_notes_db(limit=20, query=None, chat_id=None):
    cid = note_owner(chat_id)
    with closing(db()) as con:
        if query:
            like = f"%{query}%"
            rows = con.execute(
                "SELECT * FROM notes WHERE chat_id=? AND state='active' AND (text LIKE ? OR tags LIKE ?) "
                "ORDER BY pinned_at IS NULL, id DESC LIMIT ?",
                (cid, like, like, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM notes WHERE chat_id=? AND state='active' ORDER BY pinned_at IS NULL, id DESC LIMIT ?", (cid, limit)
            ).fetchall()
    return [
        {
            "id": r["id"],
            "text": r["text"],
            "tags": r["tags"],
            "source": r["source"],
            "pinned": bool(r["pinned_at"]),
            "date": datetime.fromtimestamp(r["ts"], TZ).strftime("%Y-%m-%d %H:%M"),
        }
        for r in rows
    ]


def delete_note_db(note_id):
    with closing(db()) as con, con:
        return con.execute("DELETE FROM notes WHERE id=?", (note_id,)).rowcount > 0


# ---------------------------------------------------------------- статистика й стан моделей


def count_request(pid, error=False):
    with closing(db()) as con, con:
        con.execute(
            "INSERT INTO usage(provider, day, requests, errors) VALUES (?,?,1,?) "
            "ON CONFLICT(provider, day) DO UPDATE SET requests=requests+1, errors=errors+excluded.errors",
            (pid, today(), 1 if error else 0),
        )


def usage(pid, period="day"):
    key = today() if period == "day" else today()[:7] + "%"
    with closing(db()) as con:
        row = con.execute(
            "SELECT COALESCE(SUM(requests),0) r, COALESCE(SUM(errors),0) e FROM usage WHERE provider=? AND day LIKE ?",
            (pid, key),
        ).fetchone()
    return row["r"], row["e"]


def get_state(pid):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM pstate WHERE provider=?", (pid,)).fetchone()
    return dict(row) if row else {}


def update_state(pid, **fields):
    with closing(db()) as con, con:
        con.execute("INSERT OR IGNORE INTO pstate(provider) VALUES (?)", (pid,))
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE pstate SET {sets} WHERE provider=?", (*fields.values(), pid))


def mark_ok(pid):
    update_state(pid, last_ok=time.time(), fails=0)


def mark_error(pid, info):
    st = get_state(pid)
    fails = (st.get("fails") or 0) + 1
    fields = {"err_kind": info["kind"], "err_text": info["detail"][:500], "err_ts": time.time(), "fails": fails}
    if info.get("cooldown"):
        fields["cooldown_until"] = time.time() + info["cooldown"]
        fields["cooldown_kind"] = info["kind"]
    update_state(pid, **fields)
    return fails


def paused_until(pid):
    until = get_state(pid).get("cooldown_until")
    return until if until and until > time.time() else None


def save_ratelimit(pid, headers):
    rl = {k.lower(): v for k, v in headers.items() if re.search(r"rate-?limit|x-trial", k, re.I)}
    if rl:
        update_state(pid, rl_json=json.dumps(rl), rl_ts=time.time())


def ratelimit_summary(pid):
    """Залишки з заголовків сервісу: [(назва, залишок, ліміт, скидання)]."""
    st = get_state(pid)
    if not st.get("rl_json"):
        return [], None
    rl = json.loads(st["rl_json"])
    out = []
    for k, v in rl.items():
        if "remaining" not in k:
            continue
        name = re.sub(r"^(x-)?ratelimit-|^x-", "", k).replace("remaining", "").strip("-")
        name = (
            (name or "requests")
            .replace("requests", "запити")
            .replace("request", "запити")
            .replace("req", "запити")
            .replace("tokens", "токени")
            .replace("minute", "за хвилину")
            .replace("day", "за день")
            .replace("month", "за місяць")
            .replace("-", " ")
        )
        out.append((name, v, rl.get(k.replace("remaining", "limit")), rl.get(k.replace("remaining", "reset"))))
    return out, st.get("rl_ts")


def parse_duration(s):
    """'34s', '2m59.5s', '1h2m', '120', '0.5s' -> секунди."""
    if s is None:
        return None
    s = str(s).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", s):
        found = True
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total if found else None


ERR = {
    "auth": ("🔑", "Ключ недійсний"),
    "forbidden": ("⛔", "Доступ заборонено"),
    "region": ("🌍", "Недоступно в регіоні сервера"),
    "model": ("🔍", "Модель не знайдено"),
    "rate_minute": ("⏳", "Ліміт за хвилину"),
    "rate_day": ("📅", "Денний ліміт вичерпано"),
    "rate_month": ("📅", "Місячний ліміт вичерпано"),
    "rate": ("⏳", "Ліміт вичерпано"),
    "payment": ("💳", "Закінчився баланс або кредити"),
    "overloaded": ("🔥", "Сервіс перевантажений або збій"),
    "timeout": ("⌛", "Немає відповіді вчасно"),
    "network": ("📡", "Немає зв'язку із сервісом"),
    "bad_request": ("❓", "Запит відхилено"),
    "bad_response": ("❓", "Незрозуміла відповідь сервісу"),
    "empty": ("🫥", "Порожня відповідь"),
    "paused": ("⏸", "На паузі"),
}
NEEDS_ACTION = {"auth", "forbidden", "region", "model", "payment"}
ALERT_KINDS = NEEDS_ACTION | {"rate_day", "rate_month"}


def classify(status, body, headers):
    b = (body or "").lower()
    retry = parse_duration(headers.get("retry-after")) if headers else None
    m = re.search(r'"retrydelay"\s*:\s*"([\d.]+)s"', b)
    if m:
        retry = float(m.group(1))
    if status == 401 or "invalid_api_key" in b or "api key not valid" in b or "invalid api key" in b or "incorrect api key" in b:
        return "auth", 1800
    if "location is not supported" in b or ("region" in b and status in (400, 403) and "not" in b) or "unsupported_country" in b:
        return "region", 1800
    if status == 402 or "credit balance is too low" in b or "insufficient_quota" in b or "insufficient credits" in b or "insufficient balance" in b:
        return "payment", 1800
    if status == 403:
        return "forbidden", 1800
    if status == 404 or ("model" in b and any(w in b for w in ("not found", "does not exist", "decommissioned", "unknown model", "no endpoints found", "not_found"))):
        return "model", 1800
    if status == 429 or "rate limit" in b or "rate_limit" in b or "resource_exhausted" in b or "too many requests" in b:
        if re.search(r"per ?day|perday|daily|requests per day|rpd|tokens per day|\btpd\b", b):
            return "rate_day", retry or 3600
        if re.search(r"per ?month|monthly", b):
            return "rate_month", retry or 6 * 3600
        if re.search(r"per ?minute|perminute|\brpm\b|\btpm\b|per second", b):
            return "rate_minute", retry or 60
        return "rate", retry or 120
    if status >= 500:
        return "overloaded", retry or 60
    return "bad_request", None


def advice(pid, kind, until=None):
    p = PROVIDERS.get(pid) or CATALOG.get(pid) or {"site": "сайті сервісу", "key_env": "ключ", "model": "?", "models_url": ""}
    key_env = "CF_API_TOKEN і CF_ACCOUNT_ID" if pid == "cloudflare" else p["key_env"]
    when = f" о {hhmm(until)}" if until else ""
    return {
        "auth": f"Створіть новий ключ на {p['site']} і замініть змінну {key_env} на сервері.",
        "forbidden": f"Перевірте права ключа та умови на {p['site']}.",
        "region": "Сервер бота стоїть у країні, де цей сервіс не працює. Перенесіть бота в інший регіон або приберіть ключ.",
        "model": f"Назва «{p['model']}» застаріла. Задайте актуальну змінною {pid.upper()}_MODEL (список: {p['models_url']}).",
        "payment": f"Поповніть баланс на {p['site']} або користуйтесь безкоштовними моделями.",
        "rate_day": f"Ліміт оновиться; бот спробує знову{when}. Тим часом відповідають інші моделі.",
        "rate_month": f"Бот спробує знову{when}. Тим часом відповідають інші моделі.",
        "rate_minute": "Пауза на кілька секунд, нічого робити не треба.",
        "rate": f"Бот спробує знову{when}.",
        "overloaded": "Проблема на боці сервісу, зазвичай минає за кілька хвилин.",
        "timeout": "Сервіс відповідає занадто довго. Можна збільшити REQUEST_TIMEOUT або обрати легшу модель.",
        "network": "Перевірте адресу й доступність сервісу" + (" (OLLAMA_URL, чи увімкнений комп'ютер)." if pid == "ollama_local" else "."),
        "bad_request": "Подивіться фрагмент відповіді сервісу нижче.",
        "bad_response": "Сервіс відповів у неочікуваному форматі; можливо, змінилась адреса чи модель.",
        "empty": "Спробуйте переформулювати або іншу модель.",
    }.get(kind, "")


class ProviderError(Exception):
    def __init__(self, kind, detail="", cooldown=None):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.cooldown = kind, detail, cooldown

    def info(self):
        return {"kind": self.kind, "detail": self.detail, "cooldown": self.cooldown}


# ---------------------------------------------------------------- виклик моделей

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Зберегти нотатку в спільну базу нотаток бота.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст нотатки"},
                    "tags": {"type": "string", "description": "Теги через кому, необов'язково"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "Знайти нотатки за словом або тегом. Порожній запит — останні нотатки.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Згенерувати зображення (безкоштовний генератор) і надіслати його користувачу в Telegram. "
            "Опис краще писати англійською, детально.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Детальний опис зображення англійською"},
                    "shape": {"type": "string", "enum": ["square", "portrait", "landscape", "story"]},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_music",
            "description": "Згенерувати музичний фрагмент за описом і надіслати користувачу в Telegram.",
            "parameters": {
                "type": "object",
                "properties": {"prompt": {"type": "string", "description": "Опис музики англійською: жанр, настрій, інструменти"}},
                "required": ["prompt"],
            },
        },
    },
]
SHAPES = {"square": (1024, 1024), "portrait": (1080, 1350), "landscape": (1280, 720), "story": (1080, 1920)}
MEDIA = contextvars.ContextVar("media", default=None)  # файли, які треба надіслати після відповіді
OPTS = contextvars.ContextVar("opts", default=None)  # налаштування користувача для поточного виклику

NO_TOOLS, NO_IMAGES = set(), set()
WARNED = {}  # (pid, тип попередження) -> час, щоб не повторювати попередження надто часто


def warn_once(pid, key, every=1800):
    k = (pid, key)
    if time.time() - WARNED.get(k, 0) < every:
        return False
    WARNED[k] = time.time()
    return True


async def run_tool(name, args):
    progress_stage({"generate_image": "🎨 Малюю картинку…", "generate_music": "🎵 Створюю музику…", "save_note": "📝 Зберігаю нотатку…",
                    "search_notes": "🔍 Шукаю в нотатках…", "external_add": "🔗 Надсилаю в сервіс…", "external_list": "🔗 Дивлюся в сервісі…"}.get(name, "🧰 Працюю з інструментом…"))
    if name in ("external_add", "external_list"):
        return await run_svc_tool(name, args)
    cname = canva.tool_name(name)
    if cname:
        try:
            text, urls = await canva.call_tool(cname, args)
        except Exception as e:  # noqa: BLE001
            return f"Canva недоступна: {e}"
        queue = MEDIA.get()
        if queue is not None:
            queue += [("url", u, "🎨 Canva") for u in urls[:4]]
        return text
    if name == "generate_image":
        w, h = SHAPES.get(args.get("shape") or "square", SHAPES["square"])
        try:
            data, pid, _ = await media.generate_image(args.get("prompt", ""), w, h)
        except media.MediaError as e:
            return f"Не вдалося згенерувати зображення: {e.detail}"
        count_request("img:" + pid)
        queue = MEDIA.get()
        if queue is not None:
            queue.append(("photo", data, f"🎨 {media.IMAGE_PROVIDERS[pid]['title']}"))
        return "Зображення згенеровано й буде надіслано користувачу разом із відповіддю."
    if name == "generate_music":
        try:
            data, pid, _ = await media.generate_music(args.get("prompt", ""))
        except media.MediaError as e:
            return f"Не вдалося згенерувати музику: {e.detail}"
        count_request("music:" + pid)
        queue = MEDIA.get()
        if queue is not None:
            queue.append(("audio", data, f"🎵 {media.MUSIC_PROVIDERS[pid]['title']}"))
        return "Музику згенеровано й буде надіслано користувачу разом із відповіддю."
    if name == "save_note":
        return f"Нотатку збережено, id={add_note_db(args.get('text', ''), args.get('tags', ''), 'telegram')}"
    if name == "search_notes":
        found = list_notes_db(limit=15, query=(args.get("query") or None))
        return json.dumps(found, ensure_ascii=False) if found else "Нічого не знайдено"
    return f"Невідомий інструмент {name}"


def strip_images(messages):
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            texts = [p["text"] for p in m["content"] if p.get("type") == "text"]
            m = {**m, "content": "\n".join(texts) + "\n[користувач надіслав фото, але ця модель не бачить зображень]"}
        out.append(m)
    return out


def has_images(messages):
    return any(isinstance(m.get("content"), list) for m in messages)


async def post_chat(client, p, messages, use_tools):
    o = OPTS.get() or {}
    body = {"model": p["model"], "messages": messages, "max_tokens": o.get("max_tokens", MAX_TOKENS)}
    if "temperature" in o:
        body["temperature"] = o["temperature"]
    if use_tools:
        own = CHAT.get() is None or is_admin_id(CHAT.get())  # Canva підключена до акаунта власника — лише для нього
        body["tools"] = TOOLS + ((await canva.openai_tools()) + (SVC_TOOLS if any(SVC.connected(x) for x in SERVICES) else []) if own else [])
    try:
        r = await client.post(
            f"{p['base_url']}/chat/completions", headers={"Authorization": f"Bearer {p['key']}"}, json=body
        )
    except httpx.TimeoutException:
        count_request(p["id"], error=True)
        raise ProviderError("timeout", f"не відповів за {int(REQUEST_TIMEOUT)} с", 120)
    except httpx.HTTPError as e:
        count_request(p["id"], error=True)
        raise ProviderError("network", str(e) or type(e).__name__, 300 if p["id"] == "ollama_local" else 120)
    save_ratelimit(p["id"], r.headers)
    if r.status_code >= 400:
        count_request(p["id"], error=True)
        kind, cooldown = classify(r.status_code, r.text, r.headers)
        raise ProviderError(kind, f"HTTP {r.status_code}: {r.text[:300]}", cooldown)
    count_request(p["id"])
    try:
        choice = r.json()["choices"][0]
        return choice["message"], choice.get("finish_reason")
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError("bad_response", r.text[:300])


async def ask_provider(p, messages):
    """Розмова з одним сервісом, включно з інструментами. Повертає (текст, попередження)."""
    pid, warnings = p["id"], []
    if pid in NO_IMAGES and has_images(messages):
        messages = strip_images(messages)
        warnings.append("🖼 Модель не бачить фото, отримала лише підпис. Для фото оберіть Gemini.")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for _ in range(6):
            use_tools = pid not in NO_TOOLS
            try:
                msg, finish = await post_chat(client, p, messages, use_tools)
            except ProviderError as e:
                # 400/422 часто означає «не вмію інструменти» або «не бачу фото» — спрощуємо запит
                if e.kind == "bad_request" and use_tools:
                    NO_TOOLS.add(pid)
                    log.info("%s: вимикаю інструменти (%s)", pid, e.detail)
                    if warn_once(pid, "no_tools", 86400):
                        warnings.append("📝 Ця модель не вміє зберігати нотатки сама. Користуйтесь /note або іншою моделлю.")
                    continue
                if e.kind == "bad_request" and has_images(messages):
                    NO_IMAGES.add(pid)
                    messages = strip_images(messages)
                    warnings.append("🖼 Модель не бачить фото, отримала лише підпис. Для фото оберіть Gemini.")
                    continue
                raise
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip()
                if not text:
                    raise ProviderError("empty", "модель повернула порожню відповідь", None)
                if finish == "length":
                    warnings.append("✂️ Відповідь обірвалася через ліміт довжини — «▶️ Продовжити» допише її з того самого місця (можна іншою моделлю).")
                return text, warnings
            messages = messages + [{"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}]
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                messages.append(
                    {"role": "tool", "tool_call_id": c.get("id", ""), "content": await run_tool(c["function"]["name"], args)}
                )
    raise ProviderError("bad_response", "модель зациклилась на інструментах", None)


def low_limit_warnings(pid):
    out = []
    items, _ = ratelimit_summary(pid)
    for name, rem, lim, _ in items:
        try:
            rem_i, lim_i = int(float(rem)), int(float(lim))
        except (TypeError, ValueError):
            continue
        if lim_i > 0 and rem_i / lim_i < 0.1 and warn_once(pid, f"low:{name}"):
            out.append(f"⚠️ {PROVIDERS[pid]['title']}: лишилось {rem_i} з {lim_i} ({name}).")
    lim = PROVIDERS[pid]["limit"]
    if lim and not items:
        used, _ = usage(pid, lim[0])
        left = lim[1] - used
        if left < lim[1] * 0.1 and warn_once(pid, "low:est"):
            out.append(f"⚠️ {PROVIDERS[pid]['title']}: за оцінкою лишилось ≈{max(left, 0)} запитів {'сьогодні' if lim[0] == 'day' else 'цього місяця'}.")
    return out


ERR_SHORT = {"auth": "ключ недійсний", "region": "недоступна в регіоні", "payment": "потрібна оплата", "forbidden": "доступ заборонено",
             "model": "модель не знайдено", "rate_minute": "ліміт за хвилину", "rate_day": "денний ліміт", "rate_month": "місячний ліміт", "rate": "ліміт",
             "overloaded": "перевантажена", "timeout": "задовго думала", "network": "немає зв'язку", "bad_request": "не зрозуміла запит",
             "bad_response": "дивна відповідь", "empty": "порожня відповідь"}


class Unavailable(Exception):
    """Жодна з дозволених моделей не відповіла. attempts — [(pid, info)]."""

    def __init__(self, attempts):
        super().__init__("unavailable")
        self.attempts = attempts


# ---------------------------------------------------------------- 🏆 пріоритети й черга роботи ШІ
# LLM_PARALLEL — скільки запитів до ШІ виконується одночасно. Коли місця зайняті, важливіший запит не чекає:
# менш важливий стає на паузу (без відмови) і продовжиться сам. Хто питав — розмова, бот чи модель — отримує сповіщення.
RK = Ranks(kv_get, kv_set)
# Для ботів і ШІ, що питали: пауза — це не відмова. Вони мають чекати, а не кидати завдання чи слати його знову.
KEEP_WAITING = "Не скасовуйте й не надсилайте повторно — просто чекайте своєї черги: завдання збережене й буде виконане."
QUEUE_FLAGS = {"queued": True, "keep_waiting": True, "do_not_retry": True, "do_not_cancel": True}
WQ = WorkQueue(int(env("LLM_PARALLEL", "2") or 2))
PRIO = contextvars.ContextVar("prio", default=None)  # {"score", "label", "chat", "notify"}


def partners():
    try:
        return json.loads(kv_get("partners") or "{}")
    except ValueError:
        return {}


def partner_seen(name, link=None, via=None, kind="bot"):
    """🤝 Таблиця ботів і ШІ, які звертаються до цього бота (або з якими він співпрацює)."""
    d = partners()
    p = d.setdefault(name, {"first": time.time(), "n": 0, "kind": kind})
    p["n"] += 1
    p["last"] = time.time()
    if link:
        p["link"] = link
    if via:
        p["via"] = via
    kv_set("partners", json.dumps(d, ensure_ascii=False))


PRIO_TABS = [("d", "💬 Розмови"), ("t", "⏰ Завдання"), ("b", "🤝 Боти й ШІ"), ("c", "🏘 Спільноти")]


def prio_items(chat_id, k):
    """[(ref, підпис, рядок таблиці)] для вкладки пріоритетів."""
    if k == "d":
        return [(str(d["id"]), d["title"], "") for d in dialogs_list(chat_id, limit=40)]
    if k == "t":
        return [(str(t["id"]), f"#{t['id']} {short_q(t['history_text'], 40)}", "") for t in tasks_open(chat_id)]
    if k == "b":
        out = []
        if hub.ready:
            hp = partners().get("🧭 Головний бот", {})
            out.append(("🧭 Головний бот", "🧭 Головний бот", "🧭 Головний бот · керує ботами, пересилає запити" + (f" · запитів {hp.get('n', 0)}" if hp else "")))
        for name, p in partners().items():
            if name == "🧭 Головний бот":
                continue
            link = p.get("link")
            title = f'<a href="{esc(link)}">{esc(name)}</a>' if link else esc(name)
            out.append((name, name, f"{title} · запитів {p.get('n', 0)} · востаннє {datetime.fromtimestamp(p.get('last', 0), TZ).strftime('%d.%m %H:%M')}"
                        + (f" · {esc(p['via'])}" if p.get("via") else "")))
        return out
    if k == "c":
        return [(c["id"], c["title"], "") for c in CM.chats()]
    return []


def prio_ranks(k):
    return CM.ranks if k == "c" else RK


def scr_prio(chat_id, k="d"):
    items = prio_items(chat_id, k)
    R = prio_ranks(k)
    rk = "c" if k == "c" else k
    above, normal, below = R.arrange(rk, [r for r, _, _ in items])
    by = {r: (t, row) for r, t, row in items}
    tabs = [[(("● " if kk == k else "") + t, f"m:pr:{kk}") for kk, t in PRIO_TABS[:2]], [(("● " if kk == k else "") + t, f"m:pr:{kk}") for kk, t in PRIO_TABS[2:]]]
    lines = ["🏆 <b>Пріоритети</b> — що виконувати першим, коли роботи багато.",
             f"Одночасно до ШІ йде {WQ.slots} запит(и). Важливіший запит не чекає: менш важливий стає на паузу (не відхиляється!) і продовжиться сам, "
             "а той, хто питав, отримує сповіщення й просто чекає своєї черги.",
             f"Зараз: {WQ.busy_text()} · у черзі {len(WQ.waiting)} · пауз за весь час: {WQ.paused_total}", ""]
    if k == "b":
        lines.append("🤝 <b>Боти й ШІ, з якими працює бот</b> (вище — важливіші):")
    rows = []
    n = 0
    for title, part, icon in (("🔺 Важливіші", above, "🔺"), ("▫️ Звичайні", normal, "▫️"), ("🔻 Менш важливі", below, "🔻")):
        if not part:
            continue
        lines.append(f"<b>{title}:</b>")
        for r in part:
            n += 1
            t, row = by[r]
            lines.append(f"{n}. {row or esc(t)}")
            if len(rows) < 14:
                rows.append([(f"{icon} {short_q(t, 22)}", f"m:pri:{k}:{r}"), ("⬆️", f"m:prm:{k}:{r}:up"), ("⬇️", f"m:prm:{k}:{r}:down")])
    if not items:
        lines.append({"d": "Розмов немає.", "t": "Відкритих завдань немає.", "b": "Поки ніхто з інших ботів чи ШІ не звертався. Вони з'являться тут самі.",
                      "c": "Бот ще не доданий у групи чи канали."}[k])
    return "\n".join(lines)[:4000], tabs + rows


def scr_prio_item(chat_id, k, ref):
    t = next((t for r, t, _ in prio_items(chat_id, k) if r == ref), ref)
    sc = prio_ranks(k).score(k, ref)
    state = "🔺 важливіше за звичайне" if sc > 0 else "🔻 менш важливе" if sc < 0 else "▫️ звичайний пріоритет"
    return (f"🏆 <b>{esc(t)}</b>\n{state}",
            [[("⏫ На самий верх", f"m:prm:{k}:{ref}:top"), ("⬆️ Вище", f"m:prm:{k}:{ref}:up")],
             [("⬇️ Нижче", f"m:prm:{k}:{ref}:down"), ("⏬ У самий низ", f"m:prm:{k}:{ref}:bottom")],
             [("▫️ Звичайний", f"m:prm:{k}:{ref}:reset"), ("« Пріоритети", f"m:pr:{k}")]])


def prio_row(k, ref):
    sc = RK.score(k, ref)
    return [(f"🏆 {Ranks.label(sc)} Пріоритет", f"m:pri:{k}:{ref}"), ("⬆️", f"m:prm:{k}:{ref}:up"), ("⬇️", f"m:prm:{k}:{ref}:down")]


def dialog_score(did):
    return RK.score("d", did) if did else 0


def set_prio(score, label, chat=None, notify=None):
    return PRIO.set({"score": score, "label": label, "chat": chat, "notify": notify})


async def ask_llm(history, user_content, preferred=None, fallback=True, exclude=()):
    """Запит до ШІ через чергу з пріоритетами (див. _ask_llm)."""
    p = PRIO.get() or {"score": 0, "label": short_q(user_content if isinstance(user_content, str) else "запит", 40)}

    async def tell(text):
        try:
            if p.get("notify"):
                await p["notify"](text)
            elif p.get("chat") and tg_app:
                await tg_app.bot.send_message(p["chat"], text)
        except Exception:  # noqa: BLE001
            pass

    async def on_wait(busy):
        progress_stage(f"🕒 У черзі: зараз виконую важливіше — {busy}")
        await tell(f"🕒 {p['label']}: у черзі — зараз виконую важливіше: {busy}.\nНічого не скасовано: візьмуся, щойно звільнюся, відповідь прийде сюди."
                   + ("\n" + KEEP_WAITING if p.get("notify") else ""))

    async def on_pause(by, ctx):
        jid = ctx.get(LAST_JOURNAL)
        if jid:  # цей запуск перерваний — запис заміниться новим, коли продовжимо
            with closing(db()) as con, con:
                con.execute("DELETE FROM journal WHERE id=?", (jid,))
        progress_stage(f"⏸ На паузі: зараз виконую важливіше — {by}")
        await tell(f"⏸ {p['label']}: тимчасово на паузі — зараз виконую важливіше: {by}.\n"
                   "Не відхилено: продовжу сам, щойно звільнюся, відповідь прийде сюди." + ("\n" + KEEP_WAITING if p.get("notify") else ""))

    async def on_resume():
        progress_stage("▶️ Продовжую після паузи…")

    return await WQ.run(p["score"], p["label"], lambda: _ask_llm(history, user_content, preferred, fallback, exclude),
                        on_pause=on_pause, on_wait=on_wait, on_resume=on_resume, carry=(LAST_JOURNAL, MEDIA))


async def _ask_llm(history, user_content, preferred=None, fallback=True, exclude=()):
    """Обрана модель (fallback=False — лише вона) або «Авто» з перемиканням.
    Повертає (текст, id сервісу, спроби, попередження). Спроби — [(pid, info)] для пропущених і невдалих.
    Якщо ніхто не відповів — Unavailable зі списком причин."""
    MEDIA.set([])
    progress_stage("📚 Збираю контекст діалогу й пам'ять проєкту…", 1, 3)
    hint = (OPTS.get() or {}).get("hint")
    extra = "\n\n".join(m["content"] for m in history if m["role"] == "system")  # пам'ять проєкту — в одне системне повідомлення
    history = [m for m in history if m["role"] != "system"]
    messages = [{"role": "system", "content": SYSTEM_PROMPT + ("\n" + hint if hint else "") + ("\n\n" + extra if extra else "")}] + history + [{"role": "user", "content": user_content}]
    jid = journal_start(user_content)
    LAST_JOURNAL.set(jid)  # щоб пауза чи «Зупинити» знали, який запис журналу оновити
    if preferred in PROVIDERS and not fallback:
        order = [preferred]
    else:
        order = ([preferred] if preferred in PROVIDERS else []) + [p for p in AUTO_ORDER if p != preferred]
    order = [p for p in order if p not in exclude] or order
    if not order:
        raise RuntimeError("Не підключено жодної моделі: додайте хоча б один ключ (наприклад, GEMINI_API_KEY). Довідка: /help")
    ready = [p for p in order if not paused_until(p)]
    paused = [p for p in order if paused_until(p)]
    attempts = [(p, {"kind": "paused", "detail": f"до {hhmm(paused_until(p))}", "until": paused_until(p)}) for p in paused]
    # модель на паузі пробуємо лише в «Авто», коли всі інші не відповіли; обрану вручну — не смикаємо
    cands = ready + (paused if fallback else [])
    for k, pid in enumerate(cands, 1):
        prev = [a for a in attempts if a[1].get("kind") != "paused"]
        progress_stage((f"↪️ {PROVIDERS[prev[-1][0]]['title']} не відповіла ({ERR_SHORT.get(prev[-1][1]['kind'], prev[-1][1]['kind'])}) — " if prev else "")
                       + f"🤖 {PROVIDERS[pid]['title']} думає…" + (f" (спроба {k} з {len(cands)})" if k > 1 else ""), 2, 3)
        try:
            text, warnings = await ask_provider(PROVIDERS[pid], messages)
        except ProviderError as e:
            info = e.info()
            fails = mark_error(pid, info)
            info["until"] = paused_until(pid)
            log.warning("%s: %s", pid, e)
            attempts = [a for a in attempts if a[0] != pid] + [(pid, info)]
            await maybe_alert(pid, info, fails)
            continue
        mark_ok(pid)
        attempts = [a for a in attempts if a[0] != pid]
        partial = any(w.startswith("✂️") for w in warnings)
        journal_update(jid, status="partial" if partial else "done", provider=pid, result=text[:8000],
                       attempts="\n".join(attempt_line(p, i) for p, i in attempts) or None)
        LAST_JOURNAL.set(jid)
        return text, pid, attempts, warnings + low_limit_warnings(pid)
    journal_update(jid, status="failed", attempts="\n".join(attempt_line(p, i) for p, i in attempts))
    LAST_JOURNAL.set(jid)
    raise Unavailable(attempts)


def attempt_line(pid, info):
    icon, title = ERR[info["kind"]]
    extra = ""
    if info["kind"] == "paused":
        extra = f" (до {hhmm(info['until'])}, ще {left(info['until'])}, пропущено)"
    elif info.get("until"):
        extra = f", пауза до {hhmm(info['until'])} (ще {left(info['until'])})"
    return f"{icon} {PROVIDERS[pid]['title']}: {title.lower()}{extra}"


# ---------------------------------------------------------------- сповіщення власнику

ALERTED = {}
BOT_USERNAME = ""
LAST_PURGE = 0.0
BACKGROUND = set()  # посилання на фонові задачі, щоб їх не прибрав збирач сміття


async def maybe_alert(pid, info, fails):
    kind = info["kind"]
    serious = kind in ALERT_KINDS or (kind in ("network", "timeout", "overloaded", "bad_response", "bad_request") and fails >= 3)
    if not serious or tg_app is None or not ALLOWED_USERS or not prefs(ALLOWED_USERS[0])["alerts"]:
        return
    if time.time() - ALERTED.get((pid, kind), 0) < ALERT_EVERY:
        return
    ALERTED[(pid, kind)] = time.time()
    icon, title = ERR[kind]
    p = PROVIDERS[pid]
    text = (
        f"<b>{icon} {esc(p['title'])}: {esc(title)}</b>\n"
        + (f"Помилок поспіль: {fails}\n" if fails >= 3 else "")
        + (f"⏸ Пауза до {hhmm(info['until'])} (ще {left(info['until'])}). Коли мине — бот напише.\n" if info.get("until") and kind in ("rate_day", "rate_month", "rate") else "")
        + f"\n<b>Що робити:</b> {esc(advice(pid, kind, info.get('until')))}\n"
        + f"\n<i>Відповідь сервісу:</i> <code>{esc(info['detail'][:300])}</code>\n"
        + f"\nСтан усіх моделей: /limits · Довідка: /help {pid}"
    )
    try:
        await tg_app.bot.send_message(ALLOWED_USERS[0], text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
    except Exception:  # noqa: BLE001
        log.exception("не вдалося надіслати сповіщення")


# ---------------------------------------------------------------- звіт про ліміти


async def openrouter_key_info():
    p = PROVIDERS.get("openrouter")
    if not p:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{p['base_url']}/key", headers={"Authorization": f"Bearer {p['key']}"})
        return r.json().get("data") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def provider_icon(pid):
    if pid not in PROVIDERS:
        return "▫️"
    if paused_until(pid):
        return "⏸"
    st = get_state(pid)
    if st.get("err_ts") and (not st.get("last_ok") or st["err_ts"] > st["last_ok"]):
        return "❌" if st.get("err_kind") in NEEDS_ACTION else "⚠️"
    return "✅"


def provider_report(pid, or_info=None, short=False):
    """HTML-блок про стан однієї моделі."""
    p = PROVIDERS[pid]
    lines = [f"{provider_icon(pid)} <b>{esc(p['title'])}</b> · <code>{esc(p['model'])}</code>"]
    used, errs = usage(pid, "day")
    lines.append(f"Сьогодні: {used} запит(ів), помилок: {errs}")
    if p["limit"] and p["limit"][0] == "month":
        m_used, _ = usage(pid, "month")
        lines.append(f"Цього місяця: {m_used}")
    items, rl_ts = ratelimit_summary(pid)
    if items:
        parts = []
        for name, rem, lim, reset in items:
            s = f"{name}: {rem}" + (f" з {lim}" if lim else "")
            d = parse_duration(reset)
            if d:
                s += f" (оновлення через {int(d // 60)} хв {int(d % 60)} с)" if d >= 60 else f" (оновлення через {int(d)} с)"
            parts.append(s)
        lines.append("За даними сервісу (" + ago(rl_ts) + "): " + "; ".join(parts))
    elif p["limit"]:
        period, lim = p["limit"]
        u, _ = usage(pid, period)
        lines.append(f"Оцінка залишку: ≈{max(lim - u, 0)} з {lim} {'на день' if period == 'day' else 'на місяць'} (за запитами бота)")
    else:
        lines.append("Залишок: сервіс не повідомляє" + (" — дивіться в кабінеті " + p["site"] if not short else ""))
    if pid == "openrouter" and or_info:
        free = "так" if or_info.get("is_free_tier") else "ні"
        bits = [f"безкоштовний рівень: {free}"]
        if or_info.get("usage") is not None:
            bits.append(f"витрачено кредитів: ${or_info['usage']:.2f}")
        if or_info.get("limit_remaining") is not None:
            bits.append(f"лишилось кредитів: ${or_info['limit_remaining']:.2f}")
        lines.append("OpenRouter: " + ", ".join(bits))
    until = paused_until(pid)
    st = get_state(pid)
    if until:
        lines.append(f"⏸ Пауза до {hhmm(until)} · ще {left(until)}: {ERR.get(st.get('cooldown_kind'), ('', ''))[1].lower()}")
    if st.get("err_ts"):
        icon, title = ERR.get(st["err_kind"], ("❓", st["err_kind"]))
        lines.append(f"Остання помилка: {hhmm(st['err_ts'])} — {icon} {esc(title.lower())}")
        if not short and (not st.get("last_ok") or st["err_ts"] > st["last_ok"]):
            lines.append(f"<i>Що робити:</i> {esc(advice(pid, st['err_kind'], until))}")
    if st.get("last_ok"):
        lines.append(f"Остання вдала відповідь: {ago(st['last_ok'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram: допоміжне


def resolve_provider(name, catalog=False):
    """'groq', 'Groq', 'gro', 'моя ollama' -> id сервісу або None."""
    pool = CATALOG if catalog else PROVIDERS
    n = name.strip().lower().replace(" ", "_").replace("-", "_")
    if n in pool:
        return n
    for pid, p in pool.items():
        if p["title"].lower().replace(" ", "_") == n:
            return pid
    for pid in pool:
        if pid.startswith(n) or pool[pid]["title"].lower().replace(" ", "_").startswith(n):
            return pid
    return None


def member_status(user_id):
    with closing(db()) as con:
        row = con.execute("SELECT status FROM members WHERE user_id=?", (user_id,)).fetchone()
    return row["status"] if row else None


def is_allowed_id(user_id):
    return user_id in ALLOWED_USERS or member_status(user_id) == "approved"


def allowed(update: Update) -> bool:
    ok = update.effective_user is not None and is_allowed_id(update.effective_user.id)
    if ok and update.effective_chat:
        CHAT.set(update.effective_chat.id)
    return ok


def is_owner(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == owner_id()


async def send_long(bot, chat_id, text, **kw):
    for i in range(0, len(text), 4000):
        await bot.send_message(chat_id, text[i : i + 4000], **kw)


async def reply_html(update: Update, text, kb=None):
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=NO_PREVIEW
    )


async def deny(update: Update):
    if preinvite_match(update.effective_user) and member_status(update.effective_user.id) != "blocked":
        markup = kb([[("✅ Згоден(-на)", "cons:yes"), ("❌ Не згоден(-на)", "cons:no")]])
        text = (
            "👋 Власник цього бота запросив вас стати учасником.\n\n"
            "Ви отримаєте ШІ-помічника з кількома моделями, розмовами, завданнями й генераторами.\n\nПогоджуєтесь?"
        )
        if update.callback_query:
            await update.callback_query.answer()
        return await update.effective_chat.send_message(text, reply_markup=markup)
    st = member_status(update.effective_user.id)
    if st == "pending":
        text = "⏳ Ваш запит на доступ ще розглядається. Щойно власник підтвердить, бот напише."
    elif st == "blocked":
        text = "Доступ до бота закрито."
    else:
        text = (
            "🔒 Це приватний бот. Доступ — лише за запрошенням власника.\n"
            f"Якщо ви власник: додайте свій ID {update.effective_user.id} у змінну ALLOWED_USERS на сервері."
        )
    if update.callback_query:
        return await update.callback_query.answer(text[:190], show_alert=True)
    await update.effective_message.reply_text(text)


def choice_label(choice):
    return "🔁 Авто" if choice == "auto" else PROVIDERS[choice]["title"]


# ---------------------------------------------------------------- Telegram: довідка (без ШІ)


def pairs(items):
    return [items[i : i + 2] for i in range(0, len(items), 2)]


HELP_CATEGORIES = {
    "work": ("💬 Робота", ["dialogs", "progress", "priority", "tasks", "storage", "journal", "notes"]),
    "create": ("🎨 Створення", ["create", "photos", "voice", "canva"]),
    "ai": ("🤖 Моделі та ШІ", ["__models__", "choose", "custom", "compare", "best", "paid", "limits", "errors", "connector"]),
    "services": ("🔗 Сервіси", ["community", "services"]),
    "settings": ("⚙️ Налаштування", ["settings", "runmode", "access", "safety", "commands", "privacy"]),
    "start": ("🚀 Початок", ["start", "menu", "faq"]),
}

HELP_SHORT = {
    "dialogs": "💬 Розмови", "progress": "⏳ Хід роботи", "priority": "🏆 Пріоритети",
    "tasks": "⏰ Завдання", "storage": "📌 Архів", "journal": "📒 Журнал", "notes": "📝 Нотатки",
    "create": "🎨 Генератори", "photos": "🖼 Фото", "voice": "🎙 Голос", "canva": "🖌 Canva",
    "choose": "🔀 Вибрати", "custom": "🧩 Свої моделі", "compare": "⚖️ Порівняти",
    "best": "🏆 Підібрати", "paid": "💳 Тарифи", "limits": "📊 Ліміти", "errors": "⚠️ Помилки",
    "connector": "🔗 Конектор", "community": "🏘 Спільноти", "services": "🧩 Інтеграції",
    "settings": "🎛 Параметри", "access": "👥 Доступ", "safety": "🛡 Безпека",
    "commands": "⌨️ Команди", "privacy": "🔒 Приватність", "start": "🚀 Як почати",
    "menu": "🧭 Навігація", "faq": "❓ Питання", "runmode": "⚡ Режим роботи",
}


def help_main(open_key=None):
    """Довідка-акордеон: категорії завжди лишаються на екрані, підпункти розгортаються прямо під обраною."""
    rows = []
    for key, (title, children) in HELP_CATEGORIES.items():
        is_open = key == open_key
        rows.append([(title + (" ▲" if is_open else " ▼"), f"help:x:{key}" if is_open else f"help:c:{key}")])
        if is_open:
            buttons = []
            for item in children:
                if item == "__models__":
                    buttons.append(("🤖 Про моделі", "help:models"))
                elif item in H.GENERAL:
                    buttons.append((HELP_SHORT.get(item, H.GENERAL[item][0]), f"help:g:{item}"))
            rows.extend(pairs(buttons))
    text = (
        "<b>📖 Довідка</b>\n\n"
        "Натисніть ▼, щоб розгорнути підпункти прямо під категорією. Натисніть ▲, щоб згорнути."
    )
    return text, rows


def help_category(key):
    return help_main(key if key in HELP_CATEGORIES else None)


def help_models():
    lines = ["<b>🤖 Моделі</b>\n", "✅ підключено · ⏸ на паузі · ⚠️ були помилки · ❌ потрібні дії · ▫️ не підключено\n"]
    for pid, c in CATALOG.items():
        paid = " 💳" if not c["free"] else ""
        lines.append(f"{provider_icon(pid)} <b>{esc(c['title'])}</b>{paid} — {esc(H.MODEL_SHORT[pid])}")
    customs = [r for r in custom_rows()]
    for r in customs:
        lines.append(f"{provider_icon(r['id'])} <b>{esc(r['title'])}</b> 🧩 — додана вами")
    lines.append("\nОберіть модель, щоб дізнатися, для чого вона, як працює, ліміти, приватність, платні тарифи і як її підключити.")
    rows = pairs([(f"{provider_icon(pid)} {c['title']}", f"help:m:{pid}") for pid, c in CATALOG.items()]
                 + [(f"{provider_icon(r['id'])} {r['title']} 🧩", f"help:m:{r['id']}") for r in customs])
    rows.append([("« Меню довідки", "help:main")])
    return "\n".join(lines), rows


def help_model(pid):
    if pid not in H.MODEL_HELP:  # модель, додана в боті
        p = PROVIDERS.get(pid)
        if not p:
            return help_models()
        text = (
            f"<b>{esc(p['title'])}</b> · додана вами\n\nАдреса: <code>{esc(p['base_url'])}</code>\nМодель: <code>{esc(p['model'])}</code>\n\n"
            "<b>📡 Стан зараз</b>\n" + provider_report(pid, short=True)
        )
        return text, [[("⚙️ Керувати", f"m:cp:{pid}")], [("« Меню довідки", "help:main")]]
    text = H.MODEL_HELP[pid]
    if pid in PROVIDERS:
        text += "\n\n<b>📡 Стан зараз</b>\n" + provider_report(pid, short=True)
        auto = "так" if pid in AUTO_ORDER else "ні"
        text += f"\nУ режимі «Авто»: {auto}"
    else:
        text += "\n\n<b>📡 Стан зараз</b>\n▫️ Не підключено. Як підключити — див. вище."
    rows = [[("💳 Платні тарифи", f"help:p:{pid}")]]
    rows.append([("« Усі моделі", "help:models"), ("« Меню", "help:main")])
    return text, rows


def help_paid(pid):
    rows = [[(f"« {CATALOG[pid]['title']}", f"help:m:{pid}"), ("💳 Огляд усіх тарифів", "help:g:paid")], [("« Меню", "help:main")]]
    return H.PAID_HELP[pid], rows


def help_general(key):
    rows = [[("« Меню довідки", "help:main")]]
    return H.GENERAL[key][1], rows


def help_resolve(arg):
    a = arg.strip().lower()
    aliases = {"помилки": "errors", "ліміти": "limits", "порівняння": "compare", "платні": "paid", "команди": "commands", "нотатки": "notes", "фото": "photos"}
    a = aliases.get(a, a)
    if a in H.GENERAL:
        return help_general(a)
    if a in ("models", "моделі"):
        return help_models()
    pid = resolve_provider(a, catalog=True)
    return help_model(pid) if pid else None


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    page = help_resolve(" ".join(ctx.args)) if ctx.args else None
    text, rows = page or help_main()
    L.visit(update.effective_chat.id, "help:main")
    await show(update, text, rows, edit=False)


def help_screen(data):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "main"
    if action in ("m", "pick"):
        return help_model(parts[2])
    if action == "models":
        return help_models()
    if action == "c":
        return help_category(parts[2] if len(parts) > 2 else "")
    if action == "x":
        return help_main()
    if action == "p":
        return help_paid(parts[2])
    if action == "g":
        return help_general(parts[2])
    return help_main()


async def on_help_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        return await q.answer("Доступ закритий", show_alert=True)
    parts = q.data.split(":")
    if parts[1] == "pick":
        pid = parts[2]
        if pid not in PROVIDERS:
            return await q.answer("Ця модель не підключена", show_alert=True)
        set_choice(q.message.chat_id, pid)
        await q.answer(f"Тепер відповідає: {PROVIDERS[pid]['title']}", show_alert=True)
    else:
        await q.answer()
    await open_data(update, ctx, q.data)


# ---------------------------------------------------------------- Telegram: оформлення

# Розділи бота. Кожен розділ стоїть рівно в одному місці (меню внизу, «Меню», група-підменю або прихований),
# тож кнопки ніде не дублюються. Усе змінюється в «🎛 Кнопки»: місце, назви, групи, порядок, кількість у ряду.
async def cm_ai(prompt):
    """Текст допису від ШІ для 🏘 Спільнот — тими самими моделями й перемиканням, що й звичайні відповіді."""
    token = NO_JOURNAL.set(True)
    set_prio(0, "допис для спільноти")
    try:
        text, *_ = await ask_llm([], prompt)
    finally:
        NO_JOURNAL.reset(token)
    return text


CM = Community(db, lambda: [owner_id()] if owner_id() else [], tz=TZ, ai=cm_ai, on_event=lambda t: hub.info("🏘 " + t))

ITEMS = {
    "dl": ("💬 Розмови", "m:dl"),
    "tl": ("⏰ Завдання", "m:tl"),
    "cr": ("🎨 Створити", "m:cr"),
    "menu": ("☰ Меню", "m:home"),
    "md": ("🤖 Моделі", "m:md"),
    "qask": ("❓ Спитати модель", "m:qask"),
    "st": ("🎛 Поведінка", "m:st"),
    "help": ("📖 Довідка", "help:main"),
    "voice": ("🎙 Голос", "m:voice"),
    "nt": ("📝 Нотатки", "m:nt"),
    "pins": ("📌 Закріплені", "m:pins"),
    "arc": ("🗄 Архів", "m:box:archived"),
    "trash": ("🗑 Кошик", "m:box:trash"),
    "acc": ("👥 Доступ", "m:acc"),
    "backup": ("💾 Копії", "m:backup"),
    "btns": ("🧭 Меню й кнопки", "m:btn"),
    "lib": ("🖼 Мої медіа", "m:lib"),
    "svc": ("🧩 Інтеграції", "m:svc"),
    "diag": ("🩺 Стан бота", "m:diag"),
    "jrn": ("📒 Журнал", "m:jl"),
    "comm": ("🏘 Спільноти", "m:cm"),
    "prio": ("🏆 Пріоритети", "m:pr"),
    "partners": ("🤝 Боти й ШІ", "m:pr:b"),
    "runmode": ("⚡ Режим роботи", "m:rm"),
}
DEFAULT_GROUPS = {
    "work": "💬 Робота",
    "create": "🎨 Створення",
    "ai": "🤖 Моделі та ШІ",
    "services": "🔗 Сервіси",
    "settings": "⚙️ Налаштування",
    "start": "🚀 Початок",
}
DEFAULT_LAYOUT = [
    ["g:work", "home"], ["g:create", "home"], ["g:ai", "home"],
    ["g:services", "home"], ["g:settings", "home"], ["g:start", "home"],
    ["dl", "g:work"], ["tl", "g:work"], ["nt", "g:work"], ["pins", "g:work"],
    ["arc", "g:work"], ["jrn", "g:work"],
    ["cr", "g:create"], ["lib", "g:create"], ["voice", "g:create"],
    ["md", "g:ai"], ["qask", "g:ai"], ["prio", "g:ai"], ["partners", "g:ai"],
    ["svc", "g:services"], ["comm", "g:services"],
    ["st", "g:settings"], ["acc", "g:settings"], ["backup", "g:settings"],
    ["btns", "g:settings"], ["runmode", "g:settings"], ["diag", "g:settings"], ["trash", "g:settings"],
    ["help", "g:start"], ["menu", "off"],
]
L = Layout(ITEMS, DEFAULT_LAYOUT, DEFAULT_GROUPS, lambda c: prefs(c), lambda c, k, v: set_pref(c, k, v),
           locked={"st"}, admin_items={"acc", "backup", "svc", "comm", "runmode"}, is_admin=lambda c: is_admin_id(c), use_kb=False)


def _migrate_compact_menu(chat_id):
    """Міграція меню: шість категорій-акордеонів, без дубльованих назв."""
    p = prefs(chat_id)
    if not p.get("compact_menu_v2"):
        set_pref(chat_id, "groups", dict(DEFAULT_GROUPS))
        set_pref(chat_id, "layout", [list(x) for x in DEFAULT_LAYOUT])
        set_pref(chat_id, "compact_menu_v2", True)
        p = prefs(chat_id)
    if not p.get("accordion_menu_v4"):
        # Повертаємо стандартні назви саме тематичним категоріям, але не чіпаємо довільні користувацькі групи.
        groups = dict(p.get("groups") or DEFAULT_GROUPS)
        for gid, title in DEFAULT_GROUPS.items():
            if gid in groups:
                groups[gid] = title
        set_pref(chat_id, "groups", groups)

        # Старі версії могли зберегти однакову назву для категорії «Налаштування» і її внутрішнього розділу.
        labels = dict(p.get("labels") or {})
        bad_settings = {"⚙️ Налаштування", "🎛 Налаштування", "Налаштування", "🎛 Параметри"}
        if labels.get("st") in bad_settings:
            labels["st"] = "🎛 Поведінка"
        # Аналогічно розділяємо неоднозначні старі назви сервісів/меню.
        if labels.get("svc") in {"🔗 Сервіси", "Сервіси"}:
            labels["svc"] = "🧩 Інтеграції"
        if labels.get("btns") in {"⚙️ Налаштування", "Налаштування", "Меню"}:
            labels["btns"] = "🧭 Меню й кнопки"
        set_pref(chat_id, "labels", labels)
        set_pref(chat_id, "accordion_menu_v4", True)


def layout(chat_id):
    _migrate_compact_menu(chat_id)
    return L.layout(chat_id)


def menu_kb(chat_id):
    """Нижньої клавіатури більше немає: уся навігація — у кнопковому меню. Стару прибираємо лише раз."""
    _migrate_compact_menu(chat_id)
    if prefs(chat_id).get("rk_removed"):
        return None
    set_pref(chat_id, "rk_removed", True)
    return ReplyKeyboardRemove()


HOME = ("« Меню", "m:home")
AWAIT = {}  # chat_id -> (дія, дані): бот чекає від користувача текст (назву, опис, час…)


def slider(value, lo, hi):
    filled = round((value - lo) / (hi - lo) * 8) if hi > lo else 0
    return "▰" * filled + "▱" * (8 - filled)


def seg(options, current):
    """Сегментований перемикач: [(значення, підпис)] -> підписи з позначкою обраного."""
    return [(("● " if v == current else "") + label, v) for v, label in options]


LIST_ROWS = 10  # більше рядків — показуємо перші, решта «Показати ще ▼»


def fold_rows(chat, rows):
    """Довгий список кнопок згортається: видно перші LIST_ROWS рядків, «Показати ще ▼» розгортає вниз, «▲ Згорнути» — назад.
    Рядки навігації («« …», «🏠 Меню») і акордеон головного меню не згортаються."""
    if any(d.startswith(("m:grp:", "m:grpx:", "help:c:", "help:x:")) for row in rows for _, d in row):
        return rows
    tail = []
    body = list(rows)
    while body and all(t.startswith("«") or d == "m:home" for t, d in body[-1]):
        tail.insert(0, body.pop())
    if len(body) <= LIST_ROWS + 2:
        return rows
    cur = L.current(chat)
    st = (prefs(chat).get("fold") or {})
    shown = st.get("n", LIST_ROWS) if st.get("screen") == cur else LIST_ROWS
    shown = max(LIST_ROWS, min(shown, len(body)))
    out = body[:shown]
    ctl = []
    if shown > LIST_ROWS:
        ctl.append(("▲ Згорнути", "m:more:0"))
    if shown < len(body):
        ctl.append((f"Показати ще ({len(body) - shown}) ▼", f"m:more:{shown + LIST_ROWS}"))
    return out + ([ctl] if ctl else []) + tail


async def show(update: Update, text, rows, edit=True, nav=True):
    """Показує екран меню: редагує повідомлення з кнопками або надсилає нове.
    До кожного меню додається ряд ◀️ Назад · 🏠 Додому · Вперед ▶️ (Назад/Вперед — коли є куди)."""
    chat = update.effective_chat.id
    rows = fold_rows(chat, rows)
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


def ask_input(chat_id, action, data=None):
    AWAIT[chat_id] = (action, data)


CANCEL_ROW = [("✖️ Скасувати", "m:cancel")]


# ---------------------------------------------------------------- головне меню


def scr_home(chat_id, open_group=None):
    """Головне меню-акордеон: категорія лишається на місці, а її кнопки з'являються прямо під нею."""
    _migrate_compact_menu(chat_id)
    did = current_dialog(chat_id)
    d = dialog_get(did)
    open_tasks = len(tasks_open(chat_id=chat_id))
    text = (
        "<b>🏠 Головне меню</b>\n\n"
        f"💬 Розмова: <b>{esc(d['title'])}</b>\n"
        f"🤖 Відповідає: <b>{esc(choice_label(get_choice(chat_id)))}</b>\n"
        f"⏰ Завдань в очікуванні: <b>{open_tasks}</b>\n\n"
        "Натисніть ▼, щоб розгорнути розділ. Натисніть ▲, щоб згорнути."
    )
    pending = len(members_list("pending")) if is_admin_id(chat_id) else 0
    badges = {"acc": f" · 🔔 {pending}"} if pending else {}
    if pending:
        badges[L.where(chat_id, "acc")] = f" · 🔔 {pending}"

    if pending:  # значок на всіх групах, крізь які видно «Доступ»
        for g in L.chain(chat_id, L.where(chat_id, "acc")[2:]) if L.where(chat_id, "acc").startswith("g:") else []:
            badges[g] = f" · 🔔 {pending}"
    return text, L.accordion_rows(chat_id, open_group, badges)


# ---------------------------------------------------------------- розмови й теки


def dialog_line(d, current):
    mark = "▶️ " if d["id"] == current else ""
    return f"{mark}{d['title']}"


def scr_dialogs(chat_id):
    cur = current_dialog(chat_id)
    d = dialog_get(cur)
    folders = folders_list(chat_id)
    loose = dialogs_list(chat_id, folder_id=None, limit=8)
    text = (
        "<b>💬 Розмови й теки</b>\n\n"
        f"Зараз: <b>{esc(d['title'])}</b>"
        + (f" · 📁 {esc(folder_get(d['folder_id'])['name'])}" if d["folder_id"] and folder_get(d["folder_id"]) else "")
    )
    rows = [[("➕ Нова розмова", "m:dnew"), ("✏️ Поточна", f"m:d:{cur}")]]
    if folders:
        rows += pairs([(("📌" if f.get("pinned_at") else "📁") + f" {f['name']} · {f['nd'] + f['nt']}", f"m:f:{f['id']}") for f in folders])
    rows.append([("➕ Нова тека", "m:fnew"), ("ℹ️", "help:g:dialogs")])
    for x in loose:
        mark = "▶️ " if x["id"] == cur else ("📌 " if x.get("pinned_at") else "💬 ")
        rows.append([(mark + x["title"], f"m:d:{x['id']}")])
    rows.append([HOME])
    return text, rows


def scr_dialog(chat_id, did):
    d = dialog_get(did)
    if not d or d["chat_id"] != chat_id:
        return scr_dialogs(chat_id)
    cur = current_dialog(chat_id)
    folder = folder_get(d["folder_id"]) if d["folder_id"] else None
    msgs = last_messages(did, 4)
    preview = "\n".join(
        f"{'🧑' if m['role'] == 'user' else '🤖'} {esc(short_q(m['text'], 90))}" for m in msgs
    ) or "<i>поки порожньо</i>"
    text = (
        f"<b>💬 {esc(d['title'])}</b>\n"
        f"📁 {esc(folder['name']) if folder else 'Без теки'} · оновлено {ago(d['updated'])}\n"
        + ("▶️ <b>Це поточна розмова</b>\n" if did == cur else "")
        + f"\n<b>Останнє:</b>\n{preview}"
    )
    rows = []
    if did != cur:
        rows.append([("▶️ Продовжити цю розмову", f"m:dsw:{did}")])
    rows += [
        [("✏️ Перейменувати", f"m:dren:{did}"), ("📁 У теку", f"m:dmv:{did}")],
        [("⏰ Завдання в цю розмову", f"m:tnew:{did}"), pin_button("d", did)],
        [("🗄 В архів", f"m:arc:d:{did}"), ("🗑 У кошик", f"m:trs:d:{did}")],
        [("« Розмови", "m:dl"), HOME],
    ]
    return text, rows


def scr_folder(chat_id, fid):
    f = folder_get(fid)
    if not f or f["chat_id"] != chat_id:
        return scr_dialogs(chat_id)
    cur = current_dialog(chat_id)
    ds = dialogs_list(chat_id, folder_id=fid)
    ts = [t for t in tasks_open(chat_id=chat_id) if t.get("folder_id") == fid]
    text = f"<b>📁 {esc(f['name'])}</b>\n\nРозмов: {len(ds)} · завдань в очікуванні: {len(ts)}"
    rows = [[(("▶️ " if d["id"] == cur else "💬 ") + d["title"], f"m:d:{d['id']}")] for d in ds]
    rows += [[(f"{task_icon(t)} {short_q(t['history_text'], 40)}", f"m:t:{t['id']}")] for t in ts]
    rows += [
        [("➕ Розмова тут", f"m:dnew:{fid}"), ("➕ Завдання тут", f"m:tnewf:{fid}")],
        [("✏️ Перейменувати", f"m:fren:{fid}"), pin_button("f", fid)],
        [("🗄 Теку в архів", f"m:arc:f:{fid}"), ("🗑 Теку в кошик", f"m:trs:f:{fid}")],
        [("« Розмови", "m:dl"), HOME],
    ]
    return text, rows


def scr_move(chat_id, kind, item_id):
    """Вибір теки для розмови (kind='d') або завдання (kind='t')."""
    rows = [[(f"📁 {f['name']}", f"m:mv:{kind}:{item_id}:{f['id']}")] for f in folders_list(chat_id)]
    rows.append([("📂 Без теки", f"m:mv:{kind}:{item_id}:0"), ("➕ Нова тека", f"m:mvnew:{kind}:{item_id}")])
    rows.append([("« Назад", f"m:{kind}:{item_id}")])
    return "<b>📁 Оберіть теку</b>", rows


# ---------------------------------------------------------------- завдання


def task_icon(t):
    return {"scheduled": "🕒", "deferred": "⏳", "choice": "❓"}.get(t["status"], "✅")


def task_when(t):
    if t["status"] == "scheduled" and t.get("run_at"):
        return f"заплановано на {hhmm(t['run_at'])}"
    if t["status"] == "deferred":
        prov = PROVIDERS.get(t["provider"], {}).get("title") if t["provider"] else None
        return f"чекає, поки звільниться {prov}" if prov else "чекає першу вільну модель"
    if t["status"] == "choice":
        return "чекає вашого рішення"
    return t["status"]


def scr_tasks(chat_id):
    items = tasks_open(chat_id=chat_id)
    groups = {"scheduled": [], "deferred": [], "choice": []}
    for t in items:
        groups.setdefault(t["status"], []).append(t)
    text = (
        "<b>⏰ Завдання</b>\n\n"
        f"🕒 Заплановані на час: <b>{len(groups['scheduled'])}</b>\n"
        f"⏳ Чекають вільну модель: <b>{len(groups['deferred'])}</b>\n"
        f"❓ Чекають вашого рішення: <b>{len(groups['choice'])}</b>"
    )
    mode = sort_mode(chat_id, "tasks")
    rows = [sort_row(chat_id, "tasks"), [("➕ Нове відкладене завдання", "m:tnew"), ("ℹ️", "help:g:tasks")]]
    head = None
    for t in sort_tasks(items, mode)[:14]:
        h = task_group_head(t, mode)
        if h and h != head:
            head = h
            rows.append([(f"— {h} —", "m:noop")])
        rows.append([(f"{task_icon(t)} {short_q(t['history_text'], 42)}", f"m:t:{t['id']}")])
    return text, rows


def scr_task(chat_id, tid):
    t = task_get(tid)
    if not t or t["chat_id"] != chat_id:
        return scr_tasks(chat_id)
    d = dialog_get(t["dialog_id"]) if t.get("dialog_id") else None
    f = folder_get(t["folder_id"]) if t.get("folder_id") else None
    model = PROVIDERS.get(t["provider"], {}).get("title") if t["provider"] else "🔁 Авто"
    text = (
        f"<b>{task_icon(t)} Завдання #{t['id']}</b>\n\n"
        f"«{esc(short_q(t['history_text'], 300))}»\n\n"
        f"⏰ {esc(task_when(t))}\n"
        f"🤖 Модель: {esc(model or 'Авто')}\n"
        f"💬 Розмова: {esc(d['title']) if d else '—'}\n"
        f"📁 Тека: {esc(f['name']) if f else 'Без теки'}"
        + (f"\n{dict(START_MODES)[task_start_mode(t)]} у призначений час" if t["status"] == "scheduled" else "")
        + (f"\n\n<i>{esc(t['reason'][:300])}</i>" if t.get("reason") and t["status"] != "scheduled" else "")
    )
    rows = []
    if t["status"] in OPEN:
        rows += [
            [("▶️ Виконати зараз", f"m:trun:{tid}"), ("⏰ Інший час", f"m:twhen:{tid}")],
            [("🤖 Змінити модель", f"m:tmodel:{tid}"), ("📁 У теку", f"m:tmv:{tid}")],
            [("❌ Скасувати", f"task:cancel:{tid}")],
        ]
        if t["status"] == "scheduled":
            other = "manual" if task_start_mode(t) == "auto" else "auto"
            rows.insert(1, [(("🔔 Краще нагадати мене" if other == "manual" else "🚀 Краще запустити саме"), f"m:tsm:{tid}:{other}:t")])
    if t["status"] in OPEN:
        rows.append([pin_button("t", tid), ("🗑 У кошик", f"m:trs:t:{tid}")])
    else:
        rows.append([pin_button("t", tid), ("🗄 В архів", f"m:arc:t:{tid}"), ("🗑 У кошик", f"m:trs:t:{tid}")])
    if is_admin_id(chat_id) and any(SVC.connected(x) for x in SERVICES):
        rows.append([("📤 Надіслати в таск-менеджер", f"m:svp:t:{tid}")])
    rows.append([("« Завдання", "m:tl"), HOME])
    return text, rows


WHEN_OPTIONS = [
    ("15m", "⏱ Через 15 хв"), ("1h", "🕐 Через 1 год"), ("3h", "🕒 Через 3 год"),
    ("eve", "🌆 Сьогодні о 19:00"), ("tom9", "🌅 Завтра о 9:00"), ("free", "⏳ Коли звільниться модель"),
]


START_MODES = [("auto", "🚀 Запустити саме"), ("manual", "🔔 Нагадати, запущу я")]


def task_start_mode(t):
    return t.get("start_mode") or prefs(t["chat_id"]).get("task_start", "auto")


def scr_when(tid):
    t = task_get(tid)
    mode = task_start_mode(t) if t else "auto"
    rows = [[(label, f"m:tsm:{tid}:{v}") for label, v in seg(START_MODES, mode)]]
    rows += pairs([(label, f"m:tw:{tid}:{v}") for v, label in WHEN_OPTIONS])
    rows.append([("✍️ Свій час", f"m:tw:{tid}:custom")])
    return ("<b>⏰ Коли виконати?</b>\n\n"
            "🚀 <b>Запустити саме</b> — у призначений час бот виконає завдання й надішле результат з часом запуску й завершення.\n"
            "🔔 <b>Нагадати</b> — у призначений час бот лише нагадає з кнопкою «▶️ Виконати», а результат надішле після виконання.\n\n"
            "Свій час можна написати так: <code>18:30</code>, <code>25.12 10:00</code> або <code>через 40 хв</code>."), rows


def when_to_ts(opt):
    n = now()
    if opt == "15m":
        return time.time() + 900
    if opt == "1h":
        return time.time() + 3600
    if opt == "3h":
        return time.time() + 3 * 3600
    if opt == "eve":
        t = n.replace(hour=19, minute=0, second=0, microsecond=0)
        return (t if t > n else t + timedelta(days=1)).timestamp()
    if opt == "tom9":
        return (n.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
    return None


def parse_when(text):
    """'18:30', '25.12 10:00', 'через 40 хв', 'через 2 год' -> timestamp або None."""
    s = text.strip().lower()
    m = re.match(r"через\s+(\d+)\s*(хв|хвилин\w*|год\w*|г|дн\w*|д)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = 60 if unit.startswith("хв") else 86400 if unit.startswith("д") else 3600
        return time.time() + n * mult
    m = re.match(r"(?:(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+)?(\d{1,2})[:.](\d{2})$", s)
    if m:
        day, mon, year, hh, mm = m.groups()
        n = now()
        try:
            if day:
                y = int(year) + (2000 if year and len(year) == 2 else 0) if year else n.year
                t = n.replace(year=y, month=int(mon), day=int(day), hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if t < n and not year:
                    t = t.replace(year=t.year + 1)
            else:
                t = n.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if t <= n:
                    t += timedelta(days=1)
        except ValueError:
            return None
        return t.timestamp()
    return None


def scr_task_model(tid):
    rows = [[("🔁 Авто (перша вільна)", f"m:tm:{tid}:auto")]]
    rows += pairs([(f"{provider_icon(p)} {PROVIDERS[p]['title']}", f"m:tm:{tid}:{p}") for p in PROVIDERS])
    rows.append([("« Назад", f"m:t:{tid}")])
    return "<b>🤖 Яка модель виконає завдання?</b>", rows


def create_planned_task(chat_id, text, dialog_id=None, folder_id=None):
    did = dialog_id or current_dialog(chat_id)
    tid = task_create(chat_id, 0, text, text, None, "", dialog_id=did, kind="planned", folder_id=folder_id, status="choice")
    return tid


# ---------------------------------------------------------------- моделі, ліміти, налаштування


def scr_models(chat_id):
    cur = get_choice(chat_id)
    lines = [
        "<b>🤖 Моделі</b>\n",
        f"Зараз відповідає: <b>{esc(choice_label(cur))}</b>",
        "✅ працює · ⏸ на паузі · ⚠️ були помилки · ❌ потрібні дії",
    ]
    paused = [p for p in PROVIDERS if paused_until(p)]
    if paused:
        lines.append("\n<b>⏸ На паузі:</b>")
        lines += [f"• {esc(PROVIDERS[p]['title'])} — знову доступна о {hhmm(paused_until(p))} (ще {left(paused_until(p))})" for p in paused]
        lines.append("<i>Коли пауза мине, бот напише окремо.</i>")
    rows = [[(("● " if cur == "auto" else "") + "🔁 Авто", "m:pick:auto")]]
    rows += pairs([(("● " if p == cur else "") + f"{provider_icon(p)} {PROVIDERS[p]['title']}"
                    + (f" · {left(paused_until(p))}" if paused_until(p) else ""), f"m:pick:{p}") for p in PROVIDERS])
    rows.append([("📊 Ліміти", "m:lim"), ("ℹ️", "help:models")])
    if is_admin_id(chat_id):
        rows.append([("🧩 Мої моделі · " + str(len(custom_rows())), "m:cpl"), ("➕ Додати модель", "m:cpnew")])
    return "\n".join(lines), rows


def scr_limits(pid=None):
    if pid and pid in PROVIDERS:
        text = "<b>📊 Ліміти</b>\n\n" + provider_report(pid)
        rows = [[("🔄 Оновити", f"m:lim:{pid}"), ("ℹ️", f"help:m:{pid}")], [("« Усі ліміти", "m:lim")]]
        return text, rows
    blocks = [provider_report(p, short=True) for p in PROVIDERS]
    text = f"<b>📊 Ліміти й стан</b> · {now().strftime('%H:%M')}\n\n" + "\n\n".join(blocks)
    gen = generators_report()
    if len(text) + len(gen) < 3900:
        text += "\n\n" + gen
    rows = pairs([(f"{provider_icon(p)} {PROVIDERS[p]['title']}", f"m:lim:{p}") for p in PROVIDERS])
    rows.append([("🔄 Оновити", "m:lim"), ("ℹ️", "help:g:errors")])
    rows.append([("« Моделі", "m:md")])
    if len(text) > 3900:
        text = text[:3880] + "…"
    return text, rows


def generators_report():
    lines = ["<b>🎨 Генератори</b>"]
    for pid, g in media.IMAGE_PROVIDERS.items():
        u, e = usage("img:" + pid, "day")
        lines.append(f"{'✅' if g['ready']() else '▫️'} 🖼 {esc(g['title'])} — сьогодні: {u}" + (f", помилок: {e}" if e else ""))
    for pid, g in media.MUSIC_PROVIDERS.items():
        u, e = usage("music:" + pid, "day")
        lines.append(f"{'✅' if g['ready']() else '▫️'} 🎵 {esc(g['title'])} — сьогодні: {u}" + (f", помилок: {e}" if e else ""))
    lines.append(f"{'✅' if media.ffmpeg_ok() else '▫️'} 🎞 Відео-слайдшоу (на сервері бота)")
    for pid, g in media.STT_PROVIDERS.items():
        u, e = usage("stt:" + pid, "day")
        lines.append(f"{'✅' if g['ready']() else '▫️'} 🎙 {esc(g['title'])} — сьогодні: {u}" + (f", помилок: {e}" if e else ""))
    lines.append(f"{'✅' if media.tts_ready() else '▫️'} 🔊 Озвучення (edge-tts)")
    lines.append(f"{'✅ підключено' if canva.connected() else ('▫️ не увійшли' if canva.configured() else '▫️ не налаштовано')} 🖌 Canva")
    return "\n".join(lines)


SHAPE_OPTS = [("square", "⬜ 1:1"), ("portrait", "📱 4:5"), ("landscape", "🖥 16:9"), ("story", "📲 9:16")]
LENGTH_OPTS = [("short", "Коротко"), ("normal", "Звичайно"), ("long", "Детально")]


def scr_settings(chat_id):
    p = prefs(chat_id)
    text = (
        "<b>🎛 Поведінка</b>\n\n"
        f"🧠 <b>Пам'ять розмови:</b> {p['memory']} повідомлень\n{slider(p['memory'], 6, 60)}\n\n"
        f"🎲 <b>Креативність:</b> {p['temp']:.1f}\n{slider(p['temp'], 0, 1.2)}\n\n"
        f"🎞 <b>Слайд у відео:</b> {p['slide']} с\n{slider(p['slide'], 2, 8)}\n\n"
        "📏 Довжина відповідей · 🖼 Формат картинок:"
    )
    rows = [
        [("🧠 ➖", "m:set:memory:-"), ("➕ 🧠", "m:set:memory:+")],
        [("🎲 ➖", "m:set:temp:-"), ("➕ 🎲", "m:set:temp:+")],
        [("🎞 ➖", "m:set:slide:-"), ("➕ 🎞", "m:set:slide:+")],
        [(label, f"m:set:length:{v}") for label, v in seg(LENGTH_OPTS, p["length"])],
        [(label, f"m:set:shape:{v}") for label, v in seg(SHAPE_OPTS, p["shape"])],
        [
            (("🔔 " if p["alerts"] else "🔕 ") + "Сповіщення", "m:set:alerts:toggle"),
            (("🧾 " if p["footer"] else "▫️ ") + "Підпис моделі", "m:set:footer:toggle"),
        ],
        [("🏭 Скинути", "m:fr"), ("ℹ️", "help:g:settings")],
    ]
    return text, rows


def apply_setting(chat_id, key, val):
    p = prefs(chat_id)
    if key == "memory":
        set_pref(chat_id, key, max(6, min(60, p[key] + (6 if val == "+" else -6))))
    elif key == "temp":
        set_pref(chat_id, key, round(max(0.0, min(1.2, p[key] + (0.2 if val == "+" else -0.2))), 1))
    elif key == "slide":
        set_pref(chat_id, key, max(2, min(8, p[key] + (1 if val == "+" else -1))))
    elif key in ("length", "shape"):
        set_pref(chat_id, key, val)
    elif key in ("alerts", "footer"):
        set_pref(chat_id, key, not p[key])
    elif key == "reset":
        keep = p.get("dialog")
        kv_set(f"prefs:{chat_id}", json.dumps({"dialog": keep}))


def use_prefs(chat_id):
    """Передає налаштування користувача (довжина, креативність) у виклики моделей."""
    p = prefs(chat_id)
    max_tokens, hint = LENGTHS.get(p["length"], LENGTHS["normal"])
    OPTS.set({"max_tokens": max_tokens, "temperature": p["temp"], "hint": hint})


# ---------------------------------------------------------------- архів і кошик (екрани)


def month_label(ts):
    d = datetime.fromtimestamp(ts or 0, TZ)
    return f"{MONTHS[d.month - 1].capitalize()} {d.year}"


def days_left(trashed_at):
    return max(0, TRASH_DAYS - int((time.time() - (trashed_at or time.time())) // 86400))


def scr_box(chat_id, st):
    arch = st == "archived"
    counts = {k: len(items_in(chat_id, k, st)) for k in KINDS}
    total = sum(counts.values())
    if arch:
        text = "<b>🗄 Архів</b>\n\n"
    else:
        soon = [days_left(i["trashed_at"]) for k in KINDS for i in items_in(chat_id, k, "trash")]
        text = (
            f"<b>🗑 Кошик</b> · автовидалення через {TRASH_DAYS} днів\n\n"
            + (f"⏳ Найближче видалення: через <b>{min(soon)} дн</b>\n\n" if soon else "")
        )
    if not total:
        text += "<i>Порожньо.</i>"
    rows = pairs([(f"{KINDS[k][2]} {KINDS[k][1]} · {counts[k]}", f"m:bl:{st}:{k}:0") for k in KINDS])
    if not arch and total:
        rows.append([("🧹 Очистити кошик зараз", "m:empty")])
    rows.append([("ℹ️", "help:g:storage"), HOME])
    return text, rows


PER_PAGE = 8


def scr_box_list(chat_id, st, kind, page):
    items = items_in(chat_id, kind, st)
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PER_PAGE : (page + 1) * PER_PAGE]
    date_key = "trashed_at" if st == "trash" else "archived_at"
    head = f"<b>{'🗑 Кошик' if st == 'trash' else '🗄 Архів'} · {KINDS[kind][2]} {KINDS[kind][1]}</b>"
    lines, month, rows = [head], None, []
    for n, it in enumerate(chunk, page * PER_PAGE + 1):
        m = month_label(it.get(date_key))
        if m != month:
            lines.append(f"\n📅 <b>{m}</b>")
            month = m
        when = datetime.fromtimestamp(it.get(date_key) or it["created"], TZ).strftime("%d.%m")
        extra = f" · ще {days_left(it['trashed_at'])} дн" if st == "trash" else ""
        status = ""
        if kind == "t":
            status = {"done": "✅ ", "cancelled": "❌ "}.get(it["status"], "")
        lines.append(f"{n}. {status}{esc(short_q(item_title(kind, it), 60))} · {when}{extra}")
        rows.append([(f"{n}. {status}{short_q(item_title(kind, it), 34)}", f"m:it:{st}:{kind}:{it['id']}")])
    if not chunk:
        lines.append("\n<i>Порожньо.</i>")
    if pages > 1:
        rows.append(
            [
                ("◀️", f"m:bl:{st}:{kind}:{page - 1}") if page else ("·", "m:noop"),
                (f"{page + 1} / {pages}", "m:noop"),
                ("▶️", f"m:bl:{st}:{kind}:{page + 1}") if page < pages - 1 else ("·", "m:noop"),
            ]
        )
    rows.append([("« Назад", f"m:box:{st}"), HOME])
    return "\n".join(lines), rows


def scr_item(chat_id, st, kind, item_id):
    it = item_get(kind, item_id)
    if not it:
        return scr_box(chat_id, st)
    icon, label = KINDS[kind][2], KINDS[kind][1]
    lines = [f"<b>{icon} {esc(short_q(item_title(kind, it), 120))}</b>", ""]
    lines.append(f"Створено: {datetime.fromtimestamp(it['created'], TZ).strftime('%d.%m.%Y %H:%M')}")
    if it.get("archived_at"):
        lines.append(f"В архіві з: {datetime.fromtimestamp(it['archived_at'], TZ).strftime('%d.%m.%Y %H:%M')}")
    if st == "trash" and it.get("trashed_at"):
        lines.append(f"🗑 У кошику з {datetime.fromtimestamp(it['trashed_at'], TZ).strftime('%d.%m.%Y')} · видалиться через <b>{days_left(it['trashed_at'])} дн</b>")
    if kind == "d":
        msgs = last_messages(item_id, 4)
        lines.append("\n<b>Останнє:</b>")
        lines += [f"{'🧑' if m['role'] == 'user' else '🤖'} {esc(short_q(m['text'], 90))}" for m in msgs] or ["<i>порожньо</i>"]
    elif kind == "t":
        lines.append(f"Стан: {task_when(it)}")
    elif kind == "n":
        lines.append("\n" + esc(it["text"][:1500]))
    elif kind == "f":
        nd = len(dialogs_list(chat_id, folder_id=item_id))
        lines.append(f"Розмов усередині: {nd}")
    elif kind == "m":
        lines.append("Файл можна надіслати собі знову кнопкою нижче.")
    rows = [[("📤 Надіслати файл", f"m:msend:{item_id}")]] if kind == "m" else []
    if st == "trash":
        rows.append([("↩️ Відновити", f"m:rs:{st}:{kind}:{item_id}"), ("❌ Видалити назавжди", f"m:pg:{kind}:{item_id}")])
    else:
        if kind == "t" and it.get("status") in ("done", "cancelled"):
            rows.append([("🔁 Створити знову", f"m:redo:{item_id}")])
        elif kind != "t" or it.get("state") == "archived":
            rows.append([("↩️ Повернути з архіву", f"m:rs:{st}:{kind}:{item_id}")])
        rows.append([("🗑 У кошик", f"m:trs:{kind}:{item_id}:box")])
    rows.append([(f"« {label}", f"m:bl:{st}:{kind}:0"), HOME])
    return "\n".join(lines), rows


PIN_SORTS = [("new", "🕒 Нові"), ("az", "🔤 А–Я"), ("type", "🗂 За типом")]


def pinned_target(kind, it):
    return {"d": f"m:d:{it['id']}", "f": f"m:f:{it['id']}", "t": f"m:t:{it['id']}", "n": f"m:note:{it['id']}", "m": f"m:mi:{it['id']}"}[kind]


def scr_pins(chat_id, sort=None):
    sort = sort or prefs(chat_id).get("pin_sort", "new")
    items = pinned_items(chat_id)
    if sort == "az":
        items.sort(key=lambda x: item_title(*x).lower())
    elif sort == "type":
        items.sort(key=lambda x: (list(KINDS).index(x[0]), -x[1]["pinned_at"]))
    else:
        items.sort(key=lambda x: -x[1]["pinned_at"])
    lines = ["<b>📌 Закріплені</b>\n"]
    if not items:
        lines.append("<i>Поки нічого не закріплено.</i>")
    group = None
    for kind, it in items:
        if sort == "type" and group != kind:
            group = kind
            lines.append(f"\n{KINDS[kind][2]} <b>{KINDS[kind][1]}</b>")
        when = datetime.fromtimestamp(it["pinned_at"], TZ).strftime("%d.%m")
        lines.append(f"{KINDS[kind][2]} {esc(short_q(item_title(kind, it), 60))} · <i>з {when}</i>")
    rows = [[(label, f"m:pins:{v}") for label, v in seg(PIN_SORTS, sort)]]
    rows += [[(f"{KINDS[k][2]} {short_q(item_title(k, it), 36)}", pinned_target(k, it))] for k, it in items[:20]]
    rows.append([HOME])
    return "\n".join(lines), rows


def invite_link(code):
    return f"https://t.me/{BOT_USERNAME}?start=inv_{code}" if BOT_USERNAME else f"/start inv_{code}"


def scr_access():
    pending, approved = members_list("pending"), members_list("approved")
    invites = invites_active()
    lines = ["<b>👥 Спільний доступ</b>"]
    for inv in invites:
        uses = "одноразове" if inv["uses_left"] == 1 else ("♾ багаторазове" if inv["uses_left"] is None else f"ще {inv['uses_left']}")
        lines.append(f"\n🔗 {uses}, до {datetime.fromtimestamp(inv['expires'], TZ).strftime('%d.%m')}:\n<code>{esc(invite_link(inv['code']))}</code>")
    pre = preinvites_list()
    for x in pre:
        lines.append(f"\n⏳ Додано вами, чекає згоди: <b>{esc(x['key'])}</b> — людина має відкрити бота й погодитись")
    rows = [[("🔗 Одноразове запрошення", "m:inv:1"), ("♾ На 7 днів", "m:inv:0")], [("➕ Додати за @ім'ям чи ID", "m:addmem")]]
    rows += [[(f"🚫 Скасувати додавання {x['key']}", f"m:prex:{x['key']}")] for x in pre]
    for m in pending:
        who = m["name"] + (f" @{m['username']}" if m["username"] else "")
        rows.append([(f"✅ {who}", f"m:accok:{m['user_id']}"), ("❌", f"m:accno:{m['user_id']}")])
    rows += [[(f"👤 {m['name']}", f"m:mem:{m['user_id']}")] for m in approved]
    rows += [[(f"🚫 Скасувати запрошення …{inv['code'][-4:]}", f"m:invx:{inv['code']}")] for inv in invites]
    rows.append([("ℹ️", "help:g:access"), HOME])
    return "\n".join(lines), rows


def scr_member(user_id):
    m = member_get(user_id)
    if not m:
        return scr_access()
    text = (
        f"<b>👤 {esc(m['name'] or str(user_id))}</b>\n"
        + (f"@{esc(m['username'])}\n" if m["username"] else "")
        + f"ID: <code>{user_id}</code>\nСтатус: {'✅ має доступ' if m['status'] == 'approved' else m['status']}\n"
        + (f"Доступ з: {datetime.fromtimestamp(m['approved_at'], TZ).strftime('%d.%m.%Y')}" if m.get("approved_at") else "")
    )
    n = len(member_dialogs(user_id))
    view = bool(m.get("can_view"))
    text += f"\n👁 Перегляд розмов: {'дозволено' if view else 'вимкнено'}"
    rows = [
        [("👁 Вимкнути перегляд розмов" if view else "👁 Увімкнути перегляд розмов", f"m:mview:{user_id}")],
    ]
    if view:
        rows.append([(f"💬 Розмови учасника · {n}", f"m:mdl:{user_id}")])
    rows += [
        [("🚫 Забрати доступ", f"m:accno:{user_id}"), ("🗑 Видалити зі списку", f"m:accrm:{user_id}")],
        [("« Доступ", "m:acc"), HOME],
    ]
    return text, rows


def can_view(user_id):
    m = member_get(user_id)
    return bool(m and m.get("can_view"))


def member_dialogs(user_id):
    with closing(db()) as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT d.*, (SELECT COUNT(*) FROM messages m WHERE m.dialog_id=d.id) AS n FROM dialogs d "
                "WHERE chat_id=? AND state!='trash' ORDER BY updated DESC",
                (user_id,),
            ).fetchall()
        ]


def scr_member_dialogs(user_id):
    m = member_get(user_id) or {"name": str(user_id)}
    ds = member_dialogs(user_id)
    text = f"<b>💬 Розмови: {esc(m['name'] or str(user_id))}</b>\n\n"
    text += "\n".join(
        f"• {esc(d['title'])} · {d['n']} повідомл. · {datetime.fromtimestamp(d['updated'], TZ).strftime('%d.%m %H:%M')}"
        + (" · 🗄" if d["state"] == "archived" else "")
        for d in ds
    ) or "<i>Розмов поки немає.</i>"
    rows = [[(f"💬 {short_q(d['title'], 36)}", f"m:mdv:{d['id']}:0")] for d in ds[:15]]
    rows.append([("« Учасник", f"m:mem:{user_id}"), HOME])
    return text, rows


def scr_member_dialog(did, page):
    d = dialog_get(did)
    if not d:
        return scr_access()
    with closing(db()) as con:
        rows_ = [dict(r) for r in con.execute("SELECT * FROM messages WHERE dialog_id=? ORDER BY id", (did,)).fetchall()]
    per = 8
    pages = max(1, (len(rows_) + per - 1) // per)
    page = pages - 1 if page < 0 else max(0, min(page, pages - 1))
    chunk = rows_[page * per : (page + 1) * per]
    lines = [f"<b>💬 {esc(d['title'])}</b> · сторінка {page + 1}/{pages}\n"]
    for m in chunk:
        who = "🧑" if m["role"] == "user" else "🤖 " + (PROVIDERS.get(m["provider"], {}).get("title", "") if m["provider"] else "")
        when = datetime.fromtimestamp(m["ts"], TZ).strftime("%d.%m %H:%M")
        lines.append(f"<b>{who}</b> <i>{when}</i>\n{esc(short_q(m['text'], 400))}\n")
    text = "\n".join(lines)[:3900]
    rows = []
    if pages > 1:
        rows.append([
            ("◀️", f"m:mdv:{did}:{page - 1}") if page else ("·", "m:noop"),
            (f"{page + 1} / {pages}", "m:noop"),
            ("▶️", f"m:mdv:{did}:{page + 1}") if page < pages - 1 else ("·", "m:noop"),
        ])
    rows.append([("📥 Копія до моїх розмов", f"m:mdc:{did}")])
    rows.append([("« Розмови учасника", f"m:mdl:{d['chat_id']}"), HOME])
    return text, rows


def copy_dialog_to(did, chat_id):
    """Копіює розмову учасника власнику, щоб продовжити її від себе."""
    d = dialog_get(did)
    m = member_get(d["chat_id"]) or {"name": str(d["chat_id"])}
    new = dialog_create(chat_id, f"📥 {(m['name'] or '')[:15]}: {d['title']}"[:60])
    with closing(db()) as con, con:
        con.execute(
            "INSERT INTO messages(chat_id, role, text, source, provider, in_context, ts, pending, dialog_id) "
            "SELECT ?, role, text, source, provider, in_context, ts, 0, ? FROM messages WHERE dialog_id=? ORDER BY id",
            (chat_id, new, did),
        )
    return new


def scr_note(chat_id, nid):
    n = item_get("n", nid)
    if not n or n.get("state") != "active":
        return scr_notes(chat_id)
    text = f"<b>📝 Нотатка #{nid}</b>\n{datetime.fromtimestamp(n['ts'], TZ).strftime('%d.%m.%Y %H:%M')}\n\n{esc(n['text'])}"
    rows = [[pin_button("n", nid)] + ([("📤 Надіслати в…", f"m:svp:n:{nid}")] if is_admin_id(chat_id) and any(SVC.connected(x) for x in SERVICES) else []),
            [("🗄 В архів", f"m:arc:n:{nid}"), ("🗑 У кошик", f"m:trs:n:{nid}")], [("« Нотатки", "m:nt"), HOME]]
    return text, rows


# ---------------------------------------------------------------- нотатки


# ---------------------------------------------------------------- сортування списків

SORTS = {
    "notes": [("new", "🆕 Нові"), ("old", "📜 Старі"), ("az", "🔤 А–Я"), ("pin", "📌 Спершу")],
    "tasks": [("when", "⏰ Час"), ("new", "🆕 Нові"), ("status", "🗂 Стан"), ("model", "🤖 Модель")],
    "media": [("new", "🆕 Нові"), ("old", "📜 Старі"), ("type", "🗂 Тип"), ("az", "🔤 А–Я")],
}


def sort_mode(chat_id, key):
    mode = prefs(chat_id).get(f"sort_{key}")
    return mode if mode in dict(SORTS[key]) else SORTS[key][0][0]


def sort_row(chat_id, key):
    return [(label, f"m:sort:{key}:{v}") for label, v in seg(SORTS[key], sort_mode(chat_id, key))]


def sort_tasks(items, mode):
    if mode == "new":
        return sorted(items, key=lambda t: -t["created"])
    if mode == "status":
        order = {"choice": 0, "scheduled": 1, "deferred": 2}
        return sorted(items, key=lambda t: (order.get(t["status"], 3), t.get("run_at") or t["created"]))
    if mode == "model":
        return sorted(items, key=lambda t: ((PROVIDERS.get(t["provider"] or "", {}).get("title") or "Авто"), t.get("run_at") or t["created"]))
    return sorted(items, key=lambda t: t.get("run_at") or t["created"])


def task_group_head(t, mode):
    if mode == "status":
        return {"choice": "❓ Чекають рішення", "scheduled": "🕒 Заплановані", "deferred": "⏳ Чекають модель"}.get(t["status"])
    if mode == "model":
        return "🤖 " + (PROVIDERS.get(t["provider"] or "", {}).get("title") or "Авто")
    return None


# ---------------------------------------------------------------- нотатки


def scr_notes(chat_id, page=0):
    mode = sort_mode(chat_id, "notes")
    notes = list_notes_db(limit=1000, chat_id=chat_id)
    if mode == "old":
        notes.sort(key=lambda n: n["id"])
    elif mode == "az":
        notes.sort(key=lambda n: n["text"].lower())
    elif mode == "pin":
        notes.sort(key=lambda n: (not n["pinned"], -n["id"]))
    else:
        notes.sort(key=lambda n: -n["id"])
    pages = max(1, (len(notes) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = notes[page * PER_PAGE : (page + 1) * PER_PAGE]
    text = f"<b>📝 Нотатки</b> · {len(notes)}\n\n"
    text += "\n".join(f"{'📌' if n['pinned'] else '•'} #{n['id']} · {esc(short_q(n['text'], 80))} · <i>{n['date'][8:10]}.{n['date'][5:7]}</i>" for n in chunk) if chunk else "<i>Поки порожньо.</i>"
    rows = [sort_row(chat_id, "notes"), [("➕ Додати", "m:nadd"), ("🔍 Знайти", "m:nfind")]]
    rows += pairs([(("📌" if n["pinned"] else "📝") + f" #{n['id']} {short_q(n['text'], 18)}", f"m:note:{n['id']}") for n in chunk])
    if pages > 1:
        rows.append(page_row("m:nt", page, pages))
    return text, rows


def page_row(prefix, page, pages, suffix=""):
    return [
        ("⏪", f"{prefix}:{page - 1}{suffix}") if page else ("·", "m:noop"),
        (f"{page + 1} / {pages}", "m:noop"),
        ("⏩", f"{prefix}:{page + 1}{suffix}") if page < pages - 1 else ("·", "m:noop"),
    ]


# ---------------------------------------------------------------- 🖼 мої медіа (усе створене генераторами)


def media_save(chat_id, kind, sent, data, cap):
    """Запам'ятовує створений файл у бібліотеці (за file_id Telegram — без копії на диску)."""
    file_id = None
    if sent is not None:
        obj = (sent.photo[-1] if sent.photo else None) or sent.audio or sent.video or sent.document
        file_id = getattr(obj, "file_id", None)
    if kind == "url" and not file_id:
        file_id = data
    if not file_id:
        return
    with closing(db()) as con, con:
        con.execute("INSERT INTO media (chat_id, kind, file_id, prompt, created) VALUES (?,?,?,?,?)",
                    (chat_id, kind if kind != "url" or not sent or not sent.photo else "photo", file_id, cap or "", time.time()))


def media_list(chat_id, kind=None):
    q = "SELECT * FROM media WHERE chat_id=? AND state='active'" + (" AND kind=?" if kind else "")
    with closing(db()) as con:
        return [dict(r) for r in con.execute(q, (chat_id, kind) if kind else (chat_id,))]


MEDIA_FILTERS = [("all", "Усе"), ("photo", "🖼"), ("video", "🎞"), ("audio", "🎵")]


def scr_media(chat_id, page=0, filt=None):
    filt = filt or prefs(chat_id).get("media_filter", "all")
    if filt != prefs(chat_id).get("media_filter"):
        set_pref(chat_id, "media_filter", filt)
    mode = sort_mode(chat_id, "media")
    items = media_list(chat_id, None if filt == "all" else filt)
    if mode == "old":
        items.sort(key=lambda m: m["created"])
    elif mode == "az":
        items.sort(key=lambda m: m["prompt"].lower())
    elif mode == "type":
        items.sort(key=lambda m: (m["kind"], -m["created"]))
    else:
        items.sort(key=lambda m: -m["created"])
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PER_PAGE : (page + 1) * PER_PAGE]
    lines = [f"<b>🖼 Мої медіа</b> · {len(items)}", "Усе, що ви створили: картинки, слайдшоу, музика."]
    head = None
    for m in chunk:
        if mode == "type" and head != m["kind"]:
            head = m["kind"]
            lines.append(f"\n<b>{MEDIA_ICONS.get(head, '🖼')} {dict(photo='Картинки', video='Слайдшоу', audio='Музика').get(head, 'Посилання')}</b>")
        when = datetime.fromtimestamp(m["created"], TZ).strftime("%d.%m")
        lines.append(f"{'📌' if m['pinned_at'] else MEDIA_ICONS.get(m['kind'], '🖼')} {esc(short_q(m['prompt'], 60))} · <i>{when}</i>")
    if not chunk:
        lines.append("\n<i>Поки порожньо. Створіть щось у «🎨 Створити».</i>")
    rows = [sort_row(chat_id, "media"), [(label, f"m:lib:0:{v}") for label, v in seg(MEDIA_FILTERS, filt)]]
    rows += [[(f"{MEDIA_ICONS.get(m['kind'], '🖼')} {short_q(m['prompt'], 36)}", f"m:mi:{m['id']}")] for m in chunk]
    if pages > 1:
        rows.append(page_row("m:lib", page, pages, f":{filt}"))
    return "\n".join(lines), rows


def scr_media_item(chat_id, mid):
    m = item_get("m", mid)
    if not m or m["chat_id"] != chat_id or m["state"] != "active":
        return scr_media(chat_id)
    when = datetime.fromtimestamp(m["created"], TZ).strftime("%d.%m.%Y %H:%M")
    text = f"<b>{MEDIA_ICONS.get(m['kind'], '🖼')} {esc(short_q(m['prompt'], 200))}</b>\n\nСтворено: {when}"
    rows = [[("📤 Надіслати файл", f"m:msend:{mid}"), pin_button("m", mid)], [("🗄 В архів", f"m:arc:m:{mid}"), ("🗑 У кошик", f"m:trs:m:{mid}")]]
    return text, rows


async def resend_media(bot, chat_id, m):
    fid, cap = m["file_id"], m["prompt"][:1000]
    send = {"photo": bot.send_photo, "audio": bot.send_audio, "video": bot.send_video}.get(m["kind"], bot.send_document)
    try:
        await send(chat_id, fid, caption=cap)
    except Exception as e:  # noqa: BLE001
        await bot.send_message(chat_id, f"⚠️ Не вдалося надіслати файл: {e}")


# ---------------------------------------------------------------- створити (генератори)


def scr_create(chat_id):
    p = prefs(chat_id)
    music_ready = any(g["ready"]() for g in media.MUSIC_PROVIDERS.values())
    text = (
        "<b>🎨 Створити</b>\n\n"
        f"Формат картинок: {dict(SHAPE_OPTS)[p['shape']]} · слайд: {p['slide']} с"
        + ("" if music_ready else "\nМузика не підключена.")
    )
    rows = [
        [("🖼 Картинка", "m:gen:image"), ("🎞 Слайдшоу", "m:gen:video")],
        [("🎵 Музика", "m:gen:music"), ("🖌 Canva", "m:canva")],
        [("ℹ️", "help:g:create")],
    ]
    return text, rows


GEN_PROMPTS = {
    "image": "🖼 <b>Опишіть картинку.</b>\nНайкраще англійською й детально, наприклад:\n<code>green city park in spring, people cleaning, sunny, photo</code>",
    "video": "🎞 <b>Опишіть слайди</b> — кожен з нового рядка або через «|». Текст рядка стане підписом і описом картинки.\n"
    "Музику можна додати окремим рядком: <code>музика: calm lo-fi</code>\n\nНаприклад:\n"
    "<code>Ранок у парку\nВолонтери прибирають\nЧисто й затишно\nмузика: hopeful acoustic</code>",
    "music": "🎵 <b>Опишіть музику</b> англійською: жанр, настрій, інструменти.\nНаприклад: <code>calm lo-fi beat with soft piano</code>",
    "canva": "🖌 <b>Що зробити в Canva?</b>\nНаприклад: «пост для Instagram про весняне прибирання парку, зелені кольори»",
    "canva_find": "🔍 <b>Що знайти серед ваших дизайнів Canva?</b> Напишіть слово з назви.",
}


def scr_canva(chat_id):
    if not is_admin_id(chat_id):
        return "<b>🖌 Canva</b>\n\nCanva підключена до акаунта власника бота, тому доступна лише йому.", [[("« Створити", "m:cr")]]
    if not canva.configured():
        text = (
            "<b>🖌 Canva</b>\n\n▫️ Ще не налаштовано на сервері.\n\n"
            "Потрібні змінні <code>CANVA_CLIENT_ID</code>, <code>CANVA_CLIENT_SECRET</code> і <code>PUBLIC_URL</code>."
        )
        return text, [[("ℹ️ Як підключити", "help:g:canva")], [("« Створити", "m:cr")]]
    if not canva.connected():
        text = "<b>🖌 Canva</b>\n\nПотрібно один раз увійти у ваш акаунт Canva. Після входу бот напише, що все готово."
        return text, [[("🔑 Увійти в Canva", f"url:{canva.login_url(chat_id)}")], [("« Створити", "m:cr")]]
    text = "<b>🖌 Canva</b> · ✅ підключено"
    rows = [
        [("✨ Створити дизайн", "m:gen:canva"), ("🔍 Мої дизайни", "m:gen:canva_find")],
        [("🚪 Вийти з Canva", "m:canvaout"), ("ℹ️", "help:g:canva")],
        [("« Створити", "m:cr")],
    ]
    return text, rows


def _button(t, d):
    if d.startswith("url:"):
        return InlineKeyboardButton(t, url=d[4:])
    if d.startswith("copy:"):  # копіювання тексту одним натисканням (до 256 символів)
        return InlineKeyboardButton(t, copy_text=CopyTextButton(d[5:][:256]))
    return InlineKeyboardButton(t, callback_data=d)


# Telegram не дає змінювати розмір шрифту чи переносити текст у кнопці. Тому правило одне:
# у ряд по дві ставимо лише короткі підписи, довгі — кожну на всю ширину. Підписи самі по собі вже короткі (див. ITEMS).
PAIR_MAX = 18  # видимих символів для кнопки «пів ширини» на iPhone


def _fit_button_rows(rows):
    out = []
    for row in rows:
        row = [(str(t), d) for t, d in row]
        if len(row) == 2 and any(len(t) > PAIR_MAX for t, _ in row) and not any(d.startswith(("m:nav:", "m:prm:", "m:pv")) for _, d in row):
            out.extend([[x] for x in row])
        else:
            out.append(row)
    return out


def kb(rows):
    """Кнопки: (підпис, дані). "url:…" — посилання, "copy:…" — скопіювати текст, інше — дія бота."""
    return InlineKeyboardMarkup([[_button(t, d) for t, d in row] for row in _fit_button_rows(rows)])


async def on_ui_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        return await q.answer("Доступ закритий", show_alert=True)
    # Кнопки «ui:…» лишилися в старих повідомленнях попередньої версії — просто відкриваємо поточний екран заново.
    await q.answer("Меню оновлено")
    chat_id = update.effective_chat.id
    return await open_data(update, ctx, L.current(chat_id) or "m:home", record=False)


async def media_error_text(e, what):
    lines = [f"⚠️ Не вдалося створити {what}."]
    if e.provider == "all":
        lines.append(f"Причини: {e.detail}")
    else:
        lines.append(f"{e.provider}: {e.kind} — {e.detail[:200]}")
    lines.append("Стан генераторів: /limits")
    return "\n".join(lines)


async def do_image(bot, chat_id, prompt):
    shape = prefs(chat_id)["shape"]
    w, h = SHAPES.get(shape, SHAPES["square"])
    try:
        async with Progress(bot, chat_id, "Малюю картинку"):
            data, pid, errors = await media.generate_image(prompt, w, h)
    except media.MediaError as e:
        for p in [x.strip() for x in e.detail.split(";") if ":" in x]:
            count_request("img:" + p.split(":")[0], error=True)
        return await bot.send_message(chat_id, await media_error_text(e, "картинку"))
    for p, _ in errors:
        count_request("img:" + p, error=True)
    count_request("img:" + pid)
    skipped = "".join(f"\n⚠️ {media.IMAGE_PROVIDERS[p]['title']}: {x.kind}" for p, x in errors)
    await send_media(bot, chat_id, [("photo", data, f"🖼 {media.IMAGE_PROVIDERS[pid]['title']} · {short_q(prompt, 80)}{skipped}")])
    await bot.send_message(chat_id, "Ще варіант?", reply_markup=kb([[("🔁 Ще раз", "m:again:image"), ("🎨 Створити", "m:cr")]]))
    LAST_PROMPT[(chat_id, "image")] = prompt


async def do_music(bot, chat_id, prompt):
    try:
        async with Progress(bot, chat_id, "Створюю музику (буває до хвилини)"):
            data, pid, errors = await media.generate_music(prompt)
    except media.MediaError as e:
        return await bot.send_message(chat_id, await media_error_text(e, "музику") + "\nЯк підключити музику: /help create")
    count_request("music:" + pid)
    await send_media(bot, chat_id, [("audio", data, f"🎵 {media.MUSIC_PROVIDERS[pid]['title']} · {short_q(prompt, 80)}")])
    LAST_PROMPT[(chat_id, "music")] = prompt


async def do_video(bot, chat_id, text):
    lines = [x.strip() for x in re.split(r"\n|\|", text) if x.strip()]
    music_prompt = None
    slides_text = []
    for ln in lines:
        m = re.match(r"(музика|music)\s*:\s*(.+)", ln, re.I)
        if m:
            music_prompt = m.group(2)
        else:
            slides_text.append(ln)
    if not slides_text:
        return await bot.send_message(chat_id, "Потрібен хоча б один слайд.")
    slides_text = slides_text[:10]
    p = prefs(chat_id)
    status = await bot.send_message(chat_id, f"🎞 Генерую слайди: 0/{len(slides_text)}…")
    slides = []
    for i, cap in enumerate(slides_text, 1):
        try:
            data, pid, _ = await media.generate_image(cap, 1080, 1920, seed=i)
            count_request("img:" + pid)
        except media.MediaError as e:
            return await status.edit_text(await media_error_text(e, f"слайд {i}"))
        slides.append((data, cap))
        await status.edit_text(f"🎞 Генерую слайди: {i}/{len(slides_text)}…")
    music = None
    if music_prompt:
        await status.edit_text("🎵 Генерую музику…")
        try:
            music, mpid, _ = await media.generate_music(music_prompt)
            count_request("music:" + mpid)
        except media.MediaError as e:
            await bot.send_message(chat_id, f"⚠️ Музику не вдалося згенерувати ({e.kind}), відео буде без звуку.")
    await status.edit_text("🎬 Збираю відео…")
    try:
        video = await media.make_slideshow(slides, music, (1080, 1920), p["slide"])
    except media.MediaError as e:
        return await status.edit_text(f"⚠️ Не вдалося зібрати відео: {e.detail[:300]}")
    await status.delete()
    await send_media(bot, chat_id, [("video", video, f"🎞 Слайдшоу · {len(slides)} слайдів")])


LAST_PROMPT = {}


async def do_canva_find(bot, chat_id, query):
    try:
        tools = {t.name: t for t in await canva.list_tools()}
        tool = tools.get("search-designs")
        if not tool:
            return await bot.send_message(chat_id, "У Canva немає інструмента пошуку дизайнів.")
        props = (tool.input_schema or {}).get("properties", {})
        key = next((k for k in ("query", "search", "q") if k in props), None)
        text, urls = await canva.call_tool("search-designs", {key: query} if key and query else {})
    except Exception as e:  # noqa: BLE001
        return await bot.send_message(chat_id, f"⚠️ Canva: {e}")
    found = canva_designs_from(text)
    if found:
        msg = "<b>🖌 Ваші дизайни</b>\n\n" + "\n".join(f"• <a href=\"{esc(u)}\">{esc(t)}</a>" for t, u in found[:15])
        await bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
    else:
        await send_long(bot, chat_id, "🖌 Canva відповіла:\n" + text[:3500])


def canva_designs_from(text):
    out = []
    try:
        data = json.loads(text[text.index("{"):]) if "{" in text else None
    except ValueError:
        data = None

    def walk(o):
        if isinstance(o, dict):
            title = o.get("title") or o.get("name")
            url = None
            for k in ("edit_url", "view_url", "url"):
                if isinstance(o.get(k), str):
                    url = o[k]
                    break
            if not url and isinstance(o.get("urls"), dict):
                url = o["urls"].get("edit_url") or o["urls"].get("view_url")
            if title and url:
                out.append((title, url))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


# ---------------------------------------------------------------- відкладені завдання

OPEN = ("choice", "deferred", "scheduled")


def task_create(chat_id, msg_id, content, history_text, provider, reason, dialog_id=None, kind="fallback", folder_id=None, status="choice", run_at=None):
    with closing(db()) as con, con:
        return con.execute(
            "INSERT INTO tasks(chat_id, msg_id, content_json, history_text, provider, status, reason, created, dialog_id, kind, folder_id, run_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, msg_id, json.dumps(content, ensure_ascii=False), history_text, provider, status, reason,
             time.time(), dialog_id or current_dialog(chat_id), kind, folder_id, run_at),
        ).lastrowid


def task_get(tid):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return dict(row) if row else None


def task_update(tid, **fields):
    with closing(db()) as con, con:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), tid))


def tasks_open(chat_id=None, status=None):
    q, args = f"SELECT * FROM tasks WHERE state='active' AND status IN ({','.join('?' * len(OPEN))})", list(OPEN)
    if chat_id is not None:
        q += " AND chat_id=?"
        args.append(chat_id)
    if status:
        q += " AND status=?"
        args.append(status)
    with closing(db()) as con:
        return [dict(r) for r in con.execute(q + " ORDER BY id", args).fetchall()]


def short_q(t, n=120):
    t = (t or "").replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def reasons_text(attempts):
    return "\n".join(attempt_line(p, i) for p, i in attempts)


def options_keyboard(task):
    tid, prov = task["id"], task["provider"]
    rows = []
    if prov and prov in PROVIDERS:
        until = paused_until(prov)
        when = f" (≈ о {hhmm(until)})" if until else ""
        rows.append([(f"⏳ Дочекатися {PROVIDERS[prov]['title']}{when}", f"task:defer:{tid}")])
        rows.append([("🔁 Продовжити в «Авто»", f"task:auto:{tid}"), ("🕒 На певний час", f"m:twhen:{tid}")])
    else:
        rows.append([("⏳ Виконати, щойно звільниться модель", f"task:defer:{tid}")])
        rows.append([("🕒 На певний час", f"m:twhen:{tid}")])
    others = [p for p in PROVIDERS if p != prov and not paused_until(p)]
    rows += pairs([(f"▶️ {PROVIDERS[p]['title']}", f"task:run:{tid}:{p}") for p in others])
    rows.append([("📁 У теку", f"m:tmv:{tid}"), ("❌ Скасувати", f"task:cancel:{tid}")])
    return kb(rows)


async def offer_options(bot, task, attempts, intro="⏸ Зараз не вдалося відповісти"):
    text = (
        f"<b>{esc(intro)}</b>\n"
        f"Завдання #{task['id']}: «{esc(short_q(task['history_text']))}»\n\n"
        + (f"{esc(reasons_text(attempts))}\n\n" if attempts else "")
        + "<b>Що зробити?</b>\n"
        "• <b>Дочекатися</b> — бот сам виконає завдання, щойно модель звільниться.\n"
        "• <b>На певний час</b> — виконає тоді, коли скажете.\n"
        "• <b>Інша модель</b> — продовжить з того самого місця: їй доступна вся попередня розмова, "
        "зокрема відповіді інших моделей."
    )
    await bot.send_message(task["chat_id"], text, parse_mode=ParseMode.HTML, reply_markup=options_keyboard(task))


def task_content(task):
    return json.loads(task["content_json"])


async def complete_task(bot, task, text, pid, attempts, warnings, deferred=False):
    did = task.get("dialog_id") or current_dialog(task["chat_id"])
    if task["msg_id"]:
        # переносимо питання з відповіддю в кінець історії, щоб розмова йшла по порядку
        set_msg(task["msg_id"], pending=0, in_context=0)
    save_message(task["chat_id"], "user", task["history_text"], dialog_id=did)
    save_message(task["chat_id"], "assistant", text, provider=pid, dialog_id=did)
    task_update(task["id"], status="done", last_try=time.time())
    with closing(db()) as con, con:  # відкладений запит у журналі тепер виконано
        con.execute("UPDATE journal SET status='continued', updated=? WHERE task_id=? AND status IN ('deferred','failed')", (time.time(), task["id"]))
    d = dialog_get(did)
    head = ""
    if deferred:
        fin = time.time()
        st = task.get("started_at") or fin
        took = int(fin - st)
        took_s = f"{took // 60} хв {took % 60} с" if took >= 60 else f"{took} с"
        head = (f"✅ Завдання #{task['id']} виконано: «{short_q(task['history_text'], 150)}»\n"
                f"🚀 Запуск {datetime.fromtimestamp(st, TZ).strftime('%H:%M')} · 🏁 Завершено {datetime.fromtimestamp(fin, TZ).strftime('%H:%M')} · ⏱ {took_s}\n"
                f"💬 Розмова: {d['title'] if d else '—'}\n\n")
    show_footer = prefs(task["chat_id"])["footer"] or attempts or warnings
    tail = ("\n\n" + footer(pid, attempts, warnings)) if show_footer else ""
    await send_long(bot, task["chat_id"], head + text + tail)
    await send_media(bot, task["chat_id"], MEDIA.get())


async def run_task(bot, task, preferred, fallback, deferred=False):
    """Пробує виконати завдання. True — виконано; інакше повертає спроби (attempts)."""
    CHAT.set(task["chat_id"])
    use_prefs(task["chat_id"])
    did = task.get("dialog_id") or current_dialog(task["chat_id"])
    set_prio(RK.score("t", task["id"]) or dialog_score(did), f"Завдання #{task['id']}", chat=task["chat_id"])
    history = load_history(task["chat_id"], before_id=task["msg_id"] or None, dialog_id=did)
    try:
        jtok = JOURNAL.set({"kind": "task", "task_id": task["id"]})
        try:
            text, pid, attempts, warnings = await ask_llm(history, task_content(task), preferred, fallback)
        finally:
            JOURNAL.reset(jtok)
    except Unavailable as u:
        task_update(task["id"], last_try=time.time(), reason=reasons_text(u.attempts))
        return u.attempts
    except RuntimeError as e:
        return [(preferred or "gemini", {"kind": "bad_request", "detail": str(e)})] if preferred in PROVIDERS else []
    await complete_task(bot, task, text, pid, attempts, warnings, deferred)
    return True


RATE_KINDS = ("rate_minute", "rate_day", "rate_month", "rate")


# ---------------------------------------------------------------- 💾 резервні копії

BK = Backups(DB_PATH, kv_get, kv_set, env("PUBLIC_URL", ""), name="bot", project_root=os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 🔗 сервіси: таск-менеджери й блокноти
SVC = Services(kv_get, kv_set, env("PUBLIC_URL", ""))
SVC_LAST = {}  # chat -> {sid: [id елементів останнього показаного списку]}
KIND_NAMES = {"task": "завдання", "note": "нотатки"}


def scr_services(chat_id):
    c = SVC.cfg()
    lines = ["<b>🔗 Сервіси</b>", "", "Таск-менеджери й блокноти, куди бот може надсилати ваші завдання й нотатки.", ""]
    for sid, s in SERVICES.items():
        st = "✅ підключено" if SVC.connected(sid) else ("▫️ можна підключити" if SVC.oauth_ready(sid) else "▫️ потрібен застосунок Google/Microsoft (як для копій)")
        lines.append(f"{s['icon']} <b>{s['title']}</b> — {', '.join(KIND_NAMES[k] for k in s['kinds'])} · {st}")
    lines += ["", f"📝 Нотатки за замовчуванням → <b>{esc(SERVICES[SVC.default('note')]['title']) if SVC.default('note') else '—'}</b>"
              + (" · автокопія нових нотаток ✅" if c["auto"].get("note") else ""),
              f"⏰ Завдання за замовчуванням → <b>{esc(SERVICES[SVC.default('task')]['title']) if SVC.default('task') else '—'}</b>",
              "", "Можна й словами: «додай у Todoist купити хліб завтра», «збережи це в Notion».", "", f"<i>{NOT_SUPPORTED}</i>"]
    rows = pairs([(f"{s['icon']} {s.get('btn', s['title'])}" + (" ✅" if SVC.connected(sid) else ""), f"m:sv:{sid}") for sid, s in SERVICES.items()])
    if SVC.connected_for("note"):
        rows.append([(("✅ " if c["auto"].get("note") else "▫️ ") + "Копіювати кожну нову нотатку", "m:svauto:note")])
    rows.append([("ℹ️", "help:g:services")])
    return "\n".join(lines), rows


def scr_service(chat_id, sid):
    s, conn = SERVICES[sid], SVC.connected(sid)
    acc = SVC.cfg()["acc"].get(sid, {})
    lines = [f"<b>{s['icon']} {s['title']}</b> · {', '.join(KIND_NAMES[k] for k in s['kinds'])}", ""]
    rows = []
    if conn and sid == "iphone":
        url = f"{env('PUBLIC_URL', '').rstrip('/')}/{MCP_SECRET}/iphone?key={acc['key']}"
        lines += ["✅ Підключено. Що чекає на iPhone: " + str(len(SVC.iphone_queue())), "",
                  "<b>Як налаштувати «Команди» (один раз, 5 хв):</b>",
                  "1. «Команди» → «+» → назва «Забрати з бота».",
                  "2. Дія «Отримати вміст URL» → вставте адресу (кнопка «📋 Адреса» нижче).",
                  "3. «Отримати значення словника» → ключ <code>items</code>.",
                  "4. «Повторити з кожним» → усередині «Якщо» <i>kind</i> = <code>task</code>: «Додати нове нагадування» (назва — <i>title</i>, примітки — <i>notes</i>, дата — <i>due</i>); інакше — «Створити нотатку» з <i>title</i> і <i>notes</i>.",
                  "5. Вкладка «Автоматизація» → «+» → «Час доби» → наприклад, щогодини або щоранку → «Запускати одразу» → оберіть команду «Забрати з бота».",
                  "", "<b>Із iPhone у бот:</b> ще одна команда «У бот» в меню «Поділитися»: «Отримати вміст URL» → та сама адреса, метод POST, JSON: <code>text</code> = вміст спільного доступу. Бот збереже це в 📝 Нотатки.",
                  "", "Докладно з картинками кроків — docs/06-servisy.md."]
        rows.append([("📋 Адреса", "copy:" + url)])
        rows.append([("📥 Що в черзі", f"m:svls:{sid}"), ("➕ Додати", f"m:svadd:{sid}")])
        rows.append([("🔁 Новий ключ", f"m:svcon:{sid}"), ("🔌 Відключити", f"m:svdis:{sid}")])
    elif conn:
        lines.append("✅ Підключено." + (f" Листи йдуть на {esc(acc.get('to', ''))}." if s["auth"] == "email" else ""))
        if sid == "notion":
            lines.append(f"Нові сторінки створюю всередині: <b>{esc(acc.get('page_name', '?'))}</b>")
        if sid == "trello":
            lines.append(f"Нові картки: <b>{esc(acc.get('board_name', '?'))} → {esc(acc.get('list_name', '?'))}</b>")
        for k in s["kinds"]:
            lines.append(f"За замовчуванням для {KIND_NAMES[k]}: " + ("✅ так" if SVC.default(k) == sid else "ні"))
        rows.append([("📥 Показати", f"m:svls:{sid}"), ("➕ Додати", f"m:svadd:{sid}")])
        rows.append([(f"⭐️ Головний для: {KIND_NAMES[k]}", f"m:svdef:{k}:{sid}") for k in s["kinds"] if SVC.default(k) != sid] or [("⭐️ Уже головний", "m:noop")])
        if sid in ("notion", "trello"):
            rows.append([("📍 Куди складати", f"m:svtg:{sid}")])
        rows.append([("🔌 Відключити", f"m:svdis:{sid}")])
    else:
        lines.append(s["how"])
        if s["auth"] == "shortcut":
            rows.append([("📱 Підключити", f"m:svcon:{sid}")] if SVC.oauth_ready(sid) else [("⚠️ Спершу задайте PUBLIC_URL", "m:noop")])
        elif s["auth"] == "email":
            if SVC.oauth_ready(sid):
                rows.append([("✉️ Надіслати адресу", f"m:svcon:{sid}")])
            else:
                lines.append("\n⚠️ Бот надсилає листи з вашої пошти, тож потрібні SMTP_HOST, SMTP_USER і SMTP_PASSWORD (для Gmail — пароль застосунку). Як — /help services.")
        elif s["auth"] == "token":
            rows.append([("🔑 Надіслати токен", f"m:svcon:{sid}")])
        elif SVC.oauth_ready(sid):
            rows.append([("🔐 Увійти", "url:" + SVC.login_url(sid))])
        else:
            lines.append("\n⚠️ Спершу створіть застосунок " + ("Google (GDRIVE_CLIENT_ID)" if s["auth"] == "google" else "Microsoft (ONEDRIVE_CLIENT_ID)")
                         + f" і додайте адресу повернення <code>{esc(SVC.redirect(sid)) if SVC.public_url else 'PUBLIC_URL/connect/' + sid + '/callback'}</code>. Як — /help services.")
    return "\n".join(lines), rows


async def scr_svc_items(chat_id, sid):
    try:
        items = await SVC.items(sid)
    except ServiceError as e:
        return f"⚠️ {SERVICES[sid]['title']}: {esc(str(e))}", [[("« Назад", f"m:sv:{sid}")]]
    SVC_LAST.setdefault(chat_id, {})[sid] = [i for i, _, _ in items]
    s = SERVICES[sid]
    lines = [f"<b>{s['icon']} {s['title']}</b> · {len(items)}", ""] + [f"{'☐' if done else '📄'} {esc(short_q(t, 90))}" for _, t, done in items]
    if not items:
        lines.append("<i>Порожньо.</i>")
    rows = [[(f"✅ {short_q(t, 34)}", f"m:svdone:{sid}:{n}")] for n, (_, t, done) in enumerate(items) if done][:12]
    rows.append([("➕ Додати", f"m:svadd:{sid}")])
    return "\n".join(lines), rows


def scr_svc_pick(src, item_id):
    """Куди надіслати нотатку (n) чи завдання бота (t)."""
    kind = "note" if src == "n" else "task"
    cands = [s for s in SERVICES if SVC.connected(s)]
    rows = pairs([(f"{SERVICES[s]['icon']} {SERVICES[s]['title']}", f"m:svsend:{s}:{src}:{item_id}") for s in cands])
    return (f"📤 <b>Куди надіслати {'нотатку' if kind == 'note' else 'завдання'}?</b>" + ("" if cands else "\n\nСпершу підключіть сервіс у «🔗 Сервіси»."), rows)


def svc_auto_note(text):
    """Автокопія нової нотатки в головний блокнот (у фоні, помилки — у журнал)."""
    sid = SVC.default("note")
    if not sid or not SVC.cfg()["auto"].get("note"):
        return
    title = text.strip().split("\n")[0][:80]

    async def go():
        try:
            await SVC.add(sid, "note", title, text)
        except Exception as e:  # noqa: BLE001
            log.warning("автокопія нотатки в %s не вдалася: %s", sid, e)

    try:
        BACKGROUND.add(asyncio.get_running_loop().create_task(go()))
    except RuntimeError:
        pass


SVC_TOOLS = [
    {"type": "function", "function": {"name": "external_add", "description": "Додати завдання або нотатку в підключений сервіс користувача (Todoist, Google Tasks, Microsoft To Do, Trello, Notion, OneNote).",
     "parameters": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["task", "note"], "description": "task — завдання, note — нотатка"},
         "title": {"type": "string", "description": "Коротка назва"}, "details": {"type": "string", "description": "Текст або опис, необов'язково"},
         "due": {"type": "string", "description": "Строк: дата YYYY-MM-DD або фраза англійською для Todoist (tomorrow, every monday), необов'язково"},
         "service": {"type": "string", "description": "todoist, gtasks, mstodo, trello, notion, onenote; порожньо — сервіс за замовчуванням"}}, "required": ["kind", "title"]}}},
    {"type": "function", "function": {"name": "external_list", "description": "Показати відкриті завдання або останні нотатки з підключеного сервісу.",
     "parameters": {"type": "object", "properties": {"service": {"type": "string"}, "kind": {"type": "string", "enum": ["task", "note"]}}}}},
]


async def run_svc_tool(name, args):
    kind = args.get("kind") or "task"
    sid = args.get("service") or SVC.default(kind)
    if not sid or sid not in SERVICES or not SVC.connected(sid):
        return "Сервіс не підключено. Підключіть його в меню «🔗 Сервіси»."
    try:
        if name == "external_add":
            url = await SVC.add(sid, kind, args.get("title", ""), args.get("details", ""), args.get("due"))
            return f"Додано в {SERVICES[sid]['title']}: {url}"
        items = await SVC.items(sid)
        return json.dumps([t for _, t, _ in items], ensure_ascii=False) if items else "Порожньо"
    except ServiceError as e:
        return f"{SERVICES[sid]['title']}: {e}"


# ---------------------------------------------------------------- ⏳ індикатор роботи
# Поки бот працює над запитом, у чаті видно: що саме він робить, яка модель думає, скільки минуло,
# перемикання на іншу модель і чому. Кнопка «✖️ Зупинити» перериває роботу. Коли готово — статус зникає.
PROGRESS = contextvars.ContextVar("progress", default=None)
RUNNING = {}       # id індикатора -> Progress (для кнопки «Зупинити»)
CHAT_LOCKS = {}    # chat_id -> asyncio.Lock: запити до ШІ в одному чаті виконуються по черзі
CHAT_BUSY = {}     # chat_id -> (що виконується, коли почали)
IN_BRIDGE = bool(env("BRIDGE_PLATFORM", ""))  # у WhatsApp/Instagram повідомлення не редагуються — статус показуємо скромніше


class Progress:
    def __init__(self, bot, chat_id, title, stop=True, delay=1.5):
        self.bot, self.chat_id, self.title, self.stop, self.delay = bot, chat_id, title, stop, delay
        self.stage = ""
        self.started = time.time()
        self.msg_id = None
        self.pid = secrets.token_hex(3)
        self.task = None
        self._loop = None
        self._last_text = None
        self.cancelled = False
        self.step_i, self.step_n = 0, 0
        self.typical = typical_time(title)

    def set(self, stage):
        self.stage = stage

    def step(self, i, n, label):
        """Крок i з n — для смужки прогресу."""
        self.step_i, self.step_n, self.stage = i, n, label

    def _bar(self):
        sec = time.time() - self.started
        if self.typical:  # знаємо, скільки зазвичай триває, — смужка за часом
            frac = min(0.95, sec / self.typical)
        elif self.step_n:  # не знаємо — за кроками, всередині кроку плавно наближаємось до його кінця
            frac = (self.step_i - 1 + 0.9 * (1 - math.exp(-sec / 15))) / self.step_n
        else:
            return ""
        frac = max(0.03, min(0.97, frac))
        full = round(frac * 10)
        return "▰" * full + "▱" * (10 - full) + f" {int(frac * 100)}%"

    def _text(self):
        sec = int(time.time() - self.started)
        dur = f"{sec // 60}:{sec % 60:02d}" if sec >= 60 else f"{sec} с"
        bar = self._bar()
        steps = f"Крок {self.step_i} з {self.step_n}: " if self.step_n else ""
        eta = ""
        if self.typical:
            left = int(self.typical - (time.time() - self.started))
            eta = f"\n<i>Зазвичай це ~{int(self.typical)} с" + (f", лишилось ~{left} с" if left > 2 else ", вже ось-ось") + ".</i>"
        return (f"⏳ <b>{esc(self.title)}</b> · {dur}" + (f"\n{bar}" if bar else "")
                + (f"\n{steps}{esc(self.stage)}" if self.stage else "") + eta + "\n<i>Працюю — відповідь прийде сюди.</i>")

    async def _tick(self):
        try:
            await asyncio.sleep(self.delay)
            markup = kb([[("✖️ Зупинити", f"m:stop:{self.pid}")]]) if self.stop and not IN_BRIDGE else None
            if IN_BRIDGE:  # без редагувань: одне повідомлення, лише якщо робота довга
                await asyncio.sleep(6)
                await self.bot.send_message(self.chat_id, f"⏳ {self.title}… це займе трохи часу, відповідь прийде сюди.")
                return
            m = await self.bot.send_message(self.chat_id, self._text(), parse_mode=ParseMode.HTML, reply_markup=markup)
            self.msg_id = m.message_id
            self._last_text = self._text()
            n = 0
            while True:
                await asyncio.sleep(3)
                n += 1
                try:
                    await self.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
                    t = self._text()
                    if n % 2 == 0 and t != self._last_text:  # оновлюємо раз на ~6 с, щоб не впертися в ліміти Telegram
                        await self.bot.edit_message_text(t, chat_id=self.chat_id, message_id=self.msg_id, parse_mode=ParseMode.HTML, reply_markup=markup)
                        self._last_text = t
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            pass

    async def __aenter__(self):
        self.task = asyncio.current_task()
        RUNNING[self.pid] = self
        self._token = PROGRESS.set(self)
        try:
            await self.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            pass
        self._loop = asyncio.create_task(self._tick())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            remember_time(self.title, time.time() - self.started)
        RUNNING.pop(self.pid, None)
        PROGRESS.reset(self._token)
        if self._loop:
            self._loop.cancel()
        if self.msg_id:
            try:
                await self.bot.delete_message(self.chat_id, self.msg_id)
            except Exception:  # noqa: BLE001
                pass
        if exc_type is asyncio.CancelledError and self.cancelled:
            try:
                if LAST_JOURNAL.get():
                    journal_update(LAST_JOURNAL.get(), status="cancelled")
            except Exception:  # noqa: BLE001
                pass
            sec = int(time.time() - self.started)
            await self.bot.send_message(self.chat_id, f"🛑 Зупинено через {sec} с: {self.title}.\nЗапит лишився в 📒 Журналі — його можна виконати знову.")
            return True  # зупинку обробили — далі не пробрасываємо
        return False


def progress_stage(text, step=None, of=None):
    p = PROGRESS.get()
    if p:
        if step and of:
            p.step(step, of, text)
        else:
            p.set(text)


def typical_time(title):
    """Скільки зазвичай триває така робота (ковзне середнє останніх разів) — для смужки й «лишилось ~N с»."""
    try:
        v = json.loads(kv_get("ptimes") or "{}").get(title)
        return float(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def remember_time(title, sec):
    try:
        d = json.loads(kv_get("ptimes") or "{}")
        old = d.get(title)
        d[title] = round(sec if not old else old * 0.7 + sec * 0.3, 1)
        kv_set("ptimes", json.dumps(d))
    except Exception:  # noqa: BLE001
        pass


def dialog_name(did):
    d = dialog_get(did) if did else None
    return (d or {}).get("title") or f"Розмова #{did}"


def busy_elsewhere(chat_id, did):
    """Що зараз виконується в інших розмовах цього чату."""
    return [(k[1], v) for k, v in CHAT_BUSY.items() if k[0] == chat_id and k[1] != did]


async def with_chat_queue(update, title, coro_fn, did=None):
    """У межах ОДНІЄЇ розмови запити — по черзі (щоб модель бачила попередню відповідь).
    Різні розмови працюють паралельно: поки одна ШІ думає, в іншій розмові можна ставити нові завдання іншим ШІ."""
    chat_id = update.effective_chat.id
    key = (chat_id, did)
    lock = CHAT_LOCKS.setdefault(key, asyncio.Lock())
    if lock.locked():
        what, since = CHAT_BUSY.get(key, ("попередній запит", time.time()))
        sec = int(time.time() - since)
        await update.effective_chat.send_message(
            f"🕒 У цій розмові ще працюю над попереднім: «{short_q(what, 60)}» ({sec} с).\n"
            "Цей запит у черзі — візьмуся одразу після.\n"
            "💡 Не хочете чекати? Відкрийте іншу розмову (💬 Розмови → ➕ Нова) — там можна працювати паралельно.")
    async with lock:
        CHAT_BUSY[key] = (title, time.time())
        try:
            return await coro_fn()
        finally:
            CHAT_BUSY.pop(key, None)


# ---------------------------------------------------------------- 📒 журнал запитів і 📚 пам'ять проєкту
# Кожен запит до ШІ записується в журнал: що питали, хто відповідав, чим закінчилося (виконано, не виконано й чому,
# обірвано через ліміт довжини, відкладено). З журналу будь-який запит можна продовжити на іншій моделі з того самого місця.
# Пам'ять проєкту — стислий підсумок старої частини розмови: кожна модель отримує його разом з останніми повідомленнями,
# тож продовжує весь проєкт, а не лише його уривок.
JOURNAL = contextvars.ContextVar("journal", default=None)
LAST_JOURNAL = contextvars.ContextVar("last_journal", default=None)   # {"kind", "task_id"} — що саме зараз виконується
NO_JOURNAL = contextvars.ContextVar("no_journal", default=False)
J_STATUS = {"refused": "🚫 модель відмовилась", "needs_info": "❓ потрібне уточнення", "running": "⏳ виконується", "done": "✅ виконано", "partial": "✂️ обірвано (ліміт довжини)", "failed": "❌ не виконано",
            "deferred": "🕒 відкладено", "cancelled": "🛑 зупинено", "continued": "↪️ продовжено"}
SUMMARY_EVERY = int(env("SUMMARY_EVERY", "12"))  # скільки нових «старих» повідомлень накопичити до оновлення підсумку


def journal_start(prompt, dialog_id=None):
    if NO_JOURNAL.get():
        return None
    chat = CHAT.get()
    meta = JOURNAL.get() or {}
    text = prompt if isinstance(prompt, str) else " ".join(p.get("text", "") for p in prompt if isinstance(p, dict) and p.get("type") == "text") + " [фото]"
    with closing(db()) as con, con:
        return con.execute(
            "INSERT INTO journal (chat_id, dialog_id, prompt, status, kind, task_id, created, updated) VALUES (?,?,?,?,?,?,?,?)",
            (chat, dialog_id or (current_dialog(chat) if chat else None), (meta.get("label") or text)[:8000], "running", meta.get("kind", "chat"), meta.get("task_id"), time.time(), time.time()),
        ).lastrowid


def journal_update(jid, **f):
    if not jid:
        return
    f["updated"] = time.time()
    with closing(db()) as con, con:
        con.execute(f"UPDATE journal SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), jid))


def journal_get(jid):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM journal WHERE id=?", (jid,)).fetchone()
    return dict(r) if r else None


J_FILTERS = [("all", "Усі"), ("done", "✅"), ("open", "❌ Невиконані"), ("partial", "✂️")]


def journal_list(chat_id, filt="all", limit=200):
    where = {"all": "", "done": " AND status='done'", "open": " AND status IN ('failed','deferred','running','refused','cancelled')", "partial": " AND status='partial'"}[filt]
    with closing(db()) as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM journal WHERE chat_id=?{where} ORDER BY id DESC LIMIT ?", (chat_id, limit))]


def scr_journal(chat_id, page=0, filt=None):
    filt = filt or prefs(chat_id).get("journal_filter", "all")
    set_pref(chat_id, "journal_filter", filt)
    items = journal_list(chat_id, filt)
    with closing(db()) as con:
        stat = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM journal WHERE chat_id=? GROUP BY status", (chat_id,))}
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PER_PAGE : (page + 1) * PER_PAGE]
    lines = ["<b>📒 Журнал запитів</b>",
             f"✅ {stat.get('done', 0)} · ❌ {stat.get('failed', 0)} · ✂️ {stat.get('partial', 0)} · 🕒 {stat.get('deferred', 0)} · ↪️ {stat.get('continued', 0)}", ""]
    for j in chunk:
        who = PROVIDERS.get(j["provider"] or "", {}).get("title") or ""
        lines.append(f"{J_STATUS.get(j['status'], j['status'])[:2]} {datetime.fromtimestamp(j['created'], TZ).strftime('%d.%m %H:%M')}"
                     + (f" · {esc(who)}" if who else "") + f"\n{esc(short_q(j['prompt'], 90))}")
    if not chunk:
        lines.append("<i>Порожньо.</i>")
    rows = [[(label, f"m:jl:0:{v}") for label, v in seg(J_FILTERS, filt)]]
    rows += [[(f"{J_STATUS.get(j['status'], '?')[:2]} {short_q(j['prompt'], 36)}", f"m:j:{j['id']}")] for j in chunk]
    if pages > 1:
        rows.append(page_row("m:jl", page, pages, f":{filt}"))
    return "\n".join(lines), rows


def scr_journal_item(chat_id, jid):
    j = journal_get(jid)
    if not j or j["chat_id"] != chat_id:
        return scr_journal(chat_id)
    d = dialog_get(j["dialog_id"]) if j["dialog_id"] else None
    lines = [f"<b>📒 Запит #{jid}</b> · {J_STATUS.get(j['status'], j['status'])}",
             f"{datetime.fromtimestamp(j['created'], TZ).strftime('%d.%m.%Y %H:%M')} · {'завдання #' + str(j['task_id']) if j['task_id'] else {'chat': 'розмова', 'hub': 'від головного бота'}.get(j['kind'], j['kind'])}",
             f"💬 Розмова: {esc(d['title']) if d else '—'}", ""]
    if j["provider"]:
        lines.append(f"🤖 Відповідала: {esc(PROVIDERS.get(j['provider'], {}).get('title', j['provider']))}")
    if j["attempts"]:
        lines.append("Спроби:\n" + esc(j["attempts"][:600]))
    lines += ["", "<b>Запит:</b>", esc(j["prompt"][:1500])]
    if j["result"]:
        lines += ["", "<b>Відповідь" + (" (обірвана)" if j["status"] == "partial" else "") + ":</b>", esc(short_q(j["result"], 700))]
    rows = []
    if j["status"] in ("failed", "partial", "deferred", "running"):
        verb = "▶️ Продовжити з місця обриву" if j["status"] == "partial" else "▶️ Виконати"
        rows.append([(verb + " · 🔁 Авто", f"m:jrun:{jid}:auto")])
        rows += pairs([(f"{provider_icon(p)} {PROVIDERS[p]['title']}", f"m:jrun:{jid}:{p}") for p in PROVIDERS if not paused_until(p)][:6])
    if d:
        rows.append([("💬 До розмови", f"m:d:{d['id']}")])
    return "\n".join(lines), rows


def summary_get(dialog_id):
    with closing(db()) as con:
        r = con.execute("SELECT summary, summary_upto FROM dialogs WHERE id=?", (dialog_id,)).fetchone()
    return (r["summary"], r["summary_upto"] or 0) if r else (None, 0)


def project_context(dialog_id, first_loaded_id):
    """Системна примітка для моделі: підсумок старої частини розмови + незавершені запити."""
    parts = []
    summary, _ = summary_get(dialog_id)
    with closing(db()) as con:
        older = con.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=? AND id<? AND in_context=1", (dialog_id, first_loaded_id or 0)).fetchone()[0]
        opened = con.execute("SELECT id, prompt, status FROM journal WHERE dialog_id=? AND status IN ('failed','partial','deferred') ORDER BY id DESC LIMIT 5",
                             (dialog_id,)).fetchall()
    if summary and older:
        parts.append("Стислий підсумок попередньої частини цієї розмови (проєкту), яку ти не бачиш повністю:\n" + summary)
    if opened:
        parts.append("Незавершені запити в цій розмові (врахуй, якщо користувач до них повертається):\n"
                     + "\n".join(f"- #{r['id']} ({J_STATUS.get(r['status'], r['status'])}): {short_q(r['prompt'], 200)}" for r in opened))
    return "\n\n".join(parts)


async def maybe_update_summary(dialog_id, chat_id):
    """Коли стара частина розмови виходить за «пам'ять», оновлюємо стислий підсумок (у фоні, однією з вільних моделей)."""
    summary, upto = summary_get(dialog_id)
    memory = prefs(chat_id)["memory"]
    with closing(db()) as con:
        ids = [r[0] for r in con.execute("SELECT id FROM messages WHERE dialog_id=? AND in_context=1 AND pending=0 ORDER BY id DESC", (dialog_id,))]
        if len(ids) <= memory:
            return
        boundary = ids[memory - 1]  # усе, що старше за це, модель уже не бачить напряму
        fresh = con.execute("SELECT role, text FROM messages WHERE dialog_id=? AND in_context=1 AND id>? AND id<? ORDER BY id",
                            (dialog_id, upto, boundary)).fetchall()
    if len(fresh) < SUMMARY_EVERY and summary:
        return
    if not fresh:
        return
    convo = "\n".join(f"{'Користувач' if r['role'] == 'user' else 'ШІ'}: {r['text'][:1500]}" for r in fresh)[:20000]
    prompt = ("Онови стислий підсумок проєкту. Збережи все важливе для продовження роботи: мету, домовленості й рішення, що вже зроблено, "
              "що залишилось, конкретні деталі (імена, числа, назви файлів, формулювання). Пиши українською, до 1500 символів, пунктами.\n\n"
              f"Попередній підсумок:\n{summary or '(ще немає)'}\n\nНова частина розмови:\n{convo}")
    token = NO_JOURNAL.set(True)
    set_prio(-1000, "оновлення підсумку розмови")  # фонова робота — найменш важлива, ніколи не заважає вам
    try:
        text, _, _, _ = await ask_llm([], prompt)
    except Exception as e:  # noqa: BLE001
        log.info("підсумок розмови не оновлено: %s", e)
        return
    finally:
        NO_JOURNAL.reset(token)
    with closing(db()) as con, con:
        con.execute("UPDATE dialogs SET summary=?, summary_upto=? WHERE id=?", (text[:4000], boundary - 1, dialog_id))


def schedule_summary(dialog_id, chat_id):
    try:
        BACKGROUND.add(asyncio.get_running_loop().create_task(maybe_update_summary(dialog_id, chat_id)))
    except RuntimeError:
        pass


async def journal_run(update, ctx, jid, pid):
    """Повторити невиконаний запит або продовжити обірвану відповідь — на будь-якій моделі, з повною історією й пам'яттю проєкту."""
    chat_id = update.effective_chat.id
    j = journal_get(jid)
    if not j:
        return
    preferred = None if pid == "auto" else pid
    did = j["dialog_id"] or current_dialog(chat_id)
    set_prio(RK.score("t", j["task_id"]) if j.get("task_id") and RK.score("t", j["task_id"]) else dialog_score(did), f"Запит #{jid} («{short_q(dialog_name(did), 24)}»)", chat=chat_id)
    use_prefs(chat_id)
    if j["status"] == "partial":
        content = ("Твоя попередня відповідь обірвалася через ліміт довжини. Продовж її рівно з того місця, де вона обірвалася, "
                   "без повторення вже написаного. Ось її кінець:\n…" + (j["result"] or "")[-600:])
    else:
        content = j["prompt"]
    label = f"↪️ Продовження запиту #{jid}: {short_q(j['prompt'], 120)}" if j["status"] == "partial" else j["prompt"]
    token = JOURNAL.set({"kind": "continue", "task_id": j["task_id"], "label": label})
    hist = load_history(chat_id, dialog_id=did)
    redo = j["status"] in ("refused", "needs_info")
    if redo:  # запит уже є в розмові — не дублюємо його, а відмову попередньої моделі не показуємо новій
        while hist and hist[-1]["role"] == "assistant" and j["status"] == "refused":
            hist.pop()
        if hist and hist[-1]["role"] == "user":
            hist.pop()
    exclude = (j["provider"],) if j["status"] == "refused" and j.get("provider") and preferred is None else ()
    try:
        text, used, attempts, warnings = await ask_llm(hist, content, preferred, fallback=preferred is None, exclude=exclude)
    except Unavailable as u:
        return await update.effective_chat.send_message("⚠️ Зараз не вийшло:\n" + reasons_text(u.attempts))
    finally:
        JOURNAL.reset(token)
    journal_update(jid, status="continued")
    t = task_get(j["task_id"]) if j["task_id"] else None
    if t and t["status"] in OPEN:  # відкладене завдання виконано з журналу — закриваємо його
        task_update(t["id"], status="done", last_try=time.time())
        if t.get("msg_id"):
            set_msg(t["msg_id"], pending=0, in_context=0)
    if j["status"] != "partial" and not redo:
        save_message(chat_id, "user", j["prompt"], dialog_id=did)
    save_message(chat_id, "assistant", text, provider=used, dialog_id=did)
    head = "↪️ Продовження" if j["status"] == "partial" else f"▶️ Запит #{jid}"
    if current_dialog(chat_id) != did:
        head += f" · 💬 «{dialog_name(did)}»"
    await send_long(ctx.bot, chat_id, f"{head} · {PROVIDERS[used]['title']}\n\n" + text + (("\n\n" + footer(used, attempts, warnings)) if attempts or warnings else ""))
    schedule_summary(did, chat_id)


# ---------------------------------------------------------------- 🩺 діагностика
DIAG_TABS = [("sum", "🩺 Огляд"), ("state", "✅ Стан"), ("links", "🔗 Зв'язки"), ("nums", "📊 Цифри"), ("tips", "💡 Поради")]


def links_report(chat_id):
    """🔗 Зв'язки: пов'язані боти (за їхніми планами сну — без зайвих запитів), чати й ресурси."""
    lines, rows = ["<b>🔗 Пов'язані боти</b>"], []
    mine = my_plan()
    lines.append(f"Я ({esc(mine['name'])}): {esc(plan_text(mine))}" + (" · адреса пробудження є" if mine.get("wake_url") else ""))
    if hub.ready:
        hp = hub.peer_plan
        st, until = plan_state(hp)
        when = (f" до {datetime.fromtimestamp(until, TZ).strftime('%d.%m %H:%M')}" if until else "")
        lines.append(f"🧭 Головний бот: {'🟢 не спить' if st == 'awake' else '😴 спить' + (when or ', доки не покличуть')}"
                     + (f" · {esc(plan_text(hp))}" if hp else " · план ще не отримано"))
        lines.append(f"   адреса: <code>{esc(hub.url)}</code>")
        pt = NODE.get("plan_sent_t")
        lines.append("   мій план надіслано: " + (datetime.fromtimestamp(pt, TZ).strftime("%d.%m %H:%M") if pt else "ще ні")
                     + (f" · відкладених повідомлень: {len(hub.held)}" if hub.held else ""))
        lines.append("   Плани передаються лише при змінах; поки головний спить, звичайні повідомлення чекають (тривоги — будять).")
        rows.append([("⏰ Розбудити головного", "m:lkwake"), ("📤 Надіслати план", "m:lkplan")])
    else:
        lines.append("🧭 Головний бот не підключений (HUB_URL і HUB_KEY або «✏️ Головний бот» нижче).")
    rows.append([("✏️ Головний бот", "m:lkhub"), ("✏️ Моя адреса", "m:lkpub")])
    ps = partners()
    if ps:
        lines.append("\n<b>🤝 Хто ще до мене звертався</b>")
        for name, p in list(ps.items())[:6]:
            lines.append(f"• {esc(name)} — востаннє {datetime.fromtimestamp(p.get('last', 0), TZ).strftime('%d.%m %H:%M')}, запитів {p.get('n', 0)}")
    if NODE.enabled:
        lines.append("\n<b>🛰 Сервери</b>")
        for n, _ in NODE.nodes:
            if n == NODE.name:
                lines.append(f"• {esc(n)} — цей, {'працює' if NODE.active else 'у резерві'}")
            else:
                p = NODE.peers.get(n) or {}
                lines.append(f"• {esc(n)} — " + ("✅ живий" + (" · працює" if p.get("status", {}).get("active") else "") if p.get("ok") else ("❌ недоступний" if p.get("t") else "ще не перевірявся")))
        rows.append([("🛰 Сервери", "m:cl")])
    lines.append("\n<b>🏘 Чати, групи, канали</b>")
    cc = CM.counts() if is_admin_id(chat_id) else None
    if cc and cc["chats"]:
        lines.append(f"Усього {cc['chats']}; бот не адмін у: {esc(', '.join(cc['noadmin'])) if cc['noadmin'] else 'жодному — усе гаразд'}")
        if cc["reqs"]:
            lines.append(f"🙋 Запитів чекає: {cc['reqs']}")
    else:
        lines.append("Бот не доданий у групи чи канали.")
    lines.append("\n<b>🧩 Ресурси</b>")
    conn = [SERVICES[sid]["title"] for sid in SERVICES if SVC.connected(sid)]
    lines.append("Інтеграції: " + (esc(", ".join(conn)) if conn else "не підключено"))
    tg = [TARGET_TITLES[t][1] for t in ("file", "channel", *CLOUDS) if BK.enabled(t) and (t in ("file",) or BK.connected(t) or t == "channel")]
    lines.append("Копії: " + esc(", ".join(tg) or "—") + (" · автокопії ✅" if BK.cfg().get("auto_enabled") else " · автокопії вимкнені"))
    lines.append(f"Моделей підключено: {len(PROVIDERS)}" + (f" · на паузі: {sum(1 for p in PROVIDERS if paused_until(p))}" if any(paused_until(p) for p in PROVIDERS) else ""))
    if storage_warning():
        lines.append("⚠️ Немає Railway Volume — дані зникатимуть при оновленнях")
    return lines, rows


def storage_warning():
    """Railway (і схожі хостинги) стирають файли контейнера при кожному оновленні. Без підключеного диска (Volume)
    на теці бази бот «забуде» розмови, налаштування й доступи. Повертає текст попередження або None."""
    folder = os.path.dirname(os.path.abspath(DB_PATH)) or "/"
    on_railway = any(os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"))
    if not on_railway or os.path.ismount(folder) or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        return None
    return (f"⚠️ Дані бота лежать у {folder}, але там не підключено Railway Volume — після кожного оновлення чи перезапуску "
            "розмови, налаштування й доступи зникнуть. Railway → сервіс → Settings/⋯ → Attach Volume → Mount path "
            f"<code>{folder}</code>. Докладно: docs/09-railway.md")


def diag_collect(chat_id):
    """Збирає все для діагностики: що працює, що ні, цифри, поради, суперечності, що гармонійно."""
    ok, bad, tips, conflicts, good = [], [], [], [], []
    p = prefs(chat_id)
    admin = is_admin_id(chat_id)
    sw = storage_warning()
    if sw:
        bad.append(re.sub(r"<[^>]+>", "", sw))
    if hub.ready:
        st, until = plan_state(hub.peer_plan)
        ok.append("🧭 Головний бот підключений · зараз " + ("не спить" if st == "awake" else "спить" + (f" до {datetime.fromtimestamp(until, TZ).strftime('%H:%M')}" if until else "")))
        if not NODE.get("plan_sent"):
            tips.append("Мій план сну ще не передано головному боту — 🩺 → 🔗 Зв'язки → 📤 Надіслати план")
    # --- моделі
    live = [pid for pid in PROVIDERS if not paused_until(pid)]
    paused = [pid for pid in PROVIDERS if paused_until(pid)]
    errs = [pid for pid in PROVIDERS if get_state(pid).get("fails")]
    missing = [CATALOG[k]["title"] for k in CATALOG if k not in PROVIDERS]
    customs = custom_rows()
    if live:
        ok.append(f"🤖 Моделі працюють: {', '.join(PROVIDERS[x]['title'] for x in live)}")
    for x in paused:
        bad.append(f"⏸ {PROVIDERS[x]['title']} на паузі до {hhmm(paused_until(x))} ({get_state(x).get('cooldown_kind') or 'помилка'})")
    for x in errs:
        if x not in paused:
            bad.append(f"⚠️ {PROVIDERS[x]['title']}: останні запити з помилками — подивіться «📊 Ліміти»")
    if not PROVIDERS:
        bad.append("❌ Жодна модель не підключена — бот не зможе відповідати")
    if missing:
        tips.append(f"Безкоштовно можна додати ще: {', '.join(missing[:5])}" + (" та інші" if len(missing) > 5 else "") + " (docs/02-modeli.md)")
    if len(PROVIDERS) == 1:
        tips.append(f"Лише одна модель: коли вона впреться в ліміт, «🔁 Авто» не матиме запасу. Додайте хоча б {' або '.join(missing[:2]) or 'ще одну'}")
    elif len(PROVIDERS) >= 3:
        good.append(f"{len(PROVIDERS)} моделі в «🔁 Авто» — є запас, коли якась упирається в ліміт")
    choice = get_choice(chat_id)
    if choice != "auto" and choice in PROVIDERS and paused_until(choice):
        conflicts.append(f"Обрано {PROVIDERS[choice]['title']}, але вона на паузі — відповіді йдуть через запасні моделі. Можливо, краще «🔁 Авто»")
    # --- генератори, голос, Canva
    imgs = [g["title"] for g in media.IMAGE_PROVIDERS.values() if g["ready"]()]
    music = [g["title"] for g in media.MUSIC_PROVIDERS.values() if g["ready"]()]
    stt = [g["title"] for g in media.STT_PROVIDERS.values() if g["ready"]()]
    (ok if imgs else bad).append("🖼 Картинки: " + (", ".join(imgs) if imgs else "немає генератора"))
    (ok if music else bad).append("🎵 Музика: " + (", ".join(music) if music else "не підключена (OpenRouter або свій сервер)"))
    (ok if stt else bad).append("🎙 Розшифровка голосу: " + (", ".join(stt) if stt else "немає — потрібен GROQ_API_KEY або Cloudflare"))
    tts = media.tts_ready()
    (ok if tts else bad).append("🔊 Відповіді голосом: " + ("edge-tts" if tts else "вимкнено (VOICE_TTS)"))
    if p.get("voice_reply") and not tts:
        conflicts.append("Увімкнено «Відповідати голосом», але озвучення недоступне — голосових відповідей не буде")
    if p.get("voice_mode") == "ask" and not stt:
        conflicts.append("Голосові мають бути питаннями до ШІ, але розшифровки немає — бот їх не зрозуміє")
    if admin:
        ok.append("🖌 Canva підключена") if canva.connected() else tips.append("Canva не підключена — /help canva (у вас є платна підписка)")
    # --- адреса, копії, сервіси, головний бот
    public = env("PUBLIC_URL", "")
    if not public:
        bad.append("🌐 PUBLIC_URL не задано — не працюватимуть вхід у хмари, Canva, Google/Microsoft-сервіси й команди від головного бота")
    if admin:
        c = BK.cfg()
        where = [TARGET_TITLES[t][1] for t in ("file", "channel", *CLOUDS) if BK.enabled(t)]
        last = c.get("last")
        (ok if last and time.time() - last < 8 * 86400 else bad).append(
            "💾 Остання копія: " + (f"{int((time.time() - last) // 86400)} дн тому" if last else "ще не робилася") + f" · куди: {', '.join(where) or 'нікуди'}")
        if where == ["Файлом у чат"]:
            tips.append("Копії йдуть лише файлом у чат. Підключіть Google Диск, Dropbox, OneDrive або канал — так копія переживе навіть втрату акаунта")
        if not env("BACKUP_PASSWORD") and any(BK.enabled(t) for t in ("channel", *CLOUDS)):
            conflicts.append("Копії йдуть у хмару, але BACKUP_PASSWORD не задано — у хмарі лежать незашифровані розмови й підключення")
        elif env("BACKUP_PASSWORD"):
            good.append("Копії шифруються паролем")
        if c.get("days", 7) == 0:
            tips.append("Автоматичні копії вимкнено (так і задумано, поки ви не попросите). Якщо хочете — задайте розклад у 💾 Копії, наприклад раз на тиждень у Google Диск чи приватний канал")
        svc = [SERVICES[s]["title"] for s in SERVICES if SVC.connected(s)]
        if svc:
            ok.append("🔗 Сервіси: " + ", ".join(svc))
        sc = SVC.cfg()
        if sc["auto"].get("note") and not SVC.default("note"):
            conflicts.append("Увімкнено автокопію нотаток, але жодного блокнота не підключено")
        if hub.ready:
            ok.append("🧭 Зв'язок із головним ботом налаштовано")
            if not public:
                conflicts.append("HUB_URL задано, але без PUBLIC_URL головний бот не зможе надсилати сюди команди")
        else:
            tips.append("Підключіть ШІ-бота до 🧭 головного бота (HUB_URL, HUB_KEY): він стежитиме, чи бот працює")
        pend = len(members_list("pending"))
        if pend:
            tips.append(f"{pend} запит(ів) на доступ чекають вашого рішення — «👥 Доступ»")
    # --- налаштування
    if not p.get("alerts") and not p.get("footer"):
        conflicts.append("Вимкнено і сповіщення, і підпис моделі — ви не дізнаєтесь, коли модель недоступна чи відповіла інша")
    if p.get("memory", 30) >= 48 and "groq" in PROVIDERS and AUTO_ORDER and AUTO_ORDER[0] == "groq":
        tips.append("Велика пам'ять розмови (≥48) з Groq першим у «Авто» швидко вичерпує ліміт токенів за хвилину — зменште пам'ять або поставте першим Gemini")
    # --- спільноти
    if is_admin_id(chat_id):
        cc = CM.counts()
        if cc["chats"]:
            ok.append(f"🏘 Спільнот: {cc['chats']} · правил увімкнено: {cc['rules_on']} · дописів у розкладі: {cc['posts']}")
            good.append(f"🏘 Пророблено в спільнотах: побачено {cc['stats']['seen']}, прибрано {cc['stats']['deleted'] + cc['stats']['hidden']}, "
                        f"попереджень {cc['stats']['warned']}, дописів {cc['stats']['posted']}, дозволів {cc['stats']['permits']}")
        for t in cc["noadmin"]:
            bad.append(f"🏘 У «{t}» бот не адміністратор — не бачить усіх повідомлень і не може видаляти порушення")
        if cc["reqs"]:
            tips.append(f"🙋 {cc['reqs']} запит(ів) дозволу/вступу чекають рішення — 🏘 Спільноти")
    # --- меню
    counts, mconf, mgood = L.report(chat_id)
    conflicts += mconf
    good += mgood
    # --- цифри
    with closing(db()) as con:
        q = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
        nums = {
            "dialogs": q("SELECT COUNT(*) FROM dialogs WHERE chat_id=? AND state='active'", chat_id),
            "folders": q("SELECT COUNT(*) FROM folders WHERE chat_id=? AND state='active'", chat_id),
            "msgs": q("SELECT COUNT(*) FROM messages WHERE chat_id=?", chat_id),
            "answers": q("SELECT COUNT(*) FROM messages WHERE chat_id=? AND role='assistant'", chat_id),
            "tasks_open": q("SELECT COUNT(*) FROM tasks WHERE chat_id=? AND state='active' AND status IN ('choice','deferred','scheduled')", chat_id),
            "tasks_done": q("SELECT COUNT(*) FROM tasks WHERE chat_id=? AND status='done'", chat_id),
            "notes": q("SELECT COUNT(*) FROM notes WHERE chat_id=? AND state='active'", chat_id),
            "media": q("SELECT COUNT(*) FROM media WHERE chat_id=? AND state='active'", chat_id),
            "pins": sum(q(f"SELECT COUNT(*) FROM {t} WHERE chat_id=? AND pinned_at IS NOT NULL AND state='active'", chat_id) for t, _, _ in KINDS.values()),
            "archived": sum(q(f"SELECT COUNT(*) FROM {t} WHERE chat_id=? AND state='archived'", chat_id) for t, _, _ in KINDS.values()),
            "trash": sum(q(f"SELECT COUNT(*) FROM {t} WHERE chat_id=? AND state='trash'", chat_id) for t, _, _ in KINDS.values()),
            "first": con.execute("SELECT MIN(ts) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()[0],
            "req": con.execute("SELECT provider, SUM(requests), SUM(errors) FROM usage GROUP BY provider ORDER BY SUM(requests) DESC").fetchall(),
        }
    if nums["trash"] > 50:
        tips.append(f"У кошику {nums['trash']} елементів — можна очистити зараз, а не чекати 30 днів")
    if nums["tasks_open"] > 20:
        tips.append(f"Відкритих завдань {nums['tasks_open']} — частину варто виконати або скасувати")
    total = len(ok) + len(bad)
    score = round(100 * (len(ok) + len(good)) / max(1, total + len(good) + len(conflicts)))
    return {"ok": ok, "bad": bad, "tips": tips, "conflicts": conflicts, "good": good, "nums": nums, "counts": counts, "score": score,
            "customs": len(customs), "models": len(PROVIDERS)}


def scr_diag(chat_id, tab="sum"):
    d = diag_collect(chat_id)
    for k in ("ok", "bad", "tips", "conflicts", "good"):
        d[k] = [esc(x) for x in d[k]]
    n = d["nums"]
    head = "<b>🩺 Діагностика</b>"
    if tab == "state":
        body = ["<b>✅ Працює</b>", *(d["ok"] or ["—"]), "", "<b>❌ Не працює або обмежено</b>", *(d["bad"] or ["нічого — усе гаразд"])]
    elif tab == "nums":
        days = int((time.time() - n["first"]) // 86400) + 1 if n["first"] else 0
        body = ["<b>📊 Скільки всього</b>",
                f"🤖 Моделей: {d['models']} (з них своїх 🧩 {d['customs']})",
                f"💬 Розмов: {n['dialogs']} · 📁 тек: {n['folders']} · повідомлень: {n['msgs']}",
                f"⏰ Завдань: відкритих {n['tasks_open']}, виконаних {n['tasks_done']}",
                f"📝 Нотаток: {n['notes']} · 🖼 медіа: {n['media']} · 📌 закріплено: {n['pins']}",
                f"🗄 В архіві: {n['archived']} · 🗑 у кошику: {n['trash']}",
                f"🎛 Кнопок: унизу {d['counts']['kb']}, у «Меню» {d['counts']['home']}, у групах {d['counts']['group']}, приховано {d['counts']['off']}",
                "", "<b>📈 Пророблено роботи</b>",
                f"Відповідей ШІ: {n['answers']}" + (f" за {days} дн" if days else ""),
                *[f"• {esc(PROVIDERS.get(pid, {}).get('title') or CATALOG.get(pid, {}).get('title') or pid)}: {r} запитів" + (f", помилок {e}" if e else "") for pid, r, e in n["req"][:12]]]
    elif tab == "links":
        body, extra = links_report(chat_id)
        rows = [[(("● " if t == tab else "") + label, f"m:diag:{t}") for t, label in DIAG_TABS[:3]], [(("● " if t == tab else "") + label, f"m:diag:{t}") for t, label in DIAG_TABS[3:]]]
        return head + "\n\n" + "\n".join(body), rows + extra
    elif tab == "tips":
        body = ["<b>💡 Що краще увімкнути чи змінити</b>", *(f"• {t}" for t in d["tips"])] if d["tips"] else ["<b>💡 Поради</b>", "Порад немає — налаштовано добре."]
        body += ["", "<b>⚠️ Що суперечить одне одному</b>", *(f"• {c}" for c in d["conflicts"] or ["нічого"])]
        body += ["", "<b>🤝 Що працює гармонійно</b>", *(f"• {g}" for g in d["good"] or ["—"])]
    else:
        bar = "▰" * round(d["score"] / 12.5) + "▱" * (8 - round(d["score"] / 12.5))
        body = [f"Загальний стан: <b>{d['score']}%</b> {bar}", "",
                f"✅ Працює: <b>{len(d['ok'])}</b> · ❌ проблем: <b>{len(d['bad'])}</b>",
                f"⚠️ Суперечностей: <b>{len(d['conflicts'])}</b> · 💡 порад: <b>{len(d['tips'])}</b> · 🤝 гармонійно: <b>{len(d['good'])}</b>",
                f"💬 {n['dialogs']} розмов · ⏰ {n['tasks_open']} відкритих завдань · 📝 {n['notes']} нотаток · 🖼 {n['media']} медіа",
                f"📈 Відповідей ШІ: {n['answers']}"]
        if d["bad"]:
            body += ["", "<b>Головне зараз:</b>", *d["bad"][:3]]
        elif d["conflicts"]:
            body += ["", "<b>Головне зараз:</b>", *d["conflicts"][:2]]
    rows = [[(("● " if t == tab else "") + label, f"m:diag:{t}") for t, label in DIAG_TABS[:3]], [(("● " if t == tab else "") + label, f"m:diag:{t}") for t, label in DIAG_TABS[3:]]]
    rows.append([("🤖 Моделі", "m:md"), ("🎛 Поведінка", "m:st"), ("🧭 Меню й кнопки", "m:btn")])
    if is_admin_id(chat_id):
        rows.append([("💾 Копії", "m:bk"), ("🧩 Інтеграції", "m:svc")])
    return head + "\n\n" + esc_keep(body), rows


def esc_keep(lines):
    """Рядки вже містять безпечний HTML (лише наші теги); екрануємо тільки «<» з даних, що не є тегом."""
    return "\n".join(lines)


# ---------------------------------------------------------------- 🏭 заводські налаштування
# Скидання НЕ чіпає підключень: ключі моделей (.env), 🧩 свої моделі, хмари для копій, Canva, зв'язок із головним ботом,
# учасників і їхній доступ. Що саме стерти з ваших даних — обираєте галочками.
RESET_PARTS = [  # (ключ, коротка кнопка, типово позначено); подробиці — у тексті екрана
    ("settings", "🎛 Поведінка", True),
    ("buttons", "🧭 Меню й кнопки", True),
    ("dialogs", "💬 Розмови й теки", False),
    ("tasks", "⏰ Завдання", False),
    ("notes", "📝 Нотатки", False),
    ("media", "🖼 Медіатека", False),
    ("pins", "📌 Закріплення", False),
    ("archive", "🗄 Архів", False),
    ("trash", "🗑 Кошик", False),
    ("journal", "📒 Журнал і пам'ять", False),
    ("community", "🏘 Спільноти", False),
]
BUTTON_PREFS = ("layout", "groups", "labels", "relabel", "kb_cols", "home_cols", "nav_hist", "compact_menu_v2", "accordion_menu_v4")


def reset_sel(chat_id):
    raw = kv_get(f"reset_sel:{chat_id}")
    return set(json.loads(raw)) if raw else {k for k, _, on in RESET_PARTS if on}


def scr_reset(chat_id):
    sel = reset_sel(chat_id)
    text = ("<b>🏭 Скидання до заводських налаштувань</b>\n\n"
            "Позначте, що скинути або стерти. <b>Не зачіпається ніколи:</b> ключі моделей і 🧩 свої моделі, підключені хмари для копій, "
            "Canva, зв'язок із 🧭 головним ботом, учасники та їхній доступ.\n\n"
            "Перед скиданням я надішлю вам резервну копію — усе можна буде повернути.\n\n"
            "🎛 Поведінка — пам'ять, креативність, голос, сповіщення, обрана модель · 🧭 Меню й кнопки — розташування, групи, назви · "
            "📌 Закріплення — лише позначки, самі елементи лишаються · 📒 Журнал і пам'ять — журнал запитів і підсумки проєктів · "
            "🏘 Спільноти — правила, дозволи, дописи, журнал (бот лишається в чатах).")
    rows = [[(("✅ " if k in sel else "▫️ ") + label, f"m:frt:{k}")] for k, label, _ in RESET_PARTS]
    rows.append([("☑️ Усе", "m:frt:all"), ("▫️ Лише налаштування", "m:frt:min")])
    rows.append([("🏭 Скинути вибране", "m:frgo")])
    return text, rows


def factory_reset(chat_id, sel):
    """Скидає вибране для цього користувача. Повертає підсумок."""
    done = []
    p = prefs(chat_id)
    if "settings" in sel:
        keep = {k: v for k, v in p.items() if k in BUTTON_PREFS or k == "dialog" or k.startswith("sort_")}
        kv_set(f"prefs:{chat_id}", json.dumps(keep))
        set_choice(chat_id, "auto")
        done.append("налаштування")
    if "buttons" in sel:
        cur = prefs(chat_id)
        kv_set(f"prefs:{chat_id}", json.dumps({k: v for k, v in cur.items() if k not in BUTTON_PREFS}))
        L.hist.pop(chat_id, None)
        done.append("кнопки й меню")
    with closing(db()) as con, con:
        if "dialogs" in sel:
            con.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            con.execute("DELETE FROM dialogs WHERE chat_id=?", (chat_id,))
            con.execute("DELETE FROM folders WHERE chat_id=?", (chat_id,))
            con.execute("UPDATE tasks SET dialog_id=NULL, folder_id=NULL WHERE chat_id=?", (chat_id,))
            done.append("розмови й теки")
        if "tasks" in sel:
            con.execute("DELETE FROM tasks WHERE chat_id=?", (chat_id,))
            done.append("завдання")
        if "notes" in sel:
            con.execute("DELETE FROM notes WHERE chat_id=?", (chat_id,))
            done.append("нотатки")
        if "media" in sel:
            con.execute("DELETE FROM media WHERE chat_id=?", (chat_id,))
            done.append("медіатеку")
        if "journal" in sel:
            con.execute("DELETE FROM journal WHERE chat_id=?", (chat_id,))
            con.execute("UPDATE dialogs SET summary=NULL, summary_upto=NULL WHERE chat_id=?", (chat_id,))
            done.append("журнал")
        for kind, (table, _, _) in KINDS.items():
            if "pins" in sel:
                con.execute(f"UPDATE {table} SET pinned_at=NULL WHERE chat_id=?", (chat_id,))
            if "archive" in sel:
                con.execute(f"DELETE FROM {table} WHERE chat_id=? AND state='archived'", (chat_id,))
            if "trash" in sel:
                con.execute(f"DELETE FROM {table} WHERE chat_id=? AND state='trash'", (chat_id,))
        if "archive" in sel:
            con.execute("DELETE FROM tasks WHERE chat_id=? AND status IN ('done','cancelled')", (chat_id,))
        for k in ("pins", "archive", "trash"):
            if k in sel:
                done.append({"pins": "закріплення", "archive": "архів", "trash": "кошик"}[k])
    if "community" in sel and is_admin_id(chat_id):
        CM.reset()
        done.append("налаштування спільнот")
    if "dialogs" in sel:  # поточна розмова видалена — наступне повідомлення почне нову
        raw = json.loads(kv_get(f"prefs:{chat_id}") or "{}")
        raw.pop("dialog", None)
        kv_set(f"prefs:{chat_id}", json.dumps(raw))
    return done


async def bk_run(bot, chat_id, auto=False, only=None):
    async def send_file(name, data):
        await bot.send_document(chat_id, InputFile(io.BytesIO(data), filename=name),
                                caption=("🗓 Автоматична копія.\n" if auto else "") + "💾 Резервна копія бота. Щоб відновити — надішліть цей файл боту.\n"
                                "Ключі й токени в копію не входять: зберігайте їх окремо.")

    async def send_channel(ch, name, data):
        m = await bot.send_document(ch, InputFile(io.BytesIO(data), filename=name), caption=f"💾 {name}")
        return m.document.file_id

    if auto and not only:  # файл у чат автоматично — лише якщо немає жодного іншого місця
        others = [t for t in ("channel", *CLOUDS) if BK.enabled(t)]
        only = others or ["file"]
    name, size, res = await BK.run(send_file, send_channel, only=only)
    lines = [f"💾 <b>Копія {esc(name)}</b> · {human_size(size)}"]
    for t, ok, detail in res:
        icon, title = TARGET_TITLES[t]
        lines.append(f"{'✅' if ok else '⚠️'} {icon} {title}" + ((f" · {esc(detail)}" if detail else "") if ok else f": {esc(detail)}"))
    if not res:
        lines.append("Немає жодного ввімкненого місця для копій.")
    if auto and all(ok for _, ok, _ in res):
        return res
    await bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.HTML)
    return res


async def auto_backup():
    due = BK.due_targets() if ALLOWED_USERS and tg_app is not None else []
    if due:
        await bk_run(tg_app.bot, owner_id(), auto=True, only=due)


def scr_backups(chat_id):
    return bkui.scr_backups(BK, TZ)


def scr_backup_target(chat_id, t):
    return bkui.scr_backup_target(BK, t, lambda: ask_input(chat_id, "bkch"))


async def scr_backup_list(chat_id, t):
    return await bkui.scr_backup_list(BK, chat_id, t)


def scr_restore_help():
    return bkui.scr_restore(BK)


PENDING_RESTORE = {}  # chat -> байти копії, яку користувач ще не підтвердив


async def ask_restore(bot, chat_id, blob, where):
    try:
        tmp, tables = BK.unpack(blob)
        with closing(sqlite3.connect(tmp)) as con:
            def count(t):
                return con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in tables else 0
            info = f"💬 розмов: {count('dialogs')} · 📝 нотаток: {count('notes')} · ⏰ завдань: {count('tasks')} · 🖼 медіа: {count('media')}"
        os.remove(tmp)
    except BackupError as e:
        return await bot.send_message(chat_id, f"⚠️ Не можу відновити: {e}")
    PENDING_RESTORE[chat_id] = blob
    await bot.send_message(chat_id, f"♻️ <b>Відновити базу з копії?</b>\nДжерело: {esc(where)}\n{info}\n\n"
                           "Поточні дані буде замінено (поточну базу я спершу збережу поруч).",
                           parse_mode=ParseMode.HTML, reply_markup=kb([[("✅ Так, відновити", "m:bkyes"), ("✖️ Ні", "m:bkno")]]))


async def on_backup_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Файл копії, надісланий боту: запропонувати відновлення (лише власнику й адмінам)."""
    if not allowed(update):
        return await deny(update)
    chat_id = update.effective_chat.id
    doc = update.message.document
    if not is_admin_id(chat_id):
        return await update.message.reply_text("Відновлювати копії може лише власник бота.")
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return await update.message.reply_text("Файл більший за 20 МБ — Telegram не дає ботам скачувати такі. Відновіть із хмари.")
    f = await doc.get_file()
    blob = bytes(await f.download_as_bytearray())
    await ask_restore(ctx.bot, chat_id, blob, doc.file_name or "файл")


async def announce_recovered():
    """Сигналізатор: пише власнику, коли модель знову доступна після ліміту."""
    if not ALLOWED_USERS or tg_app is None:
        return
    owner = ALLOWED_USERS[0]
    for pid in PROVIDERS:
        st = get_state(pid)
        until, kind = st.get("cooldown_until"), st.get("cooldown_kind")
        if not until or until > time.time() or kind not in RATE_KINDS:
            continue
        key = f"recovered:{pid}:{int(until)}"
        if kv_get(key):
            continue
        kv_set(key, "1")
        if kind == "rate_minute" or not prefs(owner)["alerts"]:
            continue  # хвилинні паузи надто короткі, щоб про них сповіщати
        waiting = [t for t in tasks_open(chat_id=owner) if t["provider"] == pid and t["status"] == "deferred"]
        extra = f"\n⏳ Відкладених завдань для неї: {len(waiting)} — бот виконає їх протягом хвилини." if waiting else ""
        await tg_app.bot.send_message(
            owner,
            f"✅ {PROVIDERS[pid]['title']} знову доступна: пауза після ліміту закінчилась.{extra}",
            reply_markup=kb([[(f"▶️ Обрати {PROVIDERS[pid]['title']}", f"m:pick:{pid}"), ("📊 Ліміти", "m:lim")]]),
        )


LAST_HB = 0.0


LAST_ACTIVITY = {"t": time.time(), "sleep_sent": False}


def note_activity():
    """Щось відбулося (повідомлення, команда, пробудження). Якщо головному боту вже казали «сплю» — скажемо, що прокинулись."""
    LAST_ACTIVITY["t"] = time.time()
    try:
        NODE.touch()  # 🛰 резервні сервери копіюють базу лише після роботи бота
    except NameError:
        pass
    if LAST_ACTIVITY["sleep_sent"]:
        LAST_ACTIVITY["sleep_sent"] = False
        global LAST_HB
        LAST_HB = 0.0  # наступне коло надішле «я живий»


def my_plan():
    """Мій план сну й пробуджень — те, що знають про мене пов'язані боти."""
    mode = NODE.mode(DEFAULT_MODE)
    if mode != "always" and not HOOK_ON["on"]:
        mode = "always"  # без вебхука бот фактично не засинає
    p = {"name": BOT_USERNAME and f"@{BOT_USERNAME}" or "ШІ-бот", "mode": mode, "window": 15, "tz": "Europe/Kyiv",
         "wake": bot_wake_times() if mode == "timer" and NODE.cron_key() else [], "wake_url": wake_url(),
         "cmd_url": f"{public_base()}/{MCP_SECRET}/cmd" if public_base() else ""}  # куди головному слати команди (міняється при переїзді)
    p["v"] = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]
    return p


async def send_plan_if_changed(force=False):
    """Обмін планами лише при змінах (режим, будильник, графік головного)."""
    if not hub.ready:
        return False
    p = my_plan()
    if not force and NODE.get("plan_sent") == p["v"]:
        return False
    ok = await hub.plan(p)
    if ok:
        NODE.set("plan_sent", p["v"])
        NODE.set("plan_sent_t", time.time())
    return ok


def on_hub_plan(p):
    # головний переїхав на інший сервер (резервний підхопив роботу) — пишемо вже туди
    inbox = (p or {}).get("inbox") if isinstance(p, dict) else None
    if inbox and inbox.startswith("http") and inbox.rstrip("/") != hub.url:
        NODE.set("hub_url", inbox)
        NODE.set("hub_key", hub.key)
        hub.configure(inbox, hub.key)
    if isinstance(p, dict) and p != NODE.get("hub_plan"):
        NODE.set("hub_plan", p)
        NODE.set("hub_plan_t", time.time())
    hub.peer_plan = p


def bot_wake_times():
    """Години пробудження: свої + години перевірок головного бота (щоб бути на зв'язку, коли він перевіряє)."""
    out = list(NODE.wake_times())
    hs = NODE.get("hub_sched") or {}
    if NODE.get("wake_hub", True):
        if hs.get("times"):
            out += hs["times"]
        elif hs.get("daily"):
            out.append({"t": hs["daily"], "d": []})
        elif hs.get("weekly"):
            out.append({"t": hs["weekly"][1], "d": [hs["weekly"][0]]})
    uniq = {(x["t"], tuple(x.get("d", []))): x for x in out}
    return sorted(uniq.values(), key=lambda x: x["t"])


def next_wake_ts():
    """Коли бот прокинеться сам (режим «⏰»): найближче з будильника й запланованих завдань. 0 — лише від повідомлення."""
    if NODE.mode(DEFAULT_MODE) != "timer" or not NODE.cron_key():
        return 0
    saved = NODE.get("wake", [])
    cands = []
    try:
        NODE.set("wake", bot_wake_times())
        cands += [d.timestamp() for d in NODE.next_wakes(limit=1)]
    finally:
        NODE.set("wake", saved)
    if NODE.get("wake_tasks", True):
        cands += [d.timestamp() for _, d in scheduled_task_times()]
    return int(min(cands)) if cands else 0


async def hub_tick():
    """Зв'язок із 🧭 головним ботом. Плани сну обидва боти пересилають лише коли вони змінились; поки змін немає,
    кожен сам знає, коли інший спить, і не турбує його. «Я живий» — лише в режимі «🟢 Завжди активний» і лише коли
    головний не спить; відкладені повідомлення йдуть, щойно він прокинеться."""
    global LAST_HB
    if not hub.ready:
        return
    await send_plan_if_changed()
    await hub.flush_held()
    if NODE.mode(DEFAULT_MODE) != "always" or not hub.peer_awake() or time.time() - LAST_HB < 300:
        return
    LAST_HB = time.time()
    public = env("PUBLIC_URL", "").rstrip("/")
    await hub.heartbeat(cmd_url=f"{public}/{MCP_SECRET}/cmd" if public else None)
    if PROVIDERS and all(paused_until(p) for p in AUTO_ORDER if p in PROVIDERS):
        key = f"hub_allpaused:{int(time.time() // 3600)}"
        if not kv_get(key):
            kv_set(key, "1")
            soonest = min(paused_until(p) for p in AUTO_ORDER if p in PROVIDERS)
            await hub.alert(f"🚨 Усі моделі ШІ-бота на паузі через ліміти. Найраніше звільниться о {hhmm(soonest)}.")


async def task_worker():
    """Раз на TASK_CHECK_EVERY секунд (і одразу після запуску чи пробудження): завдання, копії, спільноти, кластер."""
    while True:
        await worker_pass()
        await asyncio.sleep(TASK_CHECK_EVERY)


WORKER_LOCK = asyncio.Lock()
LAST_CLUSTER = 0


async def worker_pass(force_cluster=False):
    """Одне коло фонової роботи. На резервному сервері кластера — лише перевірка кластера, без завдань."""
    global LAST_CLUSTER
    if WORKER_LOCK.locked():
        return
    async with WORKER_LOCK:
        # у режимах сну перевірки кластера — лише під час пробудження, щоб не заважати засинати
        if NODE.enabled and (force_cluster or NODE.mode(DEFAULT_MODE) == "always") and time.time() - LAST_CLUSTER >= int(env("CLUSTER_CHECK", "60")):
            LAST_CLUSTER = time.time()
            try:
                await NODE.tick(activate_node, deactivate_node, notify_owner)
            except Exception:  # noqa: BLE001
                log.exception("помилка перевірки кластера")
        if not NODE.active:
            return
        await sync_wakes()
        try:
            await hub_tick()
            await announce_recovered()
            await auto_backup()
            await CM.tick(tg_app.bot)
            global LAST_PURGE
            if time.time() - LAST_PURGE > 3600:  # кошик чистимо раз на годину
                LAST_PURGE = time.time()
                purge_old_trash()
            for t in sorted(tasks_open(), key=lambda t: -(RK.score("t", t["id"]) or dialog_score(t.get("dialog_id")))):  # важливіші — першими
                if t["status"] == "scheduled" and t.get("run_at") and t["run_at"] <= time.time():
                    if task_start_mode(t) == "manual":
                        key = f"tnotify:{t['id']}:{int(t['run_at'])}"
                        if not kv_get(key):
                            kv_set(key, "1")
                            await tg_app.bot.send_message(
                                t["chat_id"], f"🔔 <b>Час для завдання #{t['id']}</b>\n«{esc(short_q(t['history_text'], 200))}»\n\nЗапустіть, коли буде зручно — результат я надішлю, щойно виконаю.",
                                parse_mode=ParseMode.HTML,
                                reply_markup=kb([[("▶️ Виконати зараз", f"m:trun:{t['id']}"), ("⏰ Пізніше", f"m:twhen:{t['id']}")], [("❌ Скасувати", f"task:cancel:{t['id']}")]]))
                        continue
                    prov = t["provider"] if t["provider"] in PROVIDERS else None
                    task_update(t["id"], started_at=time.time())
                    result = await run_task(tg_app.bot, task_get(t["id"]), prov, fallback=prov is None, deferred=True)
                    if result is not True:
                        task_update(t["id"], status="choice")
                        await offer_options(tg_app.bot, task_get(t["id"]), result, "⏰ Час настав, але модель не змогла відповісти")
                    continue
                if t["status"] != "deferred":
                    continue
                prov = t["provider"] if t["provider"] in PROVIDERS else None
                if prov and paused_until(prov):
                    continue
                if not prov and all(paused_until(p) for p in AUTO_ORDER):
                    continue
                if t["last_try"] and time.time() - t["last_try"] < 600 and not prov:
                    continue
                task_update(t["id"], started_at=time.time())
                result = await run_task(tg_app.bot, task_get(t["id"]), prov, fallback=prov is None, deferred=True)
                if result is not True:
                    kinds = {i["kind"] for _, i in result}
                    if kinds & NEEDS_ACTION:
                        task_update(t["id"], status="choice")
                        await offer_options(tg_app.bot, task_get(t["id"]), result, "⚠️ Відкладене завдання не вдається виконати")
        except Exception:  # noqa: BLE001
            log.exception("помилка у виконавці завдань")



# ---------------------------------------------------------------- ⚡ режим роботи й 🛰 резервні сервери
NODE = Node(os.path.dirname(os.path.abspath(DB_PATH)) or ".", DB_PATH, TZ)
DEFAULT_MODE = "sleep" if TG_WEBHOOK else "always"
HOOK_ON = {"on": False}
if NODE.get("hub_url") and NODE.get("hub_key"):  # адресу головного бота змінили з меню — вона важливіша за змінні
    hub.configure(NODE.get("hub_url"), NODE.get("hub_key"))
hub.peer_plan = NODE.get("hub_plan")
hub.on_plan = lambda p: on_hub_plan(p)


def public_base():
    """Публічна адреса бота: змінена в меню (🔗 Зв'язки) або зі змінних PUBLIC_URL / RAILWAY_PUBLIC_DOMAIN."""
    return (NODE.get("public_url") or WEBHOOK_BASE or "").rstrip("/")


def wake_url():
    return public_base() + WAKE_PATH if public_base() else ""


async def apply_receiving():
    """Як бот отримує повідомлення: polling (завжди активний) або вебхук (може спати). На резервному сервері — ніяк."""
    if not tg_app:
        return
    up = tg_app.updater
    if not NODE.active:
        if up and up.running:
            await up.stop()
        HOOK_ON["on"] = False
        return
    want_hook = NODE.mode(DEFAULT_MODE) in ("sleep", "timer") and bool(public_base())
    if want_hook:
        if up and up.running:
            await up.stop()
        await tg_app.bot.set_webhook(public_base() + HOOK_PATH, secret_token=HOOK_SECRET, allowed_updates=Update.ALL_TYPES,
                                     drop_pending_updates=False, max_connections=10)
        HOOK_ON["on"] = True
        log.info("Telegram: вебхук — бот може засинати між повідомленнями")
    else:
        if up and not up.running:
            await tg_app.bot.delete_webhook(drop_pending_updates=False)
            await up.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
        HOOK_ON["on"] = False


async def activate_node():
    await apply_receiving()
    NODE.set("plan_sent", None)  # переїхали на інший сервер — головний має дізнатися нову адресу
    try:
        await send_plan_if_changed(force=True)
    except Exception:  # noqa: BLE001
        pass


async def deactivate_node():
    await apply_receiving()


async def notify_owner(text):
    if tg_app and owner_id() and NODE.active:
        try:
            await tg_app.bot.send_message(owner_id(), text)
        except Exception:  # noqa: BLE001
            pass


def scheduled_task_times():
    out = []
    horizon = time.time() + 30 * 86400
    for t in tasks_open():
        if t["status"] == "scheduled" and t.get("run_at") and time.time() < t["run_at"] < horizon:
            out.append((t["id"], datetime.fromtimestamp(t["run_at"], TZ)))
    return out


async def sync_wakes(force=False):
    """Будильник (режим «⏰ Сон + будильник»): пробудження на cron-job.org мають збігатися з розкладом і завданнями.
    Заодно, якщо мій план сну змінився, — повідомляю пов'язаним ботам."""
    try:
        await send_plan_if_changed()
    except Exception:  # noqa: BLE001
        pass
    if not NODE.cron_key() or not wake_url():
        return None
    combined = bot_wake_times()
    sig = json.dumps([NODE.mode(DEFAULT_MODE), combined, NODE.get("wake_tasks", True), [(i, int(d.timestamp())) for i, d in scheduled_task_times()]])
    if not force and sig == NODE.get("cron_sig"):
        return None
    saved = NODE.get("wake", [])
    try:
        NODE.set("wake", combined)
        res = await NODE.sync_cron(wake_url(), scheduled_task_times())
        NODE.set("wake", saved)
        NODE.set("cron_sig", sig)
        NODE.set("cron_error", None)
        return res
    except Exception as e:  # noqa: BLE001
        NODE.set("wake", saved)
        NODE.set("cron_error", str(e)[:200])
        log.warning("будильник: %s", e)
        return None


def scr_runmode(chat_id):
    raw, mode = NODE.raw_mode(DEFAULT_MODE), NODE.mode(DEFAULT_MODE)
    t = RUN_MODES[raw][0] + (f" → {RUN_MODES[mode][0]}" if raw == "auto" else "")
    real = "вебхук (може спати)" if HOOK_ON["on"] else ("опитування Telegram (не спить)" if NODE.active else "у резерві — не отримує повідомлень")
    srv = "🆓 безкоштовний сервер" if NODE.cost() == "free" else "💳 сервер, платний за хвилини (Railway)"
    lines = ["⚡ <b>Режим роботи</b>", f"Зараз: <b>{t}</b> · {esc(real)}", f"Цей сервер: {srv}. Режим зберігається окремо для кожного сервера.", ""]
    for k, (title, desc) in RUN_MODES.items():
        lines.append(f"{'●' if k == raw else '○'} <b>{title}</b> — {esc(desc)}")
    lines.append("")
    if mode in ("sleep", "timer") and not public_base():
        lines.append("⚠️ Для сну потрібна адреса бота: Railway → Settings → Networking → <b>Generate Domain</b>. Поки її немає — бот працює як «завжди активний».")
    if mode == "always":
        lines.append("На Railway: <b>Settings → Deploy → Serverless — вимкнено</b> (у цьому режимі бот і так не засне).")
    else:
        lines.append("На Railway: <b>Settings → Deploy → Serverless — увімкнено</b>, інакше бот не засинатиме й гроші витрачатимуться весь час.")
    if mode == "timer":
        nxt = NODE.next_wakes()
        lines.append(f"\n⏰ Будильник: {esc(NODE.wake_text())}" + (f" · найближче: {nxt[0].strftime('%d.%m %H:%M')}" if nxt else ""))
    if NODE.cost() == "free" and mode != "always":
        lines.append("💡 Сервер безкоштовний: сон тут не економить грошей, а довгий простій може стати приводом для Oracle забрати сервер. Розумніше «🟢» або «🤖».")
    rows = [[(("● " if k == raw else "") + RUN_MODES[k][0], f"m:rmset:{k}")] for k in RUN_MODES]
    if mode == "timer" or raw == "auto":
        rows.append([("⏰ Будильник", "m:rw")])
    rows.append([("🛰 Сервери", "m:cl")])
    return "\n".join(lines), rows


def scr_wake(chat_id):
    w = NODE.wake_times()
    lines = ["⏰ <b>Будильник</b>", "Коли бот має прокидатися сам (час — київський). Прокинувшись, він виконує все, що назбиралося, і через ~10 хв знову засинає."]
    lines.append("\nРозклад: " + esc(NODE.wake_text()))
    nxt = NODE.next_wakes(limit=3)
    if nxt:
        lines.append("Найближчі: " + ", ".join(x.strftime("%d.%m %H:%M") for x in nxt))
    if hub.ready:
        hs = NODE.get("hub_sched")
        lines.append(f"🧭 Графік головного бота: <b>{esc(schedule_text(hs)) if hs else 'ще не отримано'}</b>"
                     + (" · прокидаюсь на його перевірки" if NODE.get("wake_hub", True) and hs else "")
                     + "\nПеред сном бот попереджає головного, до котрої спатиме, — той не б'є на сполох.")
    tasks = scheduled_task_times()
    lines.append(f"🔔 До запланованих завдань: <b>{'так' if NODE.get('wake_tasks', True) else 'ні'}</b>" + (f" (зараз їх {len(tasks)})" if tasks else ""))
    if NODE.cron_key():
        jobs = NODE.get("cron_jobs", {})
        err = NODE.get("cron_error")
        lines.append(f"\n✅ cron-job.org підключено: пробуджень поставлено — {len(jobs)}" + (f"\n⚠️ {esc(err)}" if err else ""))
    else:
        lines.append("\n<b>Хто будитиме бота</b> — безкоштовний сервіс cron-job.org (або Railway Cron):\n"
                     "• Автоматично: зареєструйтесь на cron-job.org → Settings → API → створіть ключ → Railway Variables: "
                     "<code>CRONJOB_API_KEY</code>. Далі бот сам ставить і прибирає пробудження, зокрема до кожного завдання.\n"
                     "• Вручну: на cron-job.org створіть завдання з цією адресою й годинами з розкладу вище:\n"
                     f"<code>{esc(wake_url() or 'спершу Generate Domain у Railway')}</code>")
    rows = [[(f"✖️ {x['t']}" + (f" {','.join(RUN_WEEK[d] for d in x['d'])}" if x.get("d") else ""), f"m:rwdel:{i}")] for i, x in enumerate(w)]
    rows.append([("➕ Додати час", "m:rwadd")])
    rows.append([("🌅 9:00 і 19:00", "m:rwp:2"), ("🕕 Кожні 6 год", "m:rwp:6")])
    rows.append([(("✅" if NODE.get("wake_tasks", True) else "▫️") + " До завдань", "m:rwt")] + ([("🔄 Синхронізувати", "m:rwsync")] if NODE.cron_key() else []))
    if hub.ready:
        rows.append([(("✅" if NODE.get("wake_hub", True) else "▫️") + " На перевірки головного бота", "m:rwh")])
    if wake_url() and not NODE.cron_key():
        rows.append([("📋 Копіювати адресу", "copy:" + wake_url())])
    rows.append([("« Режим роботи", "m:rm")])
    return "\n".join(lines), rows


def parse_wake(text):
    """«09:00», «9:00, 18:30», «09:00 пн,ср,пт», «кожні 4 год» -> [{"t", "d"}]."""
    t = text.strip().lower()
    m = re.match(r"^кожн\w*\s+(\d{1,2})\s*(год|h)", t)
    if m:
        n = max(1, min(12, int(m.group(1))))
        return [{"t": f"{h:02d}:00", "d": []} for h in range(0, 24, n)]
    out = []
    for part in re.split(r"[;\n]+|,(?=\s*\d)", t):
        mm = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*([а-яіїє,\s]*)$", part)
        if not mm:
            raise ValueError(part.strip() or text)
        hh, mi = int(mm.group(1)), int(mm.group(2))
        if hh > 23 or mi > 59:
            raise ValueError(part.strip())
        days = [RUN_WEEK.index(d.strip()) for d in mm.group(3).split(",") if d.strip() in RUN_WEEK]
        out.append({"t": f"{hh:02d}:{mi:02d}", "d": sorted(set(days))})
    return out


def scr_cluster(chat_id):
    return cluster_screen(NODE, esc, TZ)



async def on_task_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        return await q.answer("Доступ закритий", show_alert=True)
    parts = q.data.split(":")
    action, tid = parts[1], int(parts[2])
    task = task_get(tid)
    if not task or task["status"] not in OPEN or task["chat_id"] != q.message.chat_id:
        await q.answer("Це завдання вже виконано або скасовано", show_alert=True)
        return
    if action == "opts":
        await q.answer()
        return await offer_options(ctx.bot, task, [], "Завдання очікує")
    if action == "cancel":
        task_update(tid, status="cancelled")
        if task["msg_id"]:
            set_msg(task["msg_id"], pending=0, in_context=0)
        await q.answer("Скасовано")
        return await q.edit_message_text(f"❌ Завдання #{tid} скасовано: «{short_q(task['history_text'])}»")
    if action == "defer":
        task_update(tid, status="deferred")
        prov = task["provider"] if task["provider"] in PROVIDERS else None
        until = paused_until(prov) if prov else None
        who = PROVIDERS[prov]["title"] if prov else "перша модель, що звільниться"
        when = f" Орієнтовно о {hhmm(until)}." if until else ""
        await q.answer("Відкладено")
        return await q.edit_message_text(
            f"⏳ Завдання #{tid} відкладено: «{short_q(task['history_text'])}»\n"
            f"Виконає: {who}.{when} Відповідь прийде сюди.",
            reply_markup=kb([[("⚙️ Керувати завданням", f"m:t:{tid}"), ("⏰ Усі завдання", "m:tl")]]),
        )
    pid = parts[3] if action == "run" else None
    if pid and pid not in PROVIDERS:
        return await q.answer("Ця модель не підключена", show_alert=True)
    who = PROVIDERS[pid]["title"] if pid else "«Авто»"
    await q.answer(f"Передаю: {who}")
    await q.edit_message_text(f"▶️ Завдання #{tid} передано: {who}. Модель бачить усю попередню розмову…")
    await ctx.bot.send_chat_action(task["chat_id"], ChatAction.TYPING)
    result = await run_task(ctx.bot, task, pid, fallback=pid is None)
    if result is not True:
        task_update(tid, status="choice")
        await offer_options(ctx.bot, task_get(tid), result, f"⏸ {who} теж не змогла відповісти")


# ---------------------------------------------------------------- 🧩 свої моделі

PRESETS = [
    ("DeepSeek", "https://api.deepseek.com/v1"),
    ("xAI Grok", "https://api.x.ai/v1"),
    ("OpenAI", "https://api.openai.com/v1"),
    ("Together", "https://api.together.xyz/v1"),
    ("Perplexity", "https://api.perplexity.ai"),
    ("Cerebras", "https://api.cerebras.ai/v1"),
    ("SambaNova", "https://api.sambanova.ai/v1"),
    ("Fireworks", "https://api.fireworks.ai/inference/v1"),
]
WIZ = {}  # chat_id -> чернетка нової моделі


def custom_get(cid):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM custom_providers WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def custom_save(d):
    with closing(db()) as con, con:
        con.execute(
            "INSERT INTO custom_providers(id, title, base_url, api_key, model, enabled, created) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, base_url=excluded.base_url, api_key=excluded.api_key, "
            "model=excluded.model, enabled=excluded.enabled",
            (d["id"], d["title"], d["base_url"], d["api_key"], d["model"], d.get("enabled", 1), d.get("created", time.time())),
        )
    refresh_providers()


def custom_delete(cid):
    with closing(db()) as con, con:
        con.execute("DELETE FROM custom_providers WHERE id=?", (cid,))
    refresh_providers()


def new_custom_id(title):
    base = "c_" + (re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "model")[:20]
    cid, n = base, 2
    while custom_get(cid) or cid in CATALOG:
        cid, n = f"{base}_{n}", n + 1
    return cid


def mask(key):
    return ("•" * 6 + key[-4:]) if key and len(key) > 6 else "••••"


def scr_custom_list():
    rows_ = custom_rows()
    text = "<b>🧩 Мої моделі</b>\n\n" + (
        "\n".join(f"{'✅' if r['enabled'] else '⏸'} <b>{esc(r['title'])}</b> · <code>{esc(r['model'])}</code>" for r in rows_)
        or "<i>Поки немає. Додайте будь-яку модель із сумісним API.</i>"
    )
    rows = [[(f"{'✅' if r['enabled'] else '⏸'} {r['title']}", f"m:cp:{r['id']}")] for r in rows_]
    rows.append([("➕ Додати модель", "m:cpnew"), ("ℹ️", "help:g:custom")])
    rows.append([("« Моделі", "m:md")])
    return text, rows


def scr_custom(cid):
    r = custom_get(cid)
    if not r:
        return scr_custom_list()
    text = (
        f"<b>🧩 {esc(r['title'])}</b>\n\n"
        f"Адреса: <code>{esc(r['base_url'])}</code>\nМодель: <code>{esc(r['model'])}</code>\nКлюч: {mask(r['api_key'])}\n"
        f"Стан: {'✅ увімкнена' if r['enabled'] else '⏸ вимкнена'}"
        + ("\n\n" + provider_report(cid, short=True) if cid in PROVIDERS else "")
    )
    rows = [
        [("🧪 Перевірити", f"m:cptest:{cid}"), ("⏸ Вимкнути" if r["enabled"] else "▶️ Увімкнути", f"m:cptog:{cid}")],
        [("🤖 Інша модель", f"m:cpmodel:{cid}"), ("🔑 Новий ключ", f"m:cpkey:{cid}")],
        [("✏️ Назва", f"m:cpname:{cid}"), ("🗑 Видалити", f"m:cpdel:{cid}")],
        [("« Мої моделі", "m:cpl")],
    ]
    return text, rows


async def fetch_models(base_url, key):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
        data = r.json().get("data", []) if r.status_code == 200 else []
        return [m.get("id") for m in data if isinstance(m, dict) and m.get("id")][:40]
    except (httpx.HTTPError, ValueError):
        return []


async def test_custom(d):
    """Пробний запит до моделі. Повертає (True, відповідь) або (False, пояснення)."""
    p = {"id": d["id"], "base_url": d["base_url"].rstrip("/"), "key": d["api_key"], "model": d["model"]}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            msg, _ = await post_chat(client, p, [{"role": "user", "content": "Відповідай одним словом: привіт"}], False)
        return True, (msg.get("content") or "").strip()[:200] or "(порожньо)"
    except ProviderError as e:
        icon, title = ERR.get(e.kind, ("❓", e.kind))
        return False, f"{icon} {title}: {e.detail[:250]}"


# ---------------------------------------------------------------- 🎙 голос

VOICE_TEXTS = {}  # id розшифровки -> текст


def scr_voice(chat_id):
    p = prefs(chat_id)
    stt = [g["title"] for g in media.STT_PROVIDERS.values() if g["ready"]()]
    rate = p.get("voice_rate", 0)
    text = (
        "<b>🎙 Голос</b>\n\n"
        f"Розшифровка: {', '.join(stt) if stt else '▫️ не підключена (потрібен ключ Groq або Cloudflare)'}\n"
        f"Озвучення: {'✅ ' + media.VOICES[p.get('voice', 'polina')][1] if media.tts_ready() else '▫️ недоступне на сервері'}\n"
        f"Швидкість мовлення: {rate:+d}%\n{slider(rate, -30, 30)}"
    )
    rows = [
        [(("🔊 " if p.get("voice_reply", True) else "🔇 ") + "Відповідати голосом", "m:vset:reply"),
         (("📝 " if p.get("voice_show", True) else "▫️ ") + "Показувати розшифровку", "m:vset:show")],
        [(("● " if p.get("voice_mode", "ask") == "ask" else "") + "Моє голосове = питання", "m:vset:mode_ask"),
         (("● " if p.get("voice_mode") == "text" else "") + "Лише розшифрувати", "m:vset:mode_text")],
        [(("● " if p.get("voice", "polina") == v else "") + name, f"m:vset:v_{v}") for v, (_, name) in media.VOICES.items()],
        [("🐢 ➖", "m:vset:rate_-"), ("➕ 🐇", "m:vset:rate_+")],
        [("ℹ️", "help:g:voice")],
    ]
    return text, rows


def apply_voice(chat_id, what):
    p = prefs(chat_id)
    if what == "reply":
        set_pref(chat_id, "voice_reply", not p.get("voice_reply", True))
    elif what == "show":
        set_pref(chat_id, "voice_show", not p.get("voice_show", True))
    elif what.startswith("mode_"):
        set_pref(chat_id, "voice_mode", what[5:])
    elif what.startswith("v_"):
        set_pref(chat_id, "voice", what[2:])
    elif what.startswith("rate_"):
        r = p.get("voice_rate", 0) + (10 if what.endswith("+") else -10)
        set_pref(chat_id, "voice_rate", max(-30, min(30, r)))


def transcript_view(vid):
    text = VOICE_TEXTS.get(vid, "")
    body = f"<b>📝 Розшифровка</b>\n<pre>{esc(text)}</pre>"
    rows = []
    if len(text) <= 256:
        rows.append([("📋 Копіювати", f"copy:{text}"), ("✏️ Виправити", f"m:vedit:{vid}")])
    else:
        rows.append([("✏️ Виправити", f"m:vedit:{vid}")])  # довгий текст копіюється кнопкою на самому блоці
    rows.append([("🤖 Відповісти на це", f"m:vans:{vid}"), ("📝 У нотатки", f"m:vnote:{vid}")])
    return body, rows


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    msg = update.message
    chat_id = msg.chat_id
    att = msg.voice or msg.audio or msg.video_note or msg.video
    if att is None and msg.document and (msg.document.mime_type or "").startswith(("audio/", "video/")):
        att = msg.document
    if att is None:
        return
    p = prefs(chat_id)
    forwarded = msg.forward_origin is not None
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    file = await att.get_file()
    data = bytes(await file.download_as_bytearray())
    name = getattr(att, "file_name", None) or ("voice.ogg" if msg.voice else "audio.mp4")
    try:
        async with Progress(ctx.bot, chat_id, "Розшифровую голосове", stop=False, delay=2.5):
            text, prov, errors = await media.transcribe(data, name)
    except media.MediaError as e:
        return await msg.reply_text(f"⚠️ Не вдалося розшифрувати: {e.detail[:300]}\nЯк підключити: /help voice")
    for pr, _ in errors:
        count_request("stt:" + pr, error=True)
    count_request("stt:" + prov)
    vid = secrets.token_hex(4)
    VOICE_TEXTS[vid] = text
    if forwarded or p.get("voice_mode", "ask") == "text":
        body, rows = transcript_view(vid)
        return await msg.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=kb(rows))
    if p.get("voice_show", True):
        body, rows = transcript_view(vid)
        await msg.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=kb(rows[:1]))
    c = get_choice(chat_id)
    reply = await answer(update, ctx, text, "🎙 " + text, None if c == "auto" else c)
    if reply and p.get("voice_reply", True):
        await speak(ctx.bot, chat_id, reply)


async def speak(bot, chat_id, text):
    p = prefs(chat_id)
    await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
    try:
        ogg = await media.tts(text, p.get("voice", "polina"), p.get("voice_rate", 0))
    except media.MediaError as e:
        return await bot.send_message(chat_id, f"🔇 Озвучити не вдалося: {e.detail[:200]}")
    await bot.send_voice(chat_id, InputFile(io.BytesIO(ogg), filename="answer.ogg"))


# ---------------------------------------------------------------- обробка меню (кнопки m:…)


async def finish_wizard(update, ctx, chat_id):
    w = WIZ.pop(chat_id, {})
    if not all(w.get(k) for k in ("title", "base_url", "api_key", "model")):
        return await ctx.bot.send_message(chat_id, "Чернетка неповна, почніть знову: 🤖 Моделі → ➕ Додати модель")
    w["id"] = new_custom_id(w["title"])
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    ok, info = await test_custom(w)
    custom_save(w)
    t, rows = scr_custom(w["id"])
    head = f"✅ Модель додано й перевірено. Відповідь: {esc(info)}\n\n" if ok else (
        f"⚠️ Модель збережено, але перевірка не пройшла:\n{esc(info)}\nВиправте ключ чи назву моделі нижче.\n\n"
    )
    await ctx.bot.send_message(chat_id, head + t, parse_mode=ParseMode.HTML, reply_markup=kb(rows))


EDITOR_ACTIONS = {"btn", "bi", "bmv", "bup", "bdn", "bset", "lren", "lunren", "bgnew", "bgdel", "brl", "brldel", "breset"}
VIEW_ACTIONS = {"home", "dl", "d", "f", "tl", "t", "md", "lim", "st", "nt", "cr", "canva", "pins", "box", "it", "acc",
                "mem", "mdl", "mdv", "btn", "bi", "brl", "voice", "cpl", "cp", "bk", "fr", "svc", "sv", "svls", "diag", "jl", "j", "bks", "bkt", "bkls", "bkrs", "bkfmt", "bkscope", "lib", "mi", "note", "bl", "pr", "pri", "backup", "qask", "rm", "rw", "cl"}


def is_view(data):
    """Екран, який можна відкрити повторно (для «Назад/Вперед»), а не дія, що щось змінює."""
    if data.startswith("help:"):
        return True
    parts = data.split(":")
    if len(parts) > 1 and parts[1] == "cm":
        return CM.is_view(parts)
    return len(parts) > 1 and parts[1] in VIEW_ACTIONS


async def open_data(update, ctx, data, edit=True, record=True):
    """Відкриває екран за даними кнопки (m:… або help:…)."""
    chat = update.effective_chat.id
    if data.startswith("help:"):
        if record:
            L.visit(chat, data.replace("help:pick:", "help:m:"))
        text, rows = help_screen(data)
        return await show(update, text, rows, edit=edit)
    return await route(update, ctx, data, edit=edit, record=record)


async def route(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data, edit=True, record=True):
    chat_id = update.effective_chat.id
    q = update.callback_query
    parts = data.split(":")
    a = parts[1] if len(parts) > 1 else "home"
    arg = parts[2] if len(parts) > 2 else None
    toast = None
    screen = None

    if a == "noop":
        return await q.answer() if q else None
    if a == "more":  # «Показати ще ▼» / «▲ Згорнути» — перемальовуємо поточний екран
        set_pref(chat_id, "fold", {"screen": L.current(chat_id), "n": int(arg or 0) or LIST_ROWS})
        if q:
            await q.answer()
        return await open_data(update, ctx, L.current(chat_id) or "m:home", edit=edit, record=False)
    if a == "nav":
        if arg == "home":
            L.visit(chat_id, "m:home")
            target = "m:home"
        else:
            target = L.back(chat_id) if arg == "back" else L.fwd(chat_id)
        if q:
            await q.answer()
        return await open_data(update, ctx, target, edit=edit, record=False)
    if record and is_view(data):
        L.visit(chat_id, data)
    if a == "cancel":
        AWAIT.pop(chat_id, None)
        screen = scr_home(chat_id)
    elif a == "home":
        screen = scr_home(chat_id)
    elif a == "qask":
        if not PROVIDERS:
            screen = (
                "🤖 <b>Спитати конкретну модель</b>\n\nПоки не підключено жодної моделі.",
                [[("🤖 Підключити моделі", "m:md"), HOME]],
            )
        else:
            rows = [[(PROVIDERS[pid]["title"], f"m:qaskm:{pid}")] for pid in PROVIDERS]
            rows.append([HOME])
            screen = ("🤖 <b>Спитати конкретну модель один раз</b>\n\nОберіть модель:", rows)
    elif a == "qaskm":
        if not arg or arg not in PROVIDERS:
            screen = ("⚠️ Ця модель зараз недоступна.", [[("🤖 Моделі", "m:md"), HOME]])
        else:
            ask_input(chat_id, "qask", arg)
            screen = (
                f"🤖 <b>{esc(PROVIDERS[arg]['title'])}</b>\n\nНапишіть одне питання для цієї моделі.",
                [CANCEL_ROW],
            )
    # розмови
    elif a == "dl":
        screen = scr_dialogs(chat_id)
    elif a == "dnew":
        did = dialog_create(chat_id, folder_id=int(arg) if arg else None)
        toast = "Нова розмова почата"
        screen = (
            "<b>➕ Нова розмова</b>\n\nПишіть перше питання — воно стане назвою розмови. "
            "Попередня розмова збережена в «💬 Розмови».",
            [[("✏️ Назвати одразу", f"m:dren:{did}"), ("📁 У теку", f"m:dmv:{did}")], [("« Розмови", "m:dl"), HOME]],
        )
    elif a == "d":
        screen = scr_dialog(chat_id, int(arg))
        screen = (screen[0], [prio_row("d", arg)] + screen[1])
    elif a == "dsw":
        set_pref(chat_id, "dialog", int(arg))
        toast = "Розмову продовжено"
        screen = scr_dialog(chat_id, int(arg))
    elif a == "dren":
        ask_input(chat_id, "dren", int(arg))
        screen = ("✏️ <b>Напишіть нову назву розмови</b>", [CANCEL_ROW])
    elif a == "dmv":
        screen = scr_move(chat_id, "d", int(arg))
    elif a == "ddel":
        d = dialog_get(int(arg))
        screen = (
            f"🗑 <b>Видалити розмову «{esc(d['title'] if d else '')}»?</b>\nІсторію буде стерто, нотатки лишаться.",
            [[("🗑 Так, видалити", f"m:ddely:{arg}"), ("« Ні", f"m:d:{arg}")]],
        )
    elif a == "ddely":
        dialog_delete(int(arg))
        if prefs(chat_id).get("dialog") == int(arg):
            set_pref(chat_id, "dialog", None)
        toast = "Розмову видалено"
        screen = scr_dialogs(chat_id)
    # теки
    elif a == "f":
        screen = scr_folder(chat_id, int(arg))
    elif a == "fnew":
        ask_input(chat_id, "fnew")
        screen = ("📁 <b>Напишіть назву нової теки</b>\nНаприклад: «Робота», «GreenPost», «Ідеї»", [CANCEL_ROW])
    elif a == "fren":
        ask_input(chat_id, "fren", int(arg))
        screen = ("✏️ <b>Напишіть нову назву теки</b>", [CANCEL_ROW])
    elif a == "fdel":
        f = folder_get(int(arg))
        screen = (
            f"🗑 <b>Видалити теку «{esc(f['name'] if f else '')}»?</b>\nРозмови й завдання з неї не зникнуть, а перейдуть у «Без теки».",
            [[("🗑 Так", f"m:fdely:{arg}"), ("« Ні", f"m:f:{arg}")]],
        )
    elif a == "fdely":
        folder_delete(int(arg))
        toast = "Теку видалено"
        screen = scr_dialogs(chat_id)
    elif a == "mv":
        kind, item, fid = parts[2], int(parts[3]), int(parts[4])
        if kind == "d":
            dialog_update(item, folder_id=fid or None)
            screen = scr_dialog(chat_id, item)
        else:
            task_update(item, folder_id=fid or None)
            screen = scr_task(chat_id, item)
        toast = "Перенесено"
    elif a == "mvnew":
        ask_input(chat_id, "mvnew", (parts[2], int(parts[3])))
        screen = ("📁 <b>Назва нової теки?</b>", [CANCEL_ROW])
    # завдання
    elif a == "tl":
        screen = scr_tasks(chat_id)
    elif a in ("tnew", "tnewf"):
        ask_input(chat_id, "tnew", {"dialog": int(arg) if a == "tnew" and arg else None, "folder": int(arg) if a == "tnewf" else None})
        screen = (
            "⏰ <b>Нове відкладене завдання</b>\n\nНапишіть, що потрібно зробити. Далі оберете час і модель.\n"
            "Наприклад: «склади план постів на наступний тиждень».",
            [CANCEL_ROW],
        )
    elif a == "t":
        screen = scr_task(chat_id, int(arg))
        screen = (screen[0], [prio_row("t", arg)] + screen[1])
    elif a == "twhen":
        screen = scr_when(int(arg))
    elif a == "tw":
        tid, opt = int(parts[2]), parts[3]
        if opt == "custom":
            ask_input(chat_id, "twhen", tid)
            screen = ("✍️ <b>Коли виконати?</b>\n<code>18:30</code>, <code>25.12 10:00</code> або <code>через 40 хв</code>", [CANCEL_ROW])
        elif opt == "free":
            task_update(tid, status="deferred", run_at=None)
            toast = "Виконаю, щойно модель звільниться"
            screen = scr_task(chat_id, tid)
        else:
            task_update(tid, status="scheduled", run_at=when_to_ts(opt))
            toast = f"Заплановано на {hhmm(when_to_ts(opt))}"
            t = task_get(tid)
            screen = scr_task_model(tid) if t.get("kind") == "planned" and t.get("provider") is None and not t.get("reason") else scr_task(chat_id, tid)
            if t.get("kind") == "planned":
                task_update(tid, reason="")
    elif a == "tsm":
        task_update(int(arg), start_mode=parts[3])
        set_pref(chat_id, "task_start", parts[3])  # запам'ятати як типовий вибір
        toast = dict(START_MODES)[parts[3]]
        screen = scr_when(int(arg)) if not (len(parts) > 4) else scr_task(chat_id, int(arg))
    elif a == "trun":
        task = task_get(int(arg))
        if task and task["status"] in OPEN:
            await q.answer("Виконую…")
            await q.edit_message_text(f"▶️ Виконую завдання #{arg}…")
            prov = task["provider"] if task["provider"] in PROVIDERS else None
            task_update(task["id"], started_at=time.time())
            async with Progress(ctx.bot, chat_id, f"Виконую завдання #{task['id']}"):
                result = await run_task(ctx.bot, task_get(task["id"]), prov, fallback=prov is None, deferred=True)
            done = result is True
            try:  # проміжне «Виконую…» стає позначкою результату — окремого сповіщення про старт немає
                await q.edit_message_text(f"✅ Завдання #{arg} виконано — результат нижче." if done else f"⚠️ Завдання #{arg} не вдалося виконати — варіанти нижче.")
            except BadRequest:
                pass
            if not done:
                task_update(task["id"], status="choice")
                await offer_options(ctx.bot, task_get(task["id"]), result)
            return
        screen = scr_tasks(chat_id)
    elif a == "tmodel":
        screen = scr_task_model(int(arg))
    elif a == "tm":
        tid, pid = int(parts[2]), parts[3]
        task_update(tid, provider=None if pid == "auto" else pid)
        toast = "Модель обрано"
        screen = scr_task(chat_id, tid)
    elif a == "tmv":
        screen = scr_move(chat_id, "t", int(arg))
    # моделі, ліміти, налаштування
    elif a == "md":
        screen = scr_models(chat_id)
    elif a == "pick":
        set_choice(chat_id, arg)
        toast = f"Відповідає: {choice_label(arg)}"
        if arg != "auto" and paused_until(arg):
            toast += f" (зараз на паузі до {hhmm(paused_until(arg))})"
        screen = scr_models(chat_id)
    elif a == "lim":
        if q:
            await q.answer("Оновлюю…")
        screen = scr_limits(arg)
    elif a == "st":
        screen = scr_settings(chat_id)
    elif a == "set":
        apply_setting(chat_id, parts[2], parts[3] if len(parts) > 3 else None)
        screen = scr_create(chat_id) if len(parts) > 4 and parts[4] == "cr" else scr_settings(chat_id)
    # нотатки
    elif a == "nt":
        screen = scr_notes(chat_id, int(arg or 0))
    elif a == "sort":
        set_pref(chat_id, f"sort_{arg}", parts[3])
        screen = {"notes": lambda: scr_notes(chat_id), "tasks": lambda: scr_tasks(chat_id), "media": lambda: scr_media(chat_id)}[arg]()
    elif a == "lib":
        screen = scr_media(chat_id, int(arg or 0), parts[3] if len(parts) > 3 else None)
    elif a == "mi":
        screen = scr_media_item(chat_id, int(arg))
    elif a == "msend":
        it = item_get("m", int(arg))
        if q:
            await q.answer()
        if it:
            await resend_media(ctx.bot, chat_id, it)
        return
    elif a == "nadd":
        ask_input(chat_id, "nadd")
        screen = ("📝 <b>Текст нотатки?</b>", [CANCEL_ROW])
    elif a == "nfind":
        ask_input(chat_id, "nfind")
        screen = ("🔍 <b>Що шукати в нотатках?</b>", [CANCEL_ROW])
    elif a == "ndel":
        delete_note_db(int(arg))
        toast = "Нотатку видалено"
        screen = scr_notes(chat_id)
    # створити
    elif a == "cr":
        screen = scr_create(chat_id)
    elif a == "gen":
        ask_input(chat_id, "gen", arg)
        screen = (GEN_PROMPTS[arg], [CANCEL_ROW])
    elif a == "again":
        prompt = LAST_PROMPT.get((chat_id, arg))
        if prompt and q:
            await q.answer("Генерую ще раз…")
            return await do_image(ctx.bot, chat_id, prompt)
        screen = scr_create(chat_id)
    elif a == "canva":
        screen = scr_canva(chat_id)
    elif a == "canvaout":
        canva.disconnect()
        toast = "Вийшли з Canva"
        screen = scr_canva(chat_id)
    # закріплені
    elif a == "pins":
        if arg:
            set_pref(chat_id, "pin_sort", arg)
        screen = scr_pins(chat_id, arg)
    elif a == "pin":
        kind, item = parts[2], int(parts[3])
        now_pinned = toggle_pin(kind, item)
        toast = "📌 Закріплено" if now_pinned else "Відкріплено"
        screen = {"d": lambda: scr_dialog(chat_id, item), "f": lambda: scr_folder(chat_id, item),
                  "t": lambda: scr_task(chat_id, item), "n": lambda: scr_note(chat_id, item)}[kind]()
    # архів і кошик
    elif a in ("arc", "trs"):
        kind, item = parts[2], int(parts[3])
        set_state(kind, item, "archived" if a == "arc" else "trash")
        if kind == "d" and prefs(chat_id).get("dialog") == item:
            set_pref(chat_id, "dialog", None)
        toast = "🗄 Перенесено в архів" if a == "arc" else f"🗑 У кошику: видалиться через {TRASH_DAYS} днів"
        if len(parts) > 4 and parts[4] == "box":
            screen = scr_box(chat_id, "archived")
        else:
            screen = {"d": lambda: scr_dialogs(chat_id), "f": lambda: scr_dialogs(chat_id),
                      "t": lambda: scr_tasks(chat_id), "n": lambda: scr_notes(chat_id), "m": lambda: scr_media(chat_id)}[kind]()
    elif a == "box":
        screen = scr_box(chat_id, arg)
    elif a == "bl":
        screen = scr_box_list(chat_id, parts[2], parts[3], int(parts[4]))
    elif a == "it":
        screen = scr_item(chat_id, parts[2], parts[3], int(parts[4]))
    elif a == "rs":
        st, kind, item = parts[2], parts[3], int(parts[4])
        set_state(kind, item, "active")
        toast = "↩️ Відновлено"
        screen = scr_box_list(chat_id, st, kind, 0)
    elif a == "pg":
        kind, item = parts[2], int(parts[3])
        it = item_get(kind, item)
        screen = (
            f"❌ <b>Видалити назавжди?</b>\n«{esc(short_q(item_title(kind, it), 80)) if it else ''}»\nВідновити буде неможливо.",
            [[("❌ Так, назавжди", f"m:pgy:{kind}:{item}"), ("« Ні", f"m:it:trash:{kind}:{item}")]],
        )
    elif a == "pgy":
        purge(parts[2], int(parts[3]))
        toast = "Видалено назавжди"
        screen = scr_box_list(chat_id, "trash", parts[2], 0)
    elif a == "empty":
        screen = ("🧹 <b>Очистити кошик?</b>\nУсе з кошика буде видалено назавжди.", [[("🧹 Так, очистити", "m:emptyy"), ("« Ні", "m:box:trash")]])
    elif a == "emptyy":
        for kind in KINDS:
            for it in items_in(chat_id, kind, "trash"):
                purge(kind, it["id"])
        toast = "Кошик очищено"
        screen = scr_box(chat_id, "trash")
    elif a == "note":
        screen = scr_note(chat_id, int(arg))
    elif a == "redo":
        old = task_get(int(arg))
        tid = create_planned_task(chat_id, old["history_text"], old.get("dialog_id"), old.get("folder_id"))
        screen = scr_when(tid)
    # спільний доступ (лише власник)
    elif (a in ("acc", "inv", "invx", "accok", "accno", "accrm", "mem", "mdl", "mdv", "mdc", "addmem", "prex", "backup", "mview") or a.startswith("bk")) and not is_admin_id(chat_id):
        toast = "Лише для власника бота"
        screen = scr_home(chat_id)
    elif a == "acc":
        screen = scr_access()
    elif a == "inv":
        code = invite_create(arg == "1")
        toast = "Запрошення створено"
        screen = (
            f"🔗 <b>Запрошення готове</b>\n\nНадішліть це посилання людині:\n<code>{esc(invite_link(code))}</code>\n\n"
            "Коли вона його відкриє, вам прийде запит на підтвердження. Посилання дійсне 7 днів"
            + (" і лише для однієї людини." if arg == "1" else "."),
            [[("« Доступ", "m:acc"), HOME]],
        )
    elif a == "invx":
        invite_revoke(arg)
        toast = "Запрошення скасовано"
        screen = scr_access()
    elif a in ("accok", "accno", "accrm"):
        uid = int(arg)
        member_set(uid, {"accok": "approved", "accno": "blocked", "accrm": "removed"}[a])
        if a == "accok":
            toast = "✅ Доступ надано"
            try:
                await ctx.bot.send_message(
                    uid,
                    "✅ Власник підтвердив ваш доступ. Ласкаво просимо!",
                    reply_markup=menu_kb(uid),
                )
                t, r = scr_home(uid)
                await ctx.bot.send_message(uid, t, parse_mode=ParseMode.HTML, reply_markup=kb(r))
            except Exception:  # noqa: BLE001
                log.warning("не вдалося написати новому учаснику %s", uid)
        elif a == "accno":
            toast = "Доступ закрито"
            try:
                await ctx.bot.send_message(uid, "Доступ до бота закрито.")
            except Exception:  # noqa: BLE001
                pass
        else:
            toast = "Видалено зі списку"
        screen = scr_access()
    elif a in EDITOR_ACTIONS:
        prompts = []

        def ask(action, d, prompt):
            ask_input(chat_id, action, d)
            if prompt:
                prompts.append(prompt)

        screen, toast = handle_editor(L, chat_id, parts, ask)
        if prompts:
            if q:
                await q.answer()
            return await ctx.bot.send_message(chat_id, prompts[0], parse_mode=ParseMode.HTML, reply_markup=kb([CANCEL_ROW]))
        if a in ("bmv", "bset", "breset", "bgdel", "lunren") and not toast:
            toast = "Меню оновлено"  # нижньої клавіатури немає — окреме повідомлення не потрібне
    elif a in ("svc", "sv", "svcon", "svdis", "svls", "svdone", "svdef", "svauto", "svadd", "svtg", "svtg2", "svp", "svsend") and not is_admin_id(chat_id):
        screen = ("🔗 Сервіси підключені до акаунтів власника бота, тому доступні лише йому.", [])
    elif a == "svc":
        screen = scr_services(chat_id)
    elif a == "sv":
        screen = scr_service(chat_id, arg)
    elif a == "svcon" and arg == "iphone":
        SVC.connect_iphone()
        toast, screen = "📱 Ключ створено", scr_service(chat_id, arg)
    elif a == "svcon":
        ask_input(chat_id, "svcon", arg)
        screen = (f"🔑 <b>{SERVICES[arg]['title']}</b>\n\n{SERVICES[arg]['how']}\n\nПовідомлення з токеном я одразу видалю.", [CANCEL_ROW])
    elif a == "svdis":
        SVC.disconnect(arg)
        toast, screen = "Відключено", scr_service(chat_id, arg)
    elif a == "svdef":
        SVC.set_default(arg, parts[3])
        toast, screen = "⭐️ Тепер за замовчуванням", scr_service(chat_id, parts[3])
    elif a == "svauto":
        SVC.toggle_auto(arg)
        screen = scr_services(chat_id)
    elif a == "svls":
        if q:
            await q.answer("Завантажую…")
        screen = await scr_svc_items(chat_id, arg)
    elif a == "svdone":
        sid, n = arg, int(parts[3])
        try:
            await SVC.complete(sid, SVC_LAST.get(chat_id, {}).get(sid, [])[n])
            toast = "✅ Виконано"
        except (ServiceError, IndexError) as e:
            toast = f"⚠️ {e}"[:190]
        screen = await scr_svc_items(chat_id, sid)
    elif a == "svadd":
        ask_input(chat_id, "svadd", arg)
        k = SERVICES[arg]["kinds"][0]
        screen = (f"➕ <b>{SERVICES[arg]['title']}</b>\n\nНадішліть {'завдання' if k == 'task' else 'нотатку'}. Перший рядок — назва, далі — опис."
                  + ("\nСтрок можна дописати в кінці першого рядка після «@»: <code>Купити хліб @tomorrow</code>" if k == "task" else ""), [CANCEL_ROW])
    elif a == "svtg":
        if q:
            await q.answer("Завантажую…")
        try:
            targets = await SVC.choose_targets(arg)
            SVC_LAST.setdefault(chat_id, {})["tg:" + arg] = targets
            screen = ("📍 <b>Куди складати нові записи?</b>", [[(short_q(nm, 40), f"m:svtg2:{arg}:{n}")] for n, (_, nm) in enumerate(targets[:20])])
        except ServiceError as e:
            toast, screen = f"⚠️ {e}"[:190], scr_service(chat_id, arg)
    elif a == "svtg2":
        tid, nm = SVC_LAST.get(chat_id, {}).get("tg:" + arg, [])[int(parts[3])]
        SVC.set_target(arg, tid, nm)
        toast, screen = "📍 Збережено", scr_service(chat_id, arg)
    elif a == "svp":
        screen = scr_svc_pick(arg, int(parts[3]))
    elif a == "svsend":
        sid, src, iid = arg, parts[3], int(parts[4])
        it = item_get("n" if src == "n" else "t", iid)
        text = (it or {}).get("text") or (it or {}).get("history_text") or ""
        kind = "note" if src == "n" or "task" not in SERVICES[sid]["kinds"] else "task"
        if kind == "note" and "note" not in SERVICES[sid]["kinds"]:
            kind = "task"
        due = None
        if src == "t" and it and it.get("run_at"):
            due = datetime.fromtimestamp(it["run_at"], TZ).strftime("%Y-%m-%d")
        if q:
            await q.answer("Надсилаю…")
        try:
            url = await SVC.add(sid, kind, text.split("\n")[0][:120], text, due)
            screen = (f"✅ Надіслано в {SERVICES[sid]['title']}." + (" iPhone забере під час наступного запуску команди «Забрати з бота»." if sid == "iphone" else ""),
                      [[("🔗 Відкрити", "url:" + url)]] if url.startswith("http") else [])
        except ServiceError as e:
            screen = (f"⚠️ {SERVICES[sid]['title']}: {esc(str(e))}", [[("🧩 Інтеграції", "m:svc")]])
    elif a in ("rm", "rmset", "rw", "rwadd", "rwdel", "rwp", "rwt", "rwh", "rwsync", "cl", "clrun") and not is_admin_id(chat_id):
        screen = ("⚡ Режим роботи змінює лише власник бота.", [])
    elif a == "rm":
        screen = scr_runmode(chat_id)
    elif a == "rmset":
        if arg in RUN_MODES:
            NODE.set("mode", arg)
            try:
                await apply_receiving()
            except Exception as e:  # noqa: BLE001
                toast = f"Не вдалося перемкнути: {e}"[:190]
            await sync_wakes(force=True)
            toast = toast or RUN_MODES[arg][0]
        screen = scr_runmode(chat_id)
    elif a == "rw":
        screen = scr_wake(chat_id)
    elif a == "rwadd":
        ask_input(chat_id, "rwadd")
        screen = ("⏰ Коли будити? Приклади:\n<code>09:00</code> · <code>9:00, 18:30</code> · <code>08:00 пн,ср,пт</code> · <code>кожні 4 год</code>", [CANCEL_ROW])
    elif a == "rwdel":
        w = NODE.wake_times()
        if arg and arg.isdigit() and int(arg) < len(w):
            w.pop(int(arg))
            NODE.set("wake", w)
            await sync_wakes()
        screen = scr_wake(chat_id)
    elif a == "rwp":
        NODE.set("wake", parse_wake("9:00, 19:00") if arg == "2" else parse_wake("кожні 6 год"))
        await sync_wakes()
        toast, screen = "Будильник задано", scr_wake(chat_id)
    elif a == "rwh":
        NODE.set("wake_hub", not NODE.get("wake_hub", True))
        await sync_wakes()
        screen = scr_wake(chat_id)
    elif a == "rwt":
        NODE.set("wake_tasks", not NODE.get("wake_tasks", True))
        await sync_wakes()
        screen = scr_wake(chat_id)
    elif a == "rwsync":
        res = await sync_wakes(force=True)
        toast = (f"Додано {res[0]}, прибрано {res[1]}" if res else (NODE.get("cron_error") or "Без змін"))[:190]
        screen = scr_wake(chat_id)
    elif a == "cl":
        screen = scr_cluster(chat_id)
    elif a == "clrun":
        await worker_pass(force_cluster=True)
        toast, screen = "Перевірено", scr_cluster(chat_id)
    elif a == "pr":
        screen = scr_prio(chat_id, arg or "d")
    elif a == "pri":
        k = parts[2]
        screen = scr_prio_item(chat_id, k, ":".join(parts[3:]))
    elif a == "prm":
        k, step, ref = parts[2], parts[-1], ":".join(parts[3:-1])
        prio_ranks(k).move(k, ref, step)
        toast = {"up": "Вище", "down": "Нижче", "top": "На самому верху", "bottom": "У самому низу", "reset": "Звичайний пріоритет"}.get(step)
        back = L.current(chat_id) or f"m:pr:{k}"
        if back.startswith("m:pri:"):
            screen = scr_prio_item(chat_id, k, ref)
        elif back.startswith("m:d:") or back.startswith("m:t:"):  # стрілки на екрані розмови чи завдання — лишаємось на ньому
            if q:
                await q.answer(toast)
            return await route(update, ctx, back, edit=edit, record=False)
        else:
            screen = scr_prio(chat_id, k)
    elif a == "cm":
        if not is_admin_id(chat_id):
            screen = scr_home(chat_id)
        else:
            toast, screen = await CM.route(chat_id, parts)
    elif a == "stop":
        p = RUNNING.get(arg)
        if p and p.chat_id == chat_id and p.task and not p.task.done():
            p.cancelled = True
            p.task.cancel()
            return await q.answer("Зупиняю…") if q else None
        return await q.answer("Вже готово або зупинено", show_alert=False) if q else None
    elif a == "jl":
        screen = scr_journal(chat_id, int(arg or 0), parts[3] if len(parts) > 3 else None)
    elif a == "j":
        screen = scr_journal_item(chat_id, int(arg))
    elif a == "jrun":
        if q:
            await q.answer("Продовжую…")
        jj = journal_get(int(arg)) or {}

        async def go_j():
            async with Progress(ctx.bot, chat_id, "Продовжую запит"):
                await journal_run(update, ctx, int(arg), parts[3])

        return await with_chat_queue(update, "продовження запиту", go_j, jj.get("dialog_id") or current_dialog(chat_id))
    elif a in ("lkwake", "lkplan", "lkhub", "lkpub", "clpref", "clsel", "clpool") and not is_admin_id(chat_id):
        screen = ("Лише для власника бота.", [])
    elif a == "lkwake":
        if q:
            await q.answer("Буджу головного бота…")
        ok_ = await hub.wake_peer() or await hub.heartbeat(cmd_url=f"{public_base()}/{MCP_SECRET}/cmd" if public_base() else None)
        await update.effective_chat.send_message("⏰ Головний бот розбуджено — він на зв'язку." if ok_ else "⚠️ Не вдалося достукатися до головного бота.")
        if ok_:
            await hub.flush_held(force=True)
        screen = scr_diag(chat_id, "links")
    elif a == "lkplan":
        toast = "План надіслано" if await send_plan_if_changed(force=True) else "Не вдалося надіслати план"
        screen = scr_diag(chat_id, "links")
    elif a == "lkhub":
        ask_input(chat_id, "lkhub")
        screen = ("✏️ <b>Головний бот</b>\nНадішліть адресу скриньки й ключ через пробіл, наприклад:\n"
                  "<code>https://hub.example.com/СЕКРЕТ/in КЛЮЧ</code>\n(Ключ — у головному боті: картка бота → 🔌 Підключення.) "
                  "«-» — повернути адресу зі змінних сервера.", [CANCEL_ROW])
    elif a == "lkpub":
        ask_input(chat_id, "lkpub")
        screen = ("✏️ <b>Моя публічна адреса</b> (куди Telegram, будильник і інші боти звертаються до мене)\nНаприклад: "
                  "<code>https://orionai-production.up.railway.app</code>\n«-» — як у змінних сервера.\nЗараз: <code>" + esc(public_base() or "—") + "</code>", [CANCEL_ROW])
    elif a in ("clpref", "clsel", "clpool"):
        toast = await cluster_route(NODE, a, arg)
        await worker_pass(force_cluster=True)
        screen = scr_cluster(chat_id)
    elif a == "diag":
        screen = scr_diag(chat_id, arg or "sum")
    elif a == "fr":
        screen = scr_reset(chat_id)
    elif a == "frt":
        sel = reset_sel(chat_id)
        if arg == "all":
            sel = {k for k, _, _ in RESET_PARTS}
        elif arg == "min":
            sel = {"settings", "buttons"}
        else:
            sel ^= {arg}
        kv_set(f"reset_sel:{chat_id}", json.dumps(sorted(sel)))
        screen = scr_reset(chat_id)
    elif a == "frgo":
        sel = reset_sel(chat_id)
        if not sel:
            toast, screen = "Нічого не позначено", scr_reset(chat_id)
        else:
            names = [label.split(" (")[0] for k, label, _ in RESET_PARTS if k in sel]
            screen = ("<b>🏭 Точно скинути?</b>\n\n" + "\n".join(names) +
                      "\n\nПідключення (моделі, хмари, Canva, головний бот, учасники) лишаються. Спершу я надішлю резервну копію.",
                      [[("✅ Так, скинути", "m:fryes"), ("✖️ Ні", "m:fr")]])
    elif a == "fryes":
        if q:
            await q.answer("Роблю копію й скидаю…")
        try:
            await bk_run(ctx.bot, chat_id, only=["file"])
        except Exception as e:  # noqa: BLE001
            log.warning("копія перед скиданням не вдалася: %s", e)
        done = factory_reset(chat_id, reset_sel(chat_id))
        await ctx.bot.send_message(chat_id, "🏭 Скинуто: " + ", ".join(done) + ".\nПовернути все можна, надіславши боту файл копії вище.")
        L.visit(chat_id, "m:home")
        return await show(update, *scr_home(chat_id), edit=False)
    elif a == "grp":
        # Акордеон: група розгортається прямо всередині головного меню.
        screen = scr_home(chat_id, arg)
    elif a == "grpx":
        # Повторне натискання на відкриту категорію згортає її (вкладена — повертає до батьківської).
        screen = scr_home(chat_id, L.parent_group(chat_id, arg))
    elif a == "voice":
        screen = scr_voice(chat_id)
    elif a == "vset":
        apply_voice(chat_id, arg)
        screen = scr_voice(chat_id)
    elif a in ("vedit", "vans", "vnote"):
        text = VOICE_TEXTS.get(arg)
        if text is None:
            toast = "Розшифровка застаріла: перешліть голосове ще раз"
            screen = None
        elif a == "vedit":
            ask_input(chat_id, "vedit", (arg, q.message.message_id if q else None))
            screen = None
            await ctx.bot.send_message(
                chat_id,
                "✏️ Скопіюйте текст, виправте й надішліть мені — я оновлю розшифровку.",
                reply_markup=kb([CANCEL_ROW]),
            )
        elif a == "vnote":
            nid = add_note_db(text, "голосове", "telegram")
            toast = f"📝 Збережено в нотатки #{nid}"
            screen = None
        else:
            if q:
                await q.answer("Передаю моделі…")
            c = get_choice(chat_id)
            reply = await answer(update, ctx, text, "🎙 " + text, None if c == "auto" else c)
            if reply and prefs(chat_id).get("voice_reply", True):
                await speak(ctx.bot, chat_id, reply)
            return
    elif a in ("cpl", "cpnew", "cp", "cptest", "cptog", "cpmodel", "cpkey", "cpname", "cpdel", "cpdely", "cpurl", "cpmod") and not is_admin_id(chat_id):
        toast = "Лише для власника бота"
        screen = scr_models(chat_id)
    elif a == "cpl":
        screen = scr_custom_list()
    elif a == "cpnew":
        WIZ[chat_id] = {}
        ask_input(chat_id, "cpn_title")
        screen = ("🧩 <b>Нова модель · крок 1 з 4</b>\n\nЯк її назвати в боті? Наприклад: «DeepSeek».", [CANCEL_ROW])
    elif a == "cpurl":
        w = WIZ.setdefault(chat_id, {})
        if arg == "custom":
            ask_input(chat_id, "cpn_url")
            screen = ("🧩 <b>Крок 2 з 4</b>\n\nНадішліть адресу API, наприклад <code>https://api.example.com/v1</code>", [CANCEL_ROW])
        else:
            w["base_url"] = PRESETS[int(arg)][1]
            ask_input(chat_id, "cpn_key")
            screen = ("🧩 <b>Крок 3 з 4</b>\n\nНадішліть API-ключ. Я одразу видалю ваше повідомлення з ключем із чату.", [CANCEL_ROW])
    elif a == "cpmod":
        w = WIZ.get(chat_id) or {}
        models = w.get("models") or []
        if arg == "custom" or not models:
            ask_input(chat_id, "cpn_model")
            screen = ("🧩 <b>Крок 4 з 4</b>\n\nНадішліть точну назву моделі (як у документації сервісу).", [CANCEL_ROW])
        else:
            w["model"] = models[int(arg)]
            return await finish_wizard(update, ctx, chat_id)
    elif a == "cp":
        screen = scr_custom(arg)
    elif a == "cptest":
        if q:
            await q.answer("Перевіряю…")
        ok, info = await test_custom(custom_get(arg))
        toast = None
        await ctx.bot.send_message(chat_id, ("✅ Працює. Відповідь: " if ok else "⚠️ Не працює. ") + info)
        screen = scr_custom(arg)
    elif a == "cptog":
        r = custom_get(arg)
        r["enabled"] = 0 if r["enabled"] else 1
        custom_save(r)
        toast = "Увімкнено" if r["enabled"] else "Вимкнено"
        screen = scr_custom(arg)
    elif a in ("cpmodel", "cpkey", "cpname"):
        ask_input(chat_id, a, arg)
        prompt = {"cpmodel": "Нова назва моделі?", "cpkey": "Новий ключ? (повідомлення з ключем я видалю)", "cpname": "Нова назва в боті?"}[a]
        screen = (f"✏️ <b>{prompt}</b>", [CANCEL_ROW])
    elif a == "cpdel":
        r = custom_get(arg)
        screen = (f"🗑 <b>Видалити «{esc(r['title'])}»?</b>", [[("🗑 Так", f"m:cpdely:{arg}"), ("« Ні", f"m:cp:{arg}")]])
    elif a == "cpdely":
        custom_delete(arg)
        toast = "Модель видалено"
        screen = scr_custom_list()
    elif a in ("backup", "bk"):
        AWAIT.pop(chat_id, None)
        screen = scr_backups(chat_id)
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
            ask_input(chat_id, action, d)
            prompts.append(prompt)

        res = bkui.handle_bk_sched(BK, parts, ask_bk)
        if prompts:
            if q:
                await q.answer()
            return await ctx.bot.send_message(chat_id, prompts[0], parse_mode=ParseMode.HTML, reply_markup=kb([CANCEL_ROW]))
        if res:
            screen, toast = res
    elif a == "bkgo":
        if q:
            await q.answer("Готую копію…")
        await bk_run(ctx.bot, chat_id, only=[arg] if arg else None)
        return
    elif a == "bkt":
        screen = scr_backup_target(chat_id, arg)
    elif a == "bktog":
        BK.toggle(arg)
        screen = scr_backup_target(chat_id, arg)
    elif a == "bkdays":
        BK.set_days(int(arg))
        screen = scr_backups(chat_id)
    elif a == "bkdis":
        BK.disconnect(arg)
        toast = "Відключено"
        screen = scr_backup_target(chat_id, arg)
    elif a == "bkchx":
        BK.set_channel(None, None)
        screen = scr_backup_target(chat_id, "channel")
    elif a == "bkls":
        if q:
            await q.answer("Дивлюся в хмарі…")
        screen = await scr_backup_list(chat_id, arg)
    elif a == "bkrs":
        screen = scr_restore_help()
    elif a == "bkrc":
        t, n = arg, int(parts[3])
        if q:
            await q.answer("Завантажую копію…")
        try:
            if t == "channel":
                f = BK.cfg().get("channel_files", [])[n]
                blob = bytes(await (await ctx.bot.get_file(f["file_id"])).download_as_bytearray())
                where = f"канал · {f['name']}"
            else:
                fid = json.loads(kv_get(f"bkls:{chat_id}:{t}") or "[]")[n]
                blob = await BK.download(t, fid)
                where = f"{TARGET_TITLES[t][1]} · копія №{n + 1}"
        except Exception as e:  # noqa: BLE001
            return await ctx.bot.send_message(chat_id, f"⚠️ Не вдалося завантажити копію: {e}")
        return await ask_restore(ctx.bot, chat_id, blob, where)
    elif a == "bkyes":
        blob = PENDING_RESTORE.pop(chat_id, None)
        if not blob:
            screen, toast = scr_backups(chat_id), "Немає копії для відновлення"
        else:
            try:
                keep_cfg = kv_get("backup_cfg")  # підключення хмар лишаємо поточні, а не з копії
                safe, _ = BK.restore(blob)
                kv_set("backup_cfg", keep_cfg or "")
                refresh_providers()
                screen = (f"✅ <b>Базу відновлено.</b>\nПопередню збережено як <code>{esc(os.path.basename(safe))}</code>.", [])
            except BackupError as e:
                screen = (f"⚠️ Не вдалося: {esc(e)}", [])
    elif a == "bkno":
        PENDING_RESTORE.pop(chat_id, None)
        screen = scr_backups(chat_id)
    elif a == "mem":
        screen = scr_member(int(arg))
    elif a == "addmem":
        ask_input(chat_id, "addmem")
        screen = (
            "➕ <b>Кого додати?</b>\nНапишіть @username або числовий ID акаунта Telegram.\n\n"
            "Після цього людина має один раз відкрити бота (знайти в пошуку Telegram) — бот спитає її згоди.",
            [CANCEL_ROW],
        )
    elif a == "prex":
        preinvite_remove(data.split(":", 2)[2])
        toast = "Скасовано"
        screen = scr_access()
    elif a == "mview":
        uid = int(arg)
        on = not can_view(uid)
        with closing(db()) as con, con:
            con.execute("UPDATE members SET can_view=? WHERE user_id=?", (1 if on else 0, uid))
        try:
            await ctx.bot.send_message(
                uid,
                "ℹ️ Власник бота тепер може переглядати ваші розмови." if on else "ℹ️ Власник бота більше не переглядає ваші розмови.",
            )
        except Exception:  # noqa: BLE001
            pass
        toast = "Перегляд увімкнено, учасника повідомлено" if on else "Перегляд вимкнено"
        screen = scr_member(uid)
    elif a in ("mdl", "mdv", "mdc") and not can_view(int(arg) if a == "mdl" else (dialog_get(int(arg)) or {}).get("chat_id", 0)):
        toast = "Перегляд розмов цього учасника вимкнено"
        screen = scr_access()
    elif a == "mdl":
        screen = scr_member_dialogs(int(arg))
    elif a == "mdv":
        screen = scr_member_dialog(int(parts[2]), int(parts[3]))
    elif a == "mdc":
        new = copy_dialog_to(int(arg), chat_id)
        toast = "📥 Скопійовано й відкрито як поточну розмову"
        screen = scr_dialog(chat_id, new)
    else:
        screen = scr_home(chat_id)

    if q:
        try:
            await q.answer(toast or None)
        except BadRequest:
            pass
    if screen:
        # Єдине правило: навігація доступна на всіх екранах, у тому числі в акордеонному меню.
        await show(update, screen[0], screen[1], edit=edit, nav=True)


async def on_menu_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.callback_query.answer("Доступ закритий", show_alert=True)
    await route(update, ctx, update.callback_query.data)


async def handle_awaited(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text):
    chat_id = update.effective_chat.id
    act, data = AWAIT.get(chat_id, (None, None))
    if act in ("lren", "bgnew", "brl"):
        AWAIT.pop(chat_id, None)
        screen, tip = handle_editor_text(L, chat_id, act, data, text)
        await update.message.reply_text(tip, reply_markup=menu_kb(chat_id))
        return await show(update, *screen, edit=False)
    return await _handle_awaited(update, ctx, text)


async def _handle_awaited(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text):
    chat_id = update.effective_chat.id
    act, data = AWAIT.get(chat_id, (None, None))
    if act in ("bkd", "bkw"):
        times = re.findall(r"(\d{1,2})[:.](\d{2})", text)
        if not times:
            return await update.message.reply_text("Не зрозумів час. Приклад: 03:00")
        AWAIT.pop(chat_id, None)
        hm = f"{int(times[0][0]):02d}:{times[0][1]}"
        target = data if act == "bkd" else data[0]
        BK.set_sched(target, {"daily": hm} if act == "bkd" else {"weekly": [data[1], hm]})
        return await show(update, *bkui.scr_bk_sched(BK, target), edit=False)
    if act == "svcon":
        AWAIT.pop(chat_id, None)
        try:
            await update.message.delete()
        except Exception:  # noqa: BLE001
            pass
        try:
            await SVC.connect_token(data, text)
            await update.effective_chat.send_message(f"✅ {SERVICES[data]['title']} підключено." + ("" if SERVICES[data]["auth"] == "email" else " Повідомлення з токеном видалено з чату."))
        except (ServiceError, httpx.HTTPError) as e:
            await update.effective_chat.send_message(f"⚠️ Не вийшло: {e}")
        return await show(update, *scr_service(chat_id, data), edit=False)
    if act == "svadd":
        AWAIT.pop(chat_id, None)
        first, _, rest = text.partition("\n")
        due = None
        if "@" in first and SERVICES[data]["kinds"][0] == "task":
            first, _, due = first.rpartition("@")
        kind = SERVICES[data]["kinds"][0]
        try:
            url = await SVC.add(data, kind, first.strip(), rest.strip(), (due or "").strip() or None)
            return await reply_html(update, f"✅ Додано в {SERVICES[data]['title']}.", kb([[("🔗 Відкрити", "url:" + url)], [("📥 Показати", f"m:svls:{data}")]]))
        except ServiceError as e:
            return await reply_html(update, f"⚠️ {esc(str(e))}")

    """Користувач надіслав текст, на який бот чекав (назва, опис, час…)."""
    chat_id = update.effective_chat.id
    action, data = AWAIT.pop(chat_id)
    reply = update.message.reply_text
    if action in ("lkhub", "lkpub"):
        t = text.strip()
        if action == "lkhub":
            if t == "-":
                NODE.set("hub_url", None)
                NODE.set("hub_key", None)
                hub.configure(env("HUB_URL", ""), env("HUB_KEY", ""))
            else:
                parts_ = t.split()
                if len(parts_) != 2 or not parts_[0].startswith("http"):
                    ask_input(chat_id, "lkhub")
                    return await reply("Потрібно: адреса й ключ через пробіл.", reply_markup=kb([CANCEL_ROW]))
                NODE.set("hub_url", parts_[0])
                NODE.set("hub_key", parts_[1])
                hub.configure(parts_[0], parts_[1])
            NODE.set("plan_sent", None)
            ok_ = await send_plan_if_changed(force=True) if hub.ready else False
            await reply("✅ Головний бот: " + (hub.url or "не підключено") + (" · зв'язок є, план передано" if ok_ else (" · ⚠️ не відповідає" if hub.ready else "")))
        else:
            if t != "-" and not t.startswith("https://"):
                ask_input(chat_id, "lkpub")
                return await reply("Адреса має починатися з https://", reply_markup=kb([CANCEL_ROW]))
            NODE.set("public_url", None if t == "-" else t.rstrip("/"))
            await apply_receiving()
            await sync_wakes(force=True)
            await reply("✅ Моя адреса: " + (public_base() or "не задано"))
        return await show(update, *scr_diag(chat_id, "links"), edit=False)
    if action == "rwadd":
        try:
            w = NODE.wake_times() + parse_wake(text)
        except ValueError as e:
            ask_input(chat_id, "rwadd")
            return await reply(f"Не зрозумів «{e}». Приклади: 09:00 · 9:00, 18:30 · 08:00 пн,ср,пт · кожні 4 год", reply_markup=kb([CANCEL_ROW]))
        uniq = {(x["t"], tuple(x["d"])): x for x in w}
        NODE.set("wake", sorted(uniq.values(), key=lambda x: x["t"]))
        await sync_wakes()
        return await show(update, *scr_wake(chat_id), edit=False)
    if action == "qask":
        if data not in PROVIDERS:
            return await reply("⚠️ Обрана модель більше не підключена.")
        return await answer(update, ctx, text, text, data)
    if action == "dren":
        dialog_update(data, title=text[:60])
        t, r = scr_dialog(chat_id, data)
        return await reply_html(update, "✅ Перейменовано\n\n" + t, kb(r))
    if action == "fnew":
        fid = folder_create(chat_id, text[:40])
        t, r = scr_folder(chat_id, fid)
        return await reply_html(update, "✅ Теку створено\n\n" + t, kb(r))
    if action == "fren":
        folder_rename(data, text[:40])
        t, r = scr_folder(chat_id, data)
        return await reply_html(update, "✅ Перейменовано\n\n" + t, kb(r))
    if action == "mvnew":
        kind, item = data
        fid = folder_create(chat_id, text[:40])
        if kind == "d":
            dialog_update(item, folder_id=fid)
            t, r = scr_dialog(chat_id, item)
        else:
            task_update(item, folder_id=fid)
            t, r = scr_task(chat_id, item)
        return await reply_html(update, "✅ Теку створено й перенесено\n\n" + t, kb(r))
    if action == "tnew":
        tid = create_planned_task(chat_id, text, data.get("dialog"), data.get("folder"))
        t, r = scr_when(tid)
        return await reply_html(update, f"✅ Завдання #{tid} створено.\n\n" + t, kb(r))
    if action == "twhen":
        ts = parse_when(text)
        if not ts:
            ask_input(chat_id, "twhen", data)
            return await reply("Не зрозумів час. Приклади: 18:30, 25.12 10:00, через 40 хв")
        task_update(data, status="scheduled", run_at=ts)
        t, r = scr_task(chat_id, data)
        return await reply_html(update, f"✅ Заплановано на {hhmm(ts)}\n\n" + t, kb(r))
    if action == "vedit":
        vid, mid = data
        VOICE_TEXTS[vid] = text
        body, rows = transcript_view(vid)
        if mid:
            try:
                await ctx.bot.edit_message_text(body, chat_id=chat_id, message_id=mid, parse_mode=ParseMode.HTML, reply_markup=kb(rows))
                return await update.message.reply_text("✅ Розшифровку оновлено.")
            except BadRequest:
                pass
        return await update.message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=kb(rows))
    if action in ("cpn_key", "cpkey"):
        try:
            await update.message.delete()  # ключ не лишається в історії чату
        except Exception:  # noqa: BLE001
            pass
    if action == "cpn_title":
        WIZ.setdefault(chat_id, {})["title"] = text[:40]
        rows = pairs([(name, f"m:cpurl:{n}") for n, (name, _) in enumerate(PRESETS)])
        rows.append([("✍️ Інша адреса", "m:cpurl:custom")])
        rows.append(CANCEL_ROW)
        return await reply_html(update, "🧩 <b>Крок 2 з 4</b>\n\nОберіть сервіс або вкажіть свою адресу API (формат, сумісний з OpenAI):", kb(rows))
    if action == "cpn_url":
        if not text.startswith("http"):
            ask_input(chat_id, "cpn_url")
            return await reply("Адреса має починатися з https://")
        WIZ.setdefault(chat_id, {})["base_url"] = text.rstrip("/")
        ask_input(chat_id, "cpn_key")
        return await reply_html(update, "🧩 <b>Крок 3 з 4</b>\n\nНадішліть API-ключ. Я одразу видалю ваше повідомлення з ключем із чату.", kb([CANCEL_ROW]))
    if action == "cpn_key":
        w = WIZ.setdefault(chat_id, {})
        w["api_key"] = text
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        w["models"] = await fetch_models(w.get("base_url", ""), text)
        if w["models"]:
            rows = [[(m[:40], f"m:cpmod:{n}")] for n, m in enumerate(w["models"][:12])]
            rows.append([("✍️ Вписати назву", "m:cpmod:custom")])
            rows.append(CANCEL_ROW)
            return await reply_html(update, f"🔑 Ключ отримано й видалено з чату.\n\n🧩 <b>Крок 4 з 4</b> · сервіс має {len(w['models'])} моделей. Оберіть:", kb(rows))
        ask_input(chat_id, "cpn_model")
        return await reply_html(update, "🔑 Ключ отримано й видалено з чату.\n\n🧩 <b>Крок 4 з 4</b>\n\nНадішліть точну назву моделі.", kb([CANCEL_ROW]))
    if action == "cpn_model":
        WIZ.setdefault(chat_id, {})["model"] = text
        return await finish_wizard(update, ctx, chat_id)
    if action in ("cpmodel", "cpkey", "cpname"):
        r = custom_get(data)
        r[{"cpmodel": "model", "cpkey": "api_key", "cpname": "title"}[action]] = text if action != "cpname" else text[:40]
        custom_save(r)
        t, rows = scr_custom(data)
        return await reply_html(update, "✅ Збережено\n\n" + t, kb(rows))
    if action == "addmem":
        if not re.fullmatch(r"@?[A-Za-z0-9_]{4,32}|\d{5,15}", text):
            ask_input(chat_id, "addmem")
            return await reply("Потрібен @username (латиницею) або числовий ID. Спробуйте ще раз.")
        key = preinvite_add(text)
        link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "бот у пошуку Telegram"
        t, r = scr_access()
        return await reply_html(
            update,
            f"✅ {esc(key)} додано. Тепер цей акаунт має відкрити {esc(link)} і натиснути «Старт» — бот спитає згоди.\n\n" + t,
            kb(r),
        )
    if action == "nadd":
        nid = add_note_db(text, "", "telegram")
        t, r = scr_notes(chat_id)
        return await reply_html(update, f"✅ Нотатку #{nid} збережено\n\n" + t, kb(r))
    if action == "nfind":
        found = list_notes_db(limit=20, query=text)
        body = "\n".join(f"#{n['id']} ({n['date']}): {esc(n['text'])}" for n in found) or "Нічого не знайдено."
        return await reply_html(update, f"<b>🔍 {esc(text)}</b>\n\n{body}", kb([[("📝 Нотатки", "m:nt"), HOME]]))
    if action == "gen":
        if data == "image":
            return await do_image(ctx.bot, chat_id, text)
        if data == "music":
            return await do_music(ctx.bot, chat_id, text)
        if data == "video":
            return await do_video(ctx.bot, chat_id, text)
        if data == "canva_find":
            return await do_canva_find(ctx.bot, chat_id, text)
        if data == "canva":
            c = get_choice(chat_id)
            return await answer(update, ctx, "Зроби в Canva: " + text, "🖌 Canva: " + text, None if c == "auto" else c)


# ---------------------------------------------------------------- команди


async def on_consent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q, user = update.callback_query, update.effective_user
    key = preinvite_match(user)
    if not key:
        return await q.answer("Запрошення вже неактуальне", show_alert=True)
    preinvite_remove(key)
    who = esc(user.full_name) + (f" (@{esc(user.username)})" if user.username else "")
    await q.answer()
    if q.data == "cons:yes":
        member_upsert(user, "approved")
        member_set(user.id, "approved")
        await q.edit_message_text("✅ Дякую! Доступ відкрито.")
        await ctx.bot.send_message(user.id, "👋 Ласкаво просимо!", reply_markup=menu_kb(user.id))
        t, r = scr_home(user.id)
        await ctx.bot.send_message(user.id, t, parse_mode=ParseMode.HTML, reply_markup=kb(r))
        await ctx.bot.send_message(owner_id(), f"✅ {who} погодився(-лась) і тепер має доступ до бота.", parse_mode=ParseMode.HTML,
                                   reply_markup=kb([[("👥 Доступ", "m:acc")]]))
    else:
        await q.edit_message_text("Гаразд, запрошення відхилено.")
        await ctx.bot.send_message(owner_id(), f"❌ {who} відмовився(-лась) від запрошення.", parse_mode=ParseMode.HTML)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not allowed(update) and ctx.args and ctx.args[0].startswith("inv_"):
        if member_status(user.id) == "blocked":
            return await deny(update)
        code = ctx.args[0][4:]
        if member_status(user.id) != "pending":
            if not invite_use(code):
                return await update.message.reply_text("Це запрошення недійсне або вже використане. Попросіть у власника нове.")
            member_upsert(user, "pending", code)
            who = esc(user.full_name) + (f" (@{esc(user.username)})" if user.username else "")
            await ctx.bot.send_message(
                owner_id(),
                f"👥 <b>Новий запит на доступ</b>\n{who}\nID: <code>{user.id}</code>\n\nЛюдина прийшла за вашим запрошенням.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb([[("✅ Дозволити", f"m:accok:{user.id}"), ("❌ Відхилити", f"m:accno:{user.id}")]]),
            )
        return await update.message.reply_text(
            "👋 Вітаю! Запит на доступ надіслано власнику бота. Щойно він підтвердить, я напишу вам."
        )
    if not allowed(update):
        return await deny(update)
    chat_id = update.effective_chat.id
    current_dialog(chat_id)
    await update.message.reply_text(
        "👋 Привіт! Пишіть питання або надсилайте фото.",
        reply_markup=menu_kb(chat_id),
    )
    L.visit(chat_id, "m:home")
    await show(update, *scr_home(chat_id), edit=False, nav=True)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    chat_id = update.effective_chat.id
    rk = menu_kb(chat_id)
    if rk:
        await update.message.reply_text("Меню тепер у кнопках під повідомленням 👇", reply_markup=rk)
    L.visit(chat_id, "m:home")
    await show(update, *scr_home(chat_id), edit=False, nav=True)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш ID: {update.effective_user.id}\nID цього чату: {update.effective_chat.id}")


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    await route(update, ctx, "m:dnew", edit=False)


def screen_cmd(name):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not allowed(update):
            return await deny(update)
        AWAIT.pop(update.effective_chat.id, None)
        await route(update, ctx, f"m:{name}", edit=False)

    return handler


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    text = " ".join(ctx.args).strip()
    if not text:
        return await route(update, ctx, "m:nt", edit=False)
    await update.message.reply_text(f"Збережено, нотатка #{add_note_db(text, '', 'telegram')}")


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    q = " ".join(ctx.args).strip()
    if not q:
        return await update.message.reply_text("Напишіть, що шукати: /find квитки")
    found = list_notes_db(limit=20, query=q)
    await send_long(ctx.bot, update.effective_chat.id, "\n\n".join(f"#{n['id']} ({n['date']}): {n['text']}" for n in found) or "Нічого не знайдено.")


async def cmd_delnote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        return await update.message.reply_text("Вкажіть номер: /delnote 12 (номери видно в /notes)")
    ok = delete_note_db(int(ctx.args[0].lstrip("#")))
    await update.message.reply_text("Видалено." if ok else "Нотатки з таким номером немає.")


async def cmd_limits(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    if not PROVIDERS:
        return await update.message.reply_text("Не підключено жодної моделі. Як підключити: /help")
    pid = resolve_provider(" ".join(ctx.args)) if ctx.args else None
    t, r = scr_limits(pid)
    await reply_html(update, t, kb(r))


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    if ctx.args:
        pid = "auto" if ctx.args[0].lower() in ("auto", "авто") else resolve_provider(" ".join(ctx.args))
        if not pid:
            return await update.message.reply_text("Не знайшов такої моделі серед підключених. Список: 🤖 Моделі")
        set_choice(update.effective_chat.id, pid)
        return await update.message.reply_text(f"Тепер відповідає: {choice_label(pid)}")
    await route(update, ctx, "m:md", edit=False)


async def cmd_later(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/later текст — відкласти будь-яке завдання."""
    if not allowed(update):
        return await deny(update)
    text = " ".join(ctx.args).strip()
    if not text:
        return await route(update, ctx, "m:tnew", edit=False)
    tid = create_planned_task(update.effective_chat.id, text)
    t, r = scr_when(tid)
    await reply_html(update, f"✅ Завдання #{tid} створено.\n\n" + t, kb(r))


async def cmd_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    if not ctx.args:
        return await route(update, ctx, "m:gen:image", edit=False)
    await do_image(ctx.bot, update.effective_chat.id, " ".join(ctx.args))


async def cmd_music(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    if not ctx.args:
        return await route(update, ctx, "m:gen:music", edit=False)
    await do_music(ctx.bot, update.effective_chat.id, " ".join(ctx.args))


async def cmd_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        return await route(update, ctx, "m:gen:video", edit=False)
    await do_video(ctx.bot, update.effective_chat.id, text)


async def send_media(bot, chat_id, items):
    """Надсилає згенеровані файли: ("photo"|"audio"|"video", bytes, підпис) або ("url", посилання, підпис)."""
    for kind, data, cap in items or []:
        try:
            sent = None
            if kind == "photo":
                sent = await bot.send_photo(chat_id, InputFile(io.BytesIO(data), filename="image.jpg"), caption=cap[:1000])
            elif kind == "audio":
                name = "music.wav" if data[:4] == b"RIFF" else "music.mp3"
                sent = await bot.send_audio(chat_id, InputFile(io.BytesIO(data), filename=name), caption=cap[:1000])
            elif kind == "video":
                sent = await bot.send_video(chat_id, InputFile(io.BytesIO(data), filename="video.mp4"), caption=cap[:1000], supports_streaming=True)
            elif kind == "url":
                low = data.lower()
                if any(x in low for x in (".png", ".jpg", ".jpeg", ".webp", "thumbnail")):
                    sent = await bot.send_photo(chat_id, data, caption=cap)
                else:
                    sent = await bot.send_document(chat_id, data, caption=cap)
            media_save(chat_id, kind, sent, data, cap)
        except Exception as e:  # noqa: BLE001
            log.warning("не вдалося надіслати файл: %s", e)
            if kind == "url":
                await bot.send_message(chat_id, f"{cap}: {data}", link_preview_options=NO_PREVIEW)


def footer(pid, attempts, warnings):
    lines = [f"— {PROVIDERS[pid]['title']} · {PROVIDERS[pid]['model']}"]
    lines += [attempt_line(p, i) for p, i in attempts]
    lines += warnings
    if attempts:
        lines.append("Деталі: /limits")
    return "\n".join(lines)


async def answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE, content, history_text, preferred):
    """Відповідь моделі з індикатором роботи й чергою в межах чату."""
    chat_id = update.effective_chat.id
    did = current_dialog(chat_id)

    async def go():
        set_prio(dialog_score(did), f"Розмова «{short_q(dialog_name(did), 30)}»", chat=chat_id)
        others = busy_elsewhere(chat_id, did)
        title = "Думаю над відповіддю" + (f" · «{short_q(dialog_name(did), 30)}»" if others else "")
        async with Progress(ctx.bot, chat_id, title):
            return await _answer(update, ctx, content, history_text, preferred, did)

    return await with_chat_queue(update, history_text, go, did)


REFUSAL_RE = re.compile(
    r"^\W*(вибачте,? але я не можу|на жаль,? я не можу|я не можу (допомогти|виконати|надати|створити|написати)|"
    r"я не (буду|маю права)|не можу допомогти з цим|i'?m sorry,? but i (can'?t|cannot|won'?t)|i (can'?t|cannot|won'?t) (help|assist|do|provide|create|write)|"
    r"i'?m (not able|unable) to|извините,? но я не могу|я не могу (помочь|выполнить))", re.I)
CLARIFY_RE = re.compile(r"(уточніть|уточнити|уточнення|не зовсім зрозумів|що саме ви маєте на увазі|could you (please )?(clarify|specify)|"
                        r"можете (уточнити|розповісти|надати)|потрібно більше (деталей|інформації)|which one do you mean|уточните)", re.I)


def answer_outcome(text):
    """Чим закінчилося: done — готово; refused — модель відмовилась; needs_info — модель просить доповнити."""
    t = (text or "").strip()
    if REFUSAL_RE.search(t[:300]) and len(t) < 1500:
        return "refused"
    if len(t) < 900 and (CLARIFY_RE.search(t) or t.rstrip(" *_)»\"").endswith("?")):
        return "needs_info"
    return "done"


OUTCOME_HEAD = {
    "done": "✅ Готово",
    "refused": "🚫 Модель відмовилась це виконувати",
    "needs_info": "❓ Модель просить доповнити запит",
    "unavailable": "⚠️ Треба перемкнутися на іншу ШІ",
}


async def finish_note(bot, chat_id, did, outcome, jid=None, pid=None, q=None):
    """Сповіщення про завершення, коли користувач уже в іншій розмові (або там теж щось триває)."""
    rows = [[(f"💬 Перейти в «{short_q(dialog_name(did), 24)}»", f"m:dsw:{did}")]]
    hint = {
        "done": "Відповідь — вище, вона збережена в цій розмові.",
        "refused": f"{PROVIDERS.get(pid, {}).get('title', 'Модель')} відмовилась. Можна спробувати іншу ШІ — вона продовжить з того ж місця.",
        "needs_info": "Перейдіть у розмову й допишіть подробиці — модель продовжить.",
        "unavailable": "Жодна з обраних моделей зараз не змогла — варіанти вище.",
    }[outcome]
    if outcome == "refused" and jid:
        rows.insert(0, [("🔁 Іншою ШІ (Авто)", f"m:jrun:{jid}:auto"), ("📒 Обрати модель…", f"m:j:{jid}")])
    await bot.send_message(chat_id, f"🔔 <b>{OUTCOME_HEAD[outcome]}</b> у розмові «{esc(dialog_name(did))}»"
                           + (f"\nЗапит: «{esc(short_q(q, 80))}»" if q else "") + f"\n{hint}",
                           parse_mode=ParseMode.HTML, reply_markup=kb(rows))


async def _answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE, content, history_text, preferred, did=None):
    """preferred=None — «Авто» з перемиканням; інакше лише обрана модель, а якщо вона не може —
    бот пропонує відкласти завдання або продовжити на іншій моделі.
    did фіксується на старті: якщо поки модель думала, ви перейшли в іншу розмову, відповідь все одно ляже у свою."""
    chat_id = update.effective_chat.id
    did = did or current_dialog(chat_id)
    started = time.time()
    use_prefs(chat_id)
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    msg_id = save_message(chat_id, "user", history_text, dialog_id=did)
    try:
        text, pid, attempts, warnings = await ask_llm(
            load_history(chat_id, before_id=msg_id, dialog_id=did), content, preferred, fallback=preferred is None
        )
    except RuntimeError as e:
        set_msg(msg_id, in_context=0)
        return await update.effective_message.reply_text(str(e))
    except asyncio.CancelledError:  # «✖️ Зупинити»: запит не лишається в контексті — наступні відповіді його не враховують
        set_msg(msg_id, in_context=0)
        raise
    except Unavailable as u:
        set_msg(msg_id, pending=1)
        tid = task_create(chat_id, msg_id, content, history_text, preferred, reasons_text(u.attempts), dialog_id=did)
        journal_update(LAST_JOURNAL.get(), status="deferred", task_id=tid)
        away = current_dialog(chat_id) != did
        await offer_options(ctx.bot, task_get(tid), u.attempts,
                            intro=f"⏸ Розмова «{dialog_name(did)}»: зараз не вдалося відповісти" if away or busy_elsewhere(chat_id, did) else "⏸ Зараз не вдалося відповісти")
        if away:
            await finish_note(ctx.bot, chat_id, did, "unavailable", q=history_text)
        return None
    jid = LAST_JOURNAL.get()
    save_message(chat_id, "assistant", text, provider=pid, dialog_id=did)
    outcome = answer_outcome(text)
    if jid and outcome != "done":
        journal_update(jid, status=outcome)
    away = current_dialog(chat_id) != did  # поки модель думала, ви перейшли в іншу розмову
    parallel = bool(busy_elsewhere(chat_id, did))
    show_footer = prefs(chat_id)["footer"] or attempts or warnings
    head = f"💬 <b>{esc(dialog_name(did))}</b>\n" if (away or parallel) else ""
    body = text + (("\n\n" + footer(pid, attempts, warnings)) if show_footer else "")
    if head:
        body = f"💬 «{dialog_name(did)}» · {PROVIDERS.get(pid, {}).get('title', pid)}\n\n" + body
    if jid and any(w.startswith("✂️") for w in warnings):
        await send_long(ctx.bot, chat_id, body)
        await ctx.bot.send_message(chat_id, "✂️ Відповідь обірвалася.", reply_markup=kb([[("▶️ Продовжити · 🔁 Авто", f"m:jrun:{jid}:auto")], [("📒 Інша модель…", f"m:j:{jid}")]]))
    else:
        await send_long(ctx.bot, chat_id, body)
    await send_media(ctx.bot, chat_id, MEDIA.get())
    if away or (parallel and outcome != "done") or (time.time() - started > 60 and outcome == "done" and away):
        await finish_note(ctx.bot, chat_id, did, outcome, jid=jid, pid=pid, q=history_text)
    elif outcome == "refused" and jid:
        await ctx.bot.send_message(chat_id, "🚫 Модель відмовилась. Спробувати іншою ШІ? Вона побачить усю розмову й продовжить з цього місця.",
                                   reply_markup=kb([[("🔁 Іншою ШІ (Авто)", f"m:jrun:{jid}:auto"), ("📒 Обрати модель…", f"m:j:{jid}")]]))
    schedule_summary(did, chat_id)
    return text


async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    if len(ctx.args) < 2:
        return await update.message.reply_text("Приклад: /ask groq Як перекласти «дякую» японською?")
    pid = resolve_provider(ctx.args[0])
    if not pid:
        return await update.message.reply_text(f"Модель «{ctx.args[0]}» не підключена. Список: 🤖 Моделі")
    question = " ".join(ctx.args[1:])
    await answer(update, ctx, question, question, pid)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    msg = update.message
    chat_id = msg.chat_id
    routes = L.reply_routes(chat_id)
    pressed = L.unrelabel(chat_id, msg.text) if msg.text else None
    if pressed and pressed in routes:
        AWAIT.pop(chat_id, None)
        return await open_data(update, ctx, routes[pressed], edit=False)
    if AWAIT.get(chat_id, (None,))[0] == "bkch":
        origin = getattr(msg, "forward_origin", None)
        ch = getattr(origin, "chat", None)
        if ch is not None and ch.type == "channel":
            AWAIT.pop(chat_id, None)
            try:
                test = await ctx.bot.send_message(ch.id, "✅ Бот підключено до цього каналу для резервних копій.")
                BK.set_channel(ch.id, ch.title or str(ch.id))
                await ctx.bot.delete_message(ch.id, test.message_id)
            except Exception as e:  # noqa: BLE001
                return await msg.reply_text(f"⚠️ Не можу писати в канал «{ch.title}»: {e}\nДодайте бота в канал адміністратором і перешліть допис ще раз.")
            return await show(update, *scr_backup_target(chat_id, "channel"), edit=False)
    if not msg.text and not msg.photo:
        return
    if msg.text and CM.wants_text(chat_id):
        toast, scr = await CM.on_text(chat_id, msg.text)
        if toast:
            await msg.reply_text("✅ " + toast)
        return await show(update, *scr, edit=False)
    if msg.text and chat_id in AWAIT:
        return await handle_awaited(update, ctx, msg.text.strip())
    choice = get_choice(chat_id)
    preferred = None if choice == "auto" else choice
    if msg.photo:
        caption = msg.caption or "Що на цьому фото?"
        file = await msg.photo[-1].get_file()
        data = base64.b64encode(bytes(await file.download_as_bytearray())).decode()
        content = [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
        ]
        await answer(update, ctx, content, f"[фото] {caption}", preferred)
    else:
        await answer(update, ctx, msg.text, msg.text, preferred)


async def notify_chat(chat_id, text):
    if tg_app:
        await tg_app.bot.send_message(chat_id, text)


canva.notify = notify_chat

# Синя кнопка «Меню» Telegram не вміє показувати inline-кнопки напряму.
# Тому в ній лишаємо тільки вхід у кнопкове меню; всі інші дії доступні кнопками в чаті.
COMMANDS = [
    ("start", "Запустити бота"),
    ("menu", "Відкрити кнопкове меню"),
]

tg_app = None
if TELEGRAM_TOKEN:
    tg_app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(16).build()  # меню й «Зупинити» працюють, поки ШІ думає
    CM.install_tg(tg_app)  # 🏘 групи й канали — раніше за все інше

    async def _activity(update, ctx):
        note_activity()

    from telegram.ext import TypeHandler as _TH
    tg_app.add_handler(_TH(Update, _activity), group=-3)
    for name, fn in [
        ("start", cmd_start),
        ("menu", cmd_menu),
        ("help", cmd_help),
        ("id", cmd_id),
        ("new", cmd_new),
        ("notes", screen_cmd("nt")),
        ("note", cmd_note),
        ("find", cmd_find),
        ("delnote", cmd_delnote),
        ("limits", cmd_limits),
        ("model", cmd_model),
        ("ask", cmd_ask),
        ("tasks", screen_cmd("tl")),
        ("later", cmd_later),
        ("dialogs", screen_cmd("dl")),
        ("settings", screen_cmd("st")),
        ("create", screen_cmd("cr")),
        ("image", cmd_image),
        ("video", cmd_video),
        ("music", cmd_music),
        ("canva", screen_cmd("canva")),
        ("pinned", screen_cmd("pins")),
        ("archive", screen_cmd("box:archived")),
        ("trash", screen_cmd("box:trash")),
        ("access", screen_cmd("acc")),
        ("backup", screen_cmd("backup")),
    ]:
        tg_app.add_handler(CommandHandler(name, fn))
    tg_app.add_handler(CallbackQueryHandler(on_ui_button, pattern=r"^ui:"))
    tg_app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^m:"))
    tg_app.add_handler(CallbackQueryHandler(on_consent, pattern=r"^cons:"))
    tg_app.add_handler(CallbackQueryHandler(on_help_button, pattern=r"^help:"))
    tg_app.add_handler(CallbackQueryHandler(on_task_button, pattern=r"^task:"))
    tg_app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.VIDEO | filters.Document.AUDIO, on_voice)
    )
    tg_app.add_handler(MessageHandler(filters.Document.FileExtension("zip") | filters.Document.FileExtension("gz") | filters.Document.FileExtension("enc") | filters.Document.FileExtension("db"), on_backup_file))
    tg_app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO | (filters.FORWARDED & filters.ChatType.PRIVATE), on_message))


# ---------------------------------------------------------------- конектор MCP (зовнішній ШІ -> бот)

mcp = MCPServer(
    "Мій Telegram-бот",
    instructions=(
        "Конектор до особистого Telegram-бота користувача з кількома моделями ШІ. Через нього можна читати "
        "переписку з ботом, надсилати повідомлення в Telegram користувача або в канал, де бот є адміністратором, "
        "працювати зі спільними нотатками, ставити питання моделям бота і дивитись їхні ліміти. "
        "Перед надсиланням у канал перепитай користувача."
    ),
)


@mcp.tool()
def bot_status() -> dict:
    """Стан бота: підключені моделі, порядок «Авто», кількість повідомлень і нотаток."""
    with closing(db()) as con:
        m = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    return {
        "telegram_connected": tg_app is not None,
        "providers": {pid: {"title": p["title"], "model": p["model"], "free": p["free"]} for pid, p in PROVIDERS.items()},
        "not_connected": [pid for pid in CATALOG if pid not in PROVIDERS],
        "auto_order": AUTO_ORDER,
        "messages": m,
        "notes": n,
    }


@mcp.tool()
def limits_report() -> dict:
    """Використання й ліміти кожної моделі: запити сьогодні, залишки від сервісу, паузи, останні помилки."""
    out = {}
    for pid, p in PROVIDERS.items():
        st = get_state(pid)
        items, rl_ts = ratelimit_summary(pid)
        used, errs = usage(pid, "day")
        out[pid] = {
            "title": p["title"],
            "model": p["model"],
            "requests_today": used,
            "errors_today": errs,
            "requests_this_month": usage(pid, "month")[0],
            "known_free_limit": p["limit"],
            "service_reported": [{"name": n, "remaining": r, "limit": l, "reset": s} for n, r, l, s in items],
            "paused_until": hhmm(paused_until(pid)) if paused_until(pid) else None,
            "last_error": {"kind": st.get("err_kind"), "title": ERR.get(st.get("err_kind"), ("", ""))[1], "at": hhmm(st["err_ts"]), "advice": advice(pid, st["err_kind"])} if st.get("err_ts") else None,
            "last_ok": hhmm(st["last_ok"]) if st.get("last_ok") else None,
        }
    return out


@mcp.tool()
def recent_messages(limit: int = 20, chat_id: int | None = None) -> list[dict]:
    """Останні повідомлення переписки з ботом (від нових до старих). provider — яка модель відповіла."""
    limit = max(1, min(limit, 200))
    with closing(db()) as con:
        if chat_id is None:
            rows = con.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
    return [
        {
            "chat_id": r["chat_id"],
            "role": r["role"],
            "text": r["text"],
            "source": r["source"],
            "provider": r["provider"],
            "date": datetime.fromtimestamp(r["ts"], TZ).strftime("%Y-%m-%d %H:%M"),
        }
        for r in rows
    ]


@mcp.tool()
async def send_telegram_message(text: str, chat: str | None = None) -> str:
    """Надіслати повідомлення через бота. chat — ID чату або @канал (бот має бути адміном каналу).
    Якщо chat не вказано — повідомлення піде власнику бота."""
    if tg_app is None:
        return "Telegram не налаштований (немає TELEGRAM_TOKEN)."
    target = chat or (ALLOWED_USERS[0] if ALLOWED_USERS else None)
    if target is None:
        return "Не задано ALLOWED_USERS — нема кому надсилати."
    if isinstance(target, str) and target.lstrip("-").isdigit():
        target = int(target)
    await send_long(tg_app.bot, target, text)
    if isinstance(target, int):
        save_message(target, "assistant", text, source="connector")
    return f"Надіслано в {target}"


@mcp.tool()
async def ask_model(prompt: str, provider: str = "auto") -> dict:
    """Поставити питання одній з моделей бота (наприклад, щоб порівняти відповіді).
    provider — id з bot_status (gemini, groq, mistral…) або auto."""
    preferred = None if provider in ("", "auto") else resolve_provider(provider)
    if provider not in ("", "auto") and not preferred:
        return {"error": f"Модель {provider} не підключена", "available": list(PROVIDERS)}
    partner_seen("🔌 ШІ через конектор (MCP)", kind="ai")

    async def job():
        note = {}
        set_prio(RK.score("b", "🔌 ШІ через конектор (MCP)"), "Запит від ШІ через конектор", notify=lambda t: note.setdefault("events", []).append(t) or asyncio.sleep(0))
        try:
            text, pid, attempts, warnings = await ask_llm([], prompt, preferred, fallback=preferred is None)
        except RuntimeError as e:
            return {"error": str(e)}
        except Unavailable as u:
            return {"error": "Модель зараз не відповідає", "reasons": [{"provider": p, "reason": ERR[i["kind"]][1], "advice": advice(p, i["kind"], i.get("until"))} for p, i in u.attempts]}
        return {"provider": pid, "model": PROVIDERS[pid]["model"], "answer": text,
                "skipped": [{"provider": p, "reason": ERR[i["kind"]][1]} for p, i in attempts], "warnings": warnings,
                **({"queue": note["events"]} if note.get("events") else {})}

    return await queued(job, "Запит від ШІ через конектор", wait=40, dedupe=[prompt, provider])


HUB_PENDING = {}  # запити від головного бота, що ще в черзі (щоб повтор не створив дубль)
MCP_JOBS = {}  # id -> asyncio.Task: запити, що чекають у черзі довше, ніж готовий чекати той, хто питав


async def queued(job, label, wait=40, dedupe=None):
    """Виконує job; якщо через важливіші завдання це затягується — не відмовляє, а повертає номер у черзі."""
    key = hashlib.sha256((label + json.dumps(dedupe, ensure_ascii=False, default=str)).encode()).hexdigest()[:16] if dedupe is not None else None
    same = next((j for j, t in MCP_JOBS.items() if key and getattr(t, "_key", None) == key and not t.done()), None)
    if same:  # той самий запит прийшов знову — не дублюємо, а нагадуємо, що він уже в черзі
        return {"status": "queued", "job": same, **QUEUE_FLAGS,
                "message": f"⏸ Цей запит уже в черзі (job={same}) — повторювати не треба. {KEEP_WAITING} Результат: ask_model_result(job=\"{same}\")."}
    t = asyncio.create_task(job())
    t._key = key
    done, _ = await asyncio.wait({t}, timeout=wait)
    if done:
        return t.result()
    jid = secrets.token_hex(4)
    MCP_JOBS[jid] = t
    return {"status": "queued", "job": jid, **QUEUE_FLAGS, "position": len(WQ.waiting),
            "message": f"⏸ Бот зараз виконує пріоритетніші завдання ({WQ.busy_text()}). Запит не відхилено — він у черзі й буде виконаний. "
                       f"{KEEP_WAITING} Результат: інструмент ask_model_result(job=\"{jid}\") — викликайте його пізніше, хоч кілька разів."}


@mcp.tool()
async def ask_model_result(job: str) -> dict:
    """Результат запиту ask_model, який стояв у черзі (status=queued)."""
    t = MCP_JOBS.get(job)
    if not t:
        return {"error": "Немає такого запиту (або результат уже забрали)"}
    if not t.done():
        return {"status": "queued", **QUEUE_FLAGS, "message": f"Ще в черзі або виконується — {KEEP_WAITING} Зараз працюю над: {WQ.busy_text()}"}
    MCP_JOBS.pop(job, None)
    return t.result()


@mcp.tool()
def add_note(text: str, tags: str = "") -> str:
    """Додати нотатку в спільну базу (її бачить і бот у Telegram)."""
    return f"Збережено, id={add_note_db(text, tags, 'connector')}"


@mcp.tool()
def search_notes(query: str = "", limit: int = 20) -> list[dict]:
    """Знайти нотатки за словом або тегом. Порожній запит — останні нотатки."""
    return list_notes_db(limit=max(1, min(limit, 100)), query=query or None)


@mcp.tool()
def delete_note(note_id: int) -> str:
    """Видалити нотатку за id."""
    return "Видалено" if delete_note_db(note_id) else "Нотатку не знайдено"


@mcp.custom_route(f"/{MCP_SECRET}/iphone", methods=["GET", "POST"])
async def iphone_endpoint(request):
    """Для «Команд» на iPhone: GET — забрати нові нагадування й нотатки; POST {text} — зберегти нотатку в бот."""
    from starlette.responses import JSONResponse

    key = request.query_params.get("key", "")
    if request.method == "GET":
        items = SVC.iphone_take(key)
        if items is None:
            return JSONResponse({"ok": False, "error": "неправильний ключ"}, status_code=403)
        return JSONResponse({"ok": True, "count": len(items), "items": items})
    if not SVC.iphone_check(key):
        return JSONResponse({"ok": False, "error": "неправильний ключ"}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        body = dict(await request.form())
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"ok": False, "error": "порожній текст"}, status_code=400)
    nid = add_note_db(text[:8000], "iphone", "iphone", chat_id=owner_id())
    return JSONResponse({"ok": True, "note_id": nid})


@mcp.custom_route("/connect/{sid}/callback", methods=["GET"])
async def service_callback(request):
    from starlette.responses import HTMLResponse

    sid = request.path_params["sid"]
    if sid not in SERVICES:
        return HTMLResponse("Невідомий сервіс", status_code=404)
    ok, text = await SVC.callback(sid, request.query_params.get("code", ""), request.query_params.get("state", ""))
    if tg_app and ALLOWED_USERS:
        try:
            await tg_app.bot.send_message(owner_id(), ("✅ " if ok else "⚠️ ") + text)
        except Exception:  # noqa: BLE001
            pass
    return HTMLResponse(f"<meta name=viewport content='width=device-width'><h2 style='font-family:sans-serif'>{html.escape(text)}</h2>")


@mcp.custom_route("/backup/{cloud}/callback", methods=["GET"])
async def backup_callback(request):
    from starlette.responses import HTMLResponse

    cloud = request.path_params["cloud"]
    if cloud not in CLOUDS:
        return HTMLResponse("Невідома хмара", status_code=404)
    ok, text = await BK.callback(cloud, request.query_params.get("code", ""), request.query_params.get("state", ""))
    if tg_app and ALLOWED_USERS:
        try:
            await tg_app.bot.send_message(owner_id(), ("✅ " if ok else "⚠️ ") + text)
        except Exception:  # noqa: BLE001
            pass
    return HTMLResponse(f"<meta name=viewport content='width=device-width'><h2 style='font-family:sans-serif'>{html.escape(text)}</h2>")


@mcp.custom_route(f"/{MCP_SECRET}/cmd", methods=["POST"])
async def hub_command(request):
    """Команда від 🧭 головного бота: відповідаємо моделлю, відповідь повертаємо в головний бот."""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "потрібен JSON"}, status_code=400)
    if not hub.check(body):
        return JSONResponse({"ok": False, "error": "неправильний ключ"}, status_code=403)
    note_activity()
    if body.get("type") in ("plan", "ping"):  # план сну головного (лише коли змінився) або перевірка «ти живий?»
        if isinstance(body.get("plan"), dict):
            on_hub_plan(body["plan"])
            await sync_wakes(force=True)
        return JSONResponse({"ok": True, "reply": "pong" if body["type"] == "ping" else "план отримано", "plan": my_plan()})
    if body.get("type") == "schedule" and isinstance(body.get("schedule"), dict):  # графік перевірок від головного бота
        NODE.set("hub_sched", body["schedule"])
        await sync_wakes(force=True)
        return JSONResponse({"ok": True, "reply": "📅 Графік прийнято: " + schedule_text(body["schedule"])
                             + ("; прокидатимусь на перевірки" if NODE.mode(DEFAULT_MODE) == "timer" else "")})
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"ok": False, "error": "порожній текст"}, status_code=400)
    owner = owner_id()
    if text.lower() in ("/status", "статус", "/limits", "ліміти"):
        lines = [f"{PROVIDERS[p]['title']}: " + (f"пауза до {hhmm(paused_until(p))}" if paused_until(p) else "доступна") for p in AUTO_ORDER if p in PROVIDERS]
        return JSONResponse({"ok": True, "reply": "\n".join(lines) or "Моделі не підключені"})
    title = "🤝 Від інших ботів"
    who = str(body.get("from") or "🧭 Головний бот")
    partner_seen(who, body.get("from_link"), via="через головного бота" if body.get("from") else "головний бот")
    did = next((d["id"] for d in dialogs_list(owner) if d["title"] == title), None) or dialog_create(owner, title=title)
    prompt = f"Повідомлення від бота «{body.get('from', 'інший бот')}» ({body.get('type', 'info')}):\n{text}\n\nКоротко поясни, що це означає і що зробити."
    hint = max(-5, min(5, int(body.get("prio") or 0))) if str(body.get("prio") or "0").lstrip("-").isdigit() else 0

    async def job():
        CHAT.set(owner)
        use_prefs(owner)
        set_prio(RK.score("b", who) or hint, f"Запит від «{who}»", notify=lambda t: hub.info(f"[для «{who}»] {t}"))
        try:
            answer, pid, _, _ = await ask_llm(load_history(owner, dialog_id=did), prompt)
        except Unavailable as u:
            return "Усі моделі зараз недоступні: " + reasons_text(u.attempts)[:300]
        save_message(owner, "user", prompt, source="hub", dialog_id=did)
        save_message(owner, "assistant", answer, provider=pid, dialog_id=did)
        return answer[:3500]

    key = hashlib.sha256(f"{who}\n{text}".encode()).hexdigest()[:16]
    if key in HUB_PENDING and not HUB_PENDING[key].done():  # той самий запит повторно — не дублюємо
        return JSONResponse({"ok": True, **QUEUE_FLAGS, "reply": f"⏸ Цей запит уже в черзі — повторювати не треба. {KEEP_WAITING}"})
    t = asyncio.create_task(job())
    HUB_PENDING[key] = t
    done, _ = await asyncio.wait({t}, timeout=int(env("HUB_REPLY_WAIT", "25")))
    if done:
        HUB_PENDING.pop(key, None)
        return JSONResponse({"ok": True, "reply": t.result()})

    def later(task):
        HUB_PENDING.pop(key, None)
        if not task.cancelled() and task.exception() is None:
            asyncio.ensure_future(hub.report(f"✅ Відповідь для «{who}» (запит чекав у черзі):\n{task.result()}"))

    t.add_done_callback(later)
    return JSONResponse({"ok": True, **QUEUE_FLAGS, "position": len(WQ.waiting),
                         "reply": f"⏸ Прийнято. Зараз виконую пріоритетніші завдання ({WQ.busy_text()}) — запит не відхилено, "
                                  f"він у черзі. {KEEP_WAITING} Відповідь прийде окремим повідомленням, щойно виконаю."})


@mcp.custom_route("/canva/callback", methods=["GET"])
async def canva_callback(request):
    return await canva.callback(request)


@mcp.custom_route("/", methods=["GET"])
async def health(request):
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("Бот працює.")


# ---------------------------------------------------------------- запуск


async def main():
    init_db()
    if not MCP_SECRET or len(MCP_SECRET) < 16:
        raise SystemExit("Задайте MCP_SECRET (довгий випадковий рядок, від 16 символів).")
    if not ALLOWED_USERS:
        log.warning("ALLOWED_USERS порожній: бот нікому не відповідатиме, лише покаже ID.")
    log.info("Підключені моделі: %s", ", ".join(PROVIDERS) or "жодної")

    app = mcp.streamable_http_app(streamable_http_path=f"/{MCP_SECRET}/mcp", stateless_http=True, host="0.0.0.0")
    if tg_app and NODE.mode(DEFAULT_MODE) != "always" and not public_base():
        log.warning("Режим сну потребує адреси: задайте PUBLIC_URL або створіть домен у Railway (Settings → Networking → Generate Domain). Поки працюю як «завжди активний».")
    app.router.routes.extend(NODE.routes(deactivate_node))
    from starlette.responses import PlainTextResponse as _PT
    from starlette.routing import Route as _Route

    async def wake(request):  # будильник: прокинутись, виконати все, що назбиралося, і знову заснути
        note_activity()
        t = asyncio.create_task(worker_pass(force_cluster=True))
        BACKGROUND.add(t)
        t.add_done_callback(BACKGROUND.discard)
        return _PT("ok")

    app.router.routes.append(_Route(WAKE_PATH, wake, methods=["GET", "POST", "HEAD"]))
    if tg_app:
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def tg_webhook(request):
            if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != HOOK_SECRET:
                return PlainTextResponse("forbidden", status_code=403)
            try:
                upd = Update.de_json(await request.json(), tg_app.bot)
            except ValueError:
                return PlainTextResponse("bad", status_code=400)
            # відповідаємо Telegram одразу, а обробляємо у фоні — інакше довга відповідь ШІ спричинить повтори
            t = asyncio.create_task(tg_app.process_update(upd))
            BACKGROUND.add(t)
            t.add_done_callback(BACKGROUND.discard)
            return PlainTextResponse("ok")

        app.router.routes.append(Route(HOOK_PATH, tg_webhook, methods=["POST"]))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info"))

    if tg_app:
        await tg_app.initialize()
        try:
            await tg_app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception:  # noqa: BLE001
            log.warning("не вдалося встановити меню команд")
        global BOT_USERNAME
        BOT_USERNAME = tg_app.bot.username or ""
        await tg_app.start()
        if NODE.enabled:  # кластер: спершу з'ясовуємо, чи цей сервер має працювати
            try:
                await NODE.tick(activate_node, deactivate_node, notify_owner)
            except Exception:  # noqa: BLE001
                log.exception("перевірка кластера під час запуску")
            log.info("кластер: «%s» %s", NODE.name, "активний" if NODE.active else "у резерві")
        await apply_receiving()
        BACKGROUND.add(asyncio.create_task(task_worker()))
        log.info("Telegram-бот запущено")
        sw = storage_warning()
        if sw:
            log.warning(re.sub(r"<[^>]+>", "", sw))
            day_key = f"storage_warn:{time.strftime('%Y-%m-%d')}"
            if owner_id() and not kv_get(day_key):  # бот може часто прокидатися — нагадуємо не частіше разу на добу
                kv_set(day_key, "1")
                try:
                    await tg_app.bot.send_message(owner_id(), sw, parse_mode=ParseMode.HTML)
                except Exception:  # noqa: BLE001
                    pass
    else:
        log.warning("TELEGRAM_TOKEN не задано — працює лише конектор.")

    try:
        await server.serve()
    finally:
        if tg_app:
            if tg_app.updater and tg_app.updater.running:
                await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
