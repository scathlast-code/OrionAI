"""🌅 «На сьогодні»: особистий список дня, куди можна винести що завгодно — повідомлення з розмови, завдання,
нотатку, пункт простору. Не копія: кожен пункт пам'ятає, звідки він (kind + ref), і відкриває оригінал."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS agenda (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    ref INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL,
    UNIQUE(chat, day, kind, ref)
);
"""
KIND_ICON = {"m": "💬", "t": "⏰", "n": "📝", "s": "🤝", "a": "📌"}


class Agenda:
    def __init__(self, path):
        self.path = path
        with closing(self.db()) as con, con:
            con.executescript(SCHEMA)

    def db(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def add(self, chat, day, kind, ref, text):
        with closing(self.db()) as con, con:
            cur = con.execute("INSERT OR IGNORE INTO agenda(chat,day,kind,ref,text,added) VALUES(?,?,?,?,?,?)",
                              (int(chat), day, kind, int(ref or 0), (text or "")[:500], time.time()))
            return cur.lastrowid if cur.rowcount else None

    def items(self, chat, day):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM agenda WHERE chat=? AND day=? ORDER BY done, id", (int(chat), day))]

    def get(self, aid):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM agenda WHERE id=?", (int(aid),)).fetchone()
        return dict(r) if r else None

    def toggle(self, aid):
        with closing(self.db()) as con, con:
            con.execute("UPDATE agenda SET done=1-done WHERE id=?", (int(aid),))

    def remove(self, aid):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM agenda WHERE id=?", (int(aid),))

    def carry_over(self, chat, today, yesterday):
        """Невиконане вчора — переноситься на сьогодні (щоб нічого не губилось)."""
        with closing(self.db()) as con, con:
            con.execute("UPDATE OR IGNORE agenda SET day=? WHERE chat=? AND day=? AND done=0", (today, int(chat), yesterday))
