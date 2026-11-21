"""🤝 Спільні простори: кілька людей працюють разом віддалено — спільні нотатки, завдання, повідомлення всім і запитання до ШІ,
відповідь на які бачать усі. Налаштування — одна кнопка «➕ Створити» й посилання-запрошення.

Зберігаємо лише ID учасників (імена беремо з уже схвалених учасників бота), тексти нотаток/завдань — у самому просторі.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import closing


class Spaces:
    def __init__(self, path):
        self.path = str(path)
        with closing(self.db()) as con, con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS spaces (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, owner INTEGER NOT NULL,
                code TEXT UNIQUE NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS space_members (space INTEGER NOT NULL, user INTEGER NOT NULL, joined REAL NOT NULL,
                PRIMARY KEY(space, user));
            CREATE TABLE IF NOT EXISTS space_items (id INTEGER PRIMARY KEY AUTOINCREMENT, space INTEGER NOT NULL, kind TEXT NOT NULL,
                text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, author INTEGER NOT NULL, ts REAL NOT NULL);
            """)
            cols = [r[1] for r in con.execute("PRAGMA table_info(space_items)")]
            if "assignee" not in cols:  # 👤 кому доручено завдання
                con.execute("ALTER TABLE space_items ADD COLUMN assignee INTEGER")

    def assign(self, iid, uid):
        with closing(self.db()) as con, con:
            con.execute("UPDATE space_items SET assignee=? WHERE id=?", (int(uid) if uid else None, int(iid)))

    def db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def create(self, owner, name):
        with closing(self.db()) as con, con:
            sid = con.execute("INSERT INTO spaces(name, owner, code, created) VALUES (?,?,?,?)",
                              (name[:60], int(owner), secrets.token_urlsafe(6), time.time())).lastrowid
            con.execute("INSERT INTO space_members(space, user, joined) VALUES (?,?,?)", (sid, int(owner), time.time()))
        return sid

    def get(self, sid):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM spaces WHERE id=?", (int(sid),)).fetchone()
        return dict(r) if r else None

    def by_code(self, code):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM spaces WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None

    def mine(self, user):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute(
                "SELECT s.* FROM spaces s JOIN space_members m ON m.space=s.id WHERE m.user=? ORDER BY s.created", (int(user),))]

    def members(self, sid):
        with closing(self.db()) as con:
            return [r[0] for r in con.execute("SELECT user FROM space_members WHERE space=? ORDER BY joined", (int(sid),))]

    def is_member(self, sid, user):
        return int(user) in self.members(sid)

    def join(self, sid, user):
        with closing(self.db()) as con, con:
            con.execute("INSERT OR IGNORE INTO space_members(space, user, joined) VALUES (?,?,?)", (int(sid), int(user), time.time()))

    def leave(self, sid, user):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM space_members WHERE space=? AND user=?", (int(sid), int(user)))

    def delete(self, sid):
        with closing(self.db()) as con, con:
            for t, col in (("space_items", "space"), ("space_members", "space"), ("spaces", "id")):
                con.execute(f"DELETE FROM {t} WHERE {col}=?", (int(sid),))

    def rename(self, sid, name):
        with closing(self.db()) as con, con:
            con.execute("UPDATE spaces SET name=? WHERE id=?", (name[:60], int(sid)))

    def new_code(self, sid):
        code = secrets.token_urlsafe(6)
        with closing(self.db()) as con, con:
            con.execute("UPDATE spaces SET code=? WHERE id=?", (code, int(sid)))
        return code

    def add(self, sid, kind, text, author):
        with closing(self.db()) as con, con:
            return con.execute("INSERT INTO space_items(space, kind, text, author, ts) VALUES (?,?,?,?,?)",
                               (int(sid), kind, text[:2000], int(author), time.time())).lastrowid

    def items(self, sid, kind):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM space_items WHERE space=? AND kind=? ORDER BY done, id", (int(sid), kind))]

    def item(self, iid):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM space_items WHERE id=?", (int(iid),)).fetchone()
        return dict(r) if r else None

    def toggle(self, iid):
        with closing(self.db()) as con, con:
            con.execute("UPDATE space_items SET done=1-done WHERE id=?", (int(iid),))

    def remove(self, iid):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM space_items WHERE id=?", (int(iid),))
