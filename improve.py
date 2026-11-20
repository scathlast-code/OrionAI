"""💡 «Покращити»: позначки на будь-якому екрані, кнопці, тексті чи функції бота — збір помилок і недоробок.

Позначка зберігає місце (розділ/екран, підпис кнопки й дію під нею, уривок тексту), хто і коли, необов'язкове
пояснення. Вивантаження — Markdown і CSV (Excel). База — data/improve.db (входить у резервні копії).
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl

STATUS = {"new": "🆕 нове", "done": "✅ зроблено", "no": "❌ відхилено"}


def path():
    base = Path(os.path.dirname(os.getenv("DB_PATH", "data/bot.db")) or ".")
    base.mkdir(parents=True, exist_ok=True)
    return base / "improve.db"


def db():
    con = sqlite3.connect(path(), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS marks (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, user INTEGER NOT NULL DEFAULT 0,
        bot TEXT NOT NULL DEFAULT 'orion', where_ TEXT NOT NULL DEFAULT '', screen TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '', excerpt TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new')""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(marks)")}
    if "image" not in cols:  # 🐞 скриншот помилки (шлях до файлу поруч із базою)
        con.execute("ALTER TABLE marks ADD COLUMN image TEXT NOT NULL DEFAULT ''")
    return con


def img_dir():
    d = path().parent / "improve_img"
    d.mkdir(parents=True, exist_ok=True)
    return d


MEDIA_EXT = ("jpg", "png", "webp", "ogg", "mp3", "m4a", "wav", "mp4", "mov", "webm")


def set_image(mid, data: bytes, ext="jpg"):
    """Зберегти скриншот, голосове чи коротке відео до позначки (до 20 МБ)."""
    if not data or len(data) > 20 * 1024 * 1024:
        return ""
    f = img_dir() / f"{int(mid)}.{ext if ext in MEDIA_EXT else 'jpg'}"
    f.write_bytes(data)
    with closing(db()) as con, con:
        con.execute("UPDATE marks SET image=? WHERE id=?", (f.name, int(mid)))
    return f.name


def image_bytes(mid):
    with closing(db()) as con:
        r = con.execute("SELECT image FROM marks WHERE id=?", (int(mid),)).fetchone()
    if not r or not r["image"]:
        return None
    f = img_dir() / r["image"]
    return f.read_bytes() if f.exists() else None


def media_name(mid):
    with closing(db()) as con:
        r = con.execute("SELECT image FROM marks WHERE id=?", (int(mid),)).fetchone()
    return (r["image"] if r else "") or ""


def add_note(mid, extra):
    """Дописати до позначки (напр. розшифровку голосового чи відео)."""
    with closing(db()) as con, con:
        con.execute("UPDATE marks SET note=TRIM(note || char(10) || ?) WHERE id=?", (extra[:4000], int(mid)))


def add(user, where_, screen="", target="", action="", excerpt="", note="", bot="orion"):
    with closing(db()) as con, con:
        cur = con.execute("INSERT INTO marks(at,user,bot,where_,screen,target,action,excerpt,note) VALUES(?,?,?,?,?,?,?,?,?)",
                          (time.time(), int(user or 0), bot[:20], where_[:20], screen[:200], target[:200], action[:120], excerpt[:600], note[:2000]))
        return cur.lastrowid


def set_note(mid, note):
    with closing(db()) as con, con:
        con.execute("UPDATE marks SET note=? WHERE id=?", (note[:2000], int(mid)))


def set_status(mid, status):
    with closing(db()) as con, con:
        if status == "del":
            con.execute("DELETE FROM marks WHERE id=?", (int(mid),))
        else:
            con.execute("UPDATE marks SET status=? WHERE id=?", (status, int(mid)))


def items(status=None, limit=10, offset=0):
    with closing(db()) as con:
        q = "SELECT * FROM marks" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT ? OFFSET ?"
        return [dict(r) for r in con.execute(q, ((status,) if status else ()) + (limit, offset))]


def counts():
    with closing(db()) as con:
        return {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM marks GROUP BY status")}


def clear_done():
    with closing(db()) as con, con:
        return con.execute("DELETE FROM marks WHERE status IN ('done','no')").rowcount


def describe(m):
    where = {"chat": "чат", "app": "застосунок", "sandbox": "пісочниця", "reply": "відповідь", "cmd": "команда"}.get(m["where_"], m["where_"])
    parts = [f"#{m['id']} {STATUS.get(m['status'], m['status'])} · {time.strftime('%d.%m %H:%M', time.localtime(m['at']))} · {where}"]
    if m["screen"]:
        parts.append(f"екран: {m['screen']}")
    if m["target"]:
        parts.append(f"що: {m['target']}" + (f" ({m['action']})" if m["action"] else ""))
    if m["excerpt"]:
        parts.append(f"текст: «{m['excerpt'][:160]}»")
    if m["note"]:
        parts.append(f"💬 {m['note']}")
    img = dict(m).get("image") or ""
    if img:
        ext = img.rsplit(".", 1)[-1]
        parts.append("🎙 є голосове" if ext in ("ogg", "mp3", "m4a", "wav") else "🎬 є відео" if ext in ("mp4", "mov", "webm") else "🖼 є скриншот")
    return "\n".join(parts)


def export_md():
    rows = items(limit=100000)
    out = ["# 💡 Покращити — OrionAI", f"Вивантажено {time.strftime('%d.%m.%Y %H:%M')} · усього {len(rows)}", "",
           "Для будь-якого ШІ-редактора бота: це помилки (🐞) й побажання (💡), позначені користувачами саме там, де вони їх побачили. "
           "«де» — екран бота чи застосунку, «уривок» — текст того екрана, «пояснення» — опис і розшифровка голосового/відео. "
           "Після виправлення позначте пункт як зроблений.", ""]
    for st, title in (("new", "Нове"), ("done", "Зроблено"), ("no", "Відхилено")):
        part = [m for m in rows if m["status"] == st]
        if not part:
            continue
        out += [f"## {title} ({len(part)})", ""]
        for m in part:
            out.append(f"- **#{m['id']}** {time.strftime('%d.%m.%Y %H:%M', time.localtime(m['at']))} · {m['where_']} · {m['screen'] or '—'}")
            if m["target"]:
                out.append(f"  - що: {m['target']}" + (f" (`{m['action']}`)" if m["action"] else ""))
            if m["excerpt"]:
                out.append(f"  - текст: {m['excerpt'][:300]}")
            if m["note"]:
                out.append(f"  - пояснення: {m['note']}")
            if m.get("image"):
                out.append(f"  - файл (скриншот/голосове/відео): improve_img/{m['image']}")
        out.append("")
    return "\n".join(out).encode("utf-8")


def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "дата", "статус", "де", "бот", "екран", "що", "дія", "текст", "пояснення"])
    for m in items(limit=100000):
        w.writerow([m["id"], time.strftime("%d.%m.%Y %H:%M", time.localtime(m["at"])), STATUS.get(m["status"], m["status"]), m["where_"],
                    m["bot"], m["screen"], m["target"], m["action"], m["excerpt"], m["note"]])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def check_webapp(init_data: str, token: str, max_age=86400):
    """Перевірка підпису Telegram WebApp initData. -> user_id або None."""
    if not init_data or not token:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    if time.time() - int(pairs.get("auth_date", "0") or 0) > max_age:
        return None
    try:
        return int(json.loads(pairs.get("user", "{}")).get("id"))
    except (ValueError, TypeError):
        return None
