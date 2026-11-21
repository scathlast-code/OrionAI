"""SQLite-сховище новинарні: матеріали, чернетки, джерела, налаштування."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    origin TEXT NOT NULL,            -- manual | forward | channel | rss | whatsapp | webhook
    source_name TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    raw_text TEXT NOT NULL,
    photo TEXT DEFAULT '',           -- base64 JPEG (лише поки матеріал не оброблено)
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | processing | done | duplicate | error | skipped
    dup_of INTEGER,
    related_to INTEGER,
    attempts INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    requested_by INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_fp ON items(fingerprint);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'news',     -- news | digest
    created REAL NOT NULL,
    updated REAL NOT NULL,
    headline TEXT DEFAULT '',
    body TEXT DEFAULT '',
    region TEXT DEFAULT '',
    category TEXT DEFAULT '',
    urgency TEXT DEFAULT 'normal',
    confirmed TEXT DEFAULT '[]',
    unconfirmed TEXT DEFAULT '[]',
    sources TEXT DEFAULT '[]',
    notes TEXT DEFAULT '',
    check_report TEXT DEFAULT '',
    target INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | length_mismatch | approved | rejected | published
    version INTEGER DEFAULT 1,
    model TEXT DEFAULT '',
    published_chat TEXT DEFAULT '',
    published_msg INTEGER DEFAULT 0,
    decided_by INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts(status);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    created REAL NOT NULL,
    headline TEXT, body TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- rss | tg
    ref TEXT NOT NULL UNIQUE,        -- URL стрічки або chat_id каналу
    title TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    last_poll REAL DEFAULT 0,
    last_error TEXT DEFAULT '',
    taken INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

DEFAULTS = {
    "target": 0,               # 0 — вільна довжина окремих новин
    "publish_chat": "",
    "publish_title": "",
    "publish_on_approve": False,
    "show_sources": True,
    "rules": "",               # власні правила редакції
    "auto_rss": True,
    "rss_every": 300,
    "rss_max_per_feed": 12,
    "notify": True,
    "signature": "",           # підпис під кожним дописом, напр. «🌿 GreenPost»
    "intake": [],              # чати, де ввімкнено режим прийому
    "account_new_on": True,    # нові підписки службового акаунта одразу увімкнені
    "hour_cap": 60,
    "spam_level": "soft",      # off | soft | strict
    "word_bilingual": False,   # велика стаття одразу у Word українською й російською
    "media_topics": "",        # тематика медіа для оцінки (порожньо — з тем кнопок)
    "forward_auto": True,      # переслане власником саме йде в новинарню            # не більше стількох матеріалів на годину йде в ШІ (решта чекає в черзі)
}


def normalize(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha1(normalize(text).encode()).hexdigest()


def shingles(text: str, k: int = 4) -> set:
    words = normalize(text).split()
    if len(words) <= k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.db()) as con, con:
            con.executescript(SCHEMA)
            cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
            if "hint" not in cols:
                con.execute("ALTER TABLE items ADD COLUMN hint TEXT DEFAULT ''")
            dcols = {r[1] for r in con.execute("PRAGMA table_info(drafts)")}
            if "folder" not in dcols:
                con.execute("ALTER TABLE drafts ADD COLUMN folder TEXT DEFAULT ''")

    def db(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    # ----------------------------------------------------------- налаштування
    def get(self, k):
        with closing(self.db()) as con:
            row = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else DEFAULTS.get(k)

    def set(self, k, v):
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, json.dumps(v, ensure_ascii=False)))

    # ----------------------------------------------------------- матеріали
    def add_item(self, origin, text, source_name="", source_url="", photo="", requested_by=0, window_h=72, hint=""):
        """Повертає (item_id, стан, id_оригіналу). Точні повтори й дуже схожі тексти позначаються дублікатами."""
        fp = fingerprint(text + (source_url if not text.strip() else ""))
        now = time.time()
        with closing(self.db()) as con, con:
            same = con.execute("SELECT id FROM items WHERE fingerprint=? AND created>? ORDER BY id LIMIT 1",
                               (fp, now - window_h * 3600)).fetchone()
            dup_of, related, status = None, None, "queued"
            if same and text.strip():
                dup_of, status = same["id"], "duplicate"
            elif text.strip():
                best, best_id = 0.0, None
                for row in con.execute("SELECT id, raw_text FROM items WHERE created>? AND status!='duplicate' ORDER BY id DESC LIMIT 400",
                                       (now - window_h * 3600,)):
                    s = similarity(text, row["raw_text"])
                    if s > best:
                        best, best_id = s, row["id"]
                if best >= 0.6:
                    dup_of, status = best_id, "duplicate"
                elif best >= 0.3:
                    related = best_id
            cur = con.execute(
                "INSERT INTO items(created,origin,source_name,source_url,raw_text,photo,fingerprint,status,dup_of,related_to,requested_by,hint)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, origin, source_name[:200], source_url[:500], text[:20000], photo, fp, status, dup_of, related, requested_by, hint[:200]))
            return cur.lastrowid, status, dup_of

    def item(self, iid):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        return dict(r) if r else None

    def set_item(self, iid, **kw):
        if not kw:
            return
        cols = ", ".join(f"{k}=?" for k in kw)
        with closing(self.db()) as con, con:
            con.execute(f"UPDATE items SET {cols} WHERE id=?", (*kw.values(), iid))

    def queued(self):
        with closing(self.db()) as con:
            return [r["id"] for r in con.execute("SELECT id FROM items WHERE status IN ('queued','processing') ORDER BY id")]

    def draft_for_item(self, iid):
        with closing(self.db()) as con:
            r = con.execute("SELECT id FROM drafts WHERE item_id=? ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
        return r["id"] if r else None

    # ----------------------------------------------------------- чернетки
    def add_draft(self, item_id, data: dict, kind="news", model="", target=0, status="draft"):
        now = time.time()
        with closing(self.db()) as con, con:
            cur = con.execute(
                "INSERT INTO drafts(item_id,kind,created,updated,headline,body,region,category,urgency,confirmed,unconfirmed,sources,notes,target,status,model)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, kind, now, now, data.get("headline", ""), data.get("body", ""), data.get("region", ""),
                 data.get("category", ""), data.get("urgency", "normal"),
                 json.dumps(data.get("confirmed") or [], ensure_ascii=False),
                 json.dumps(data.get("unconfirmed") or [], ensure_ascii=False),
                 json.dumps(data.get("sources") or [], ensure_ascii=False),
                 data.get("notes", ""), target, status, model))
            return cur.lastrowid

    def draft(self, did):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("confirmed", "unconfirmed", "sources"):
            try:
                d[k] = json.loads(d[k] or "[]")
            except ValueError:
                d[k] = []
        return d

    def update_draft(self, did, keep_version=False, note="", **kw):
        d = self.draft(did)
        if not d:
            return None
        if not keep_version and ("body" in kw or "headline" in kw):
            with closing(self.db()) as con, con:
                con.execute("INSERT INTO versions(draft_id,created,headline,body,note) VALUES(?,?,?,?,?)",
                            (did, time.time(), d["headline"], d["body"], note))
            kw["version"] = d["version"] + 1
        for k in ("confirmed", "unconfirmed", "sources"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k], ensure_ascii=False)
        kw["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in kw)
        with closing(self.db()) as con, con:
            con.execute(f"UPDATE drafts SET {cols} WHERE id=?", (*kw.values(), did))
        return self.draft(did)

    def versions(self, did):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM versions WHERE draft_id=? ORDER BY id DESC LIMIT 10", (did,))]

    def drafts(self, statuses=None, since=0, limit=10, offset=0, kind=None):
        q, args = "SELECT id FROM drafts WHERE created>=?", [since]
        if statuses:
            q += " AND status IN (%s)" % ",".join("?" * len(statuses))
            args += list(statuses)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with closing(self.db()) as con:
            ids = [r["id"] for r in con.execute(q, args)]
        return [self.draft(i) for i in ids]

    def count(self, table, where="1=1", args=()):
        with closing(self.db()) as con:
            return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]

    def stats(self):
        day = time.time() - 86400
        return {
            "queued": self.count("items", "status IN ('queued','processing')"),
            "errors": self.count("items", "status='error'"),
            "dups24": self.count("items", "status='duplicate' AND created>?", (day,)),
            "in24": self.count("items", "created>?", (day,)),
            "new": self.count("drafts", "status IN ('draft','length_mismatch')"),
            "approved": self.count("drafts", "status='approved'"),
            "published24": self.count("drafts", "status='published' AND updated>?", (day,)),
        }

    # ----------------------------------------------------------- джерела
    def sources(self, kind=None):
        with closing(self.db()) as con:
            if kind:
                rows = con.execute("SELECT * FROM sources WHERE kind=? ORDER BY id", (kind,))
            else:
                rows = con.execute("SELECT * FROM sources ORDER BY kind, id")
            return [dict(r) for r in rows]

    def source(self, sid):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def source_by_ref(self, ref):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM sources WHERE ref=?", (str(ref),)).fetchone()
        return dict(r) if r else None

    def add_source(self, kind, ref, title="", enabled=1):
        with closing(self.db()) as con, con:
            con.execute("INSERT OR IGNORE INTO sources(kind,ref,title,enabled) VALUES(?,?,?,?)", (kind, str(ref), title[:120], enabled))
        return self.source_by_ref(ref)

    def set_source(self, sid, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        with closing(self.db()) as con, con:
            con.execute(f"UPDATE sources SET {cols} WHERE id=?", (*kw.values(), sid))

    def del_source(self, sid):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM sources WHERE id=?", (sid,))

    def seen_once(self, key) -> bool:
        """True, якщо ключ новий (і запам'ятовує його)."""
        with closing(self.db()) as con, con:
            cur = con.execute("INSERT OR IGNORE INTO seen(key,at) VALUES(?,?)", (key[:500], time.time()))
            return cur.rowcount == 1

    def cleanup(self, days=30):
        cut = time.time() - days * 86400
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM seen WHERE at<?", (cut,))
            con.execute("UPDATE items SET photo='' WHERE status!='queued' AND photo!=''")
