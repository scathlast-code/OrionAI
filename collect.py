"""📥 Режим збору: людина надсилає або пересилає тексти без команд — бот мовчки складає їх (відповідає лише «ок»),
а потім пише з них новину або віддає все зібране повідомленнями, .txt чи Word.

Зберігаємо лише текст і час (без імен авторів пересланих повідомлень), окремо для кожної людини.
"""
from __future__ import annotations

import io
import sqlite3
import time
from contextlib import closing


class Collect:
    def __init__(self, path):
        self.path = str(path)
        with closing(self.db()) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS collected (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, "
                        "ts REAL NOT NULL, text TEXT NOT NULL, fwd INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE INDEX IF NOT EXISTS collected_chat ON collected(chat, id)")
            con.execute("CREATE TABLE IF NOT EXISTS summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, ts REAL NOT NULL, "
                        "text TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0, first_id INTEGER, last_id INTEGER, "
                        "delivered INTEGER NOT NULL DEFAULT 0)")
            con.execute("CREATE TABLE IF NOT EXISTS col_state (chat INTEGER PRIMARY KEY, done_id INTEGER NOT NULL DEFAULT 0, "
                        "warned INTEGER NOT NULL DEFAULT 0)")
            # окремі зведення за темою («зведення про погоду») беруть лише свої репости — решта чекає свого зведення
            con.execute("CREATE TABLE IF NOT EXISTS col_used (chat INTEGER NOT NULL, item INTEGER NOT NULL, PRIMARY KEY(chat, item))")

    def db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def add(self, chat, text, fwd=False):
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO collected(chat, ts, text, fwd) VALUES (?,?,?,?)", (int(chat), time.time(), text[:20000], int(bool(fwd))))
            return con.execute("SELECT COUNT(*) FROM collected WHERE chat=?", (int(chat),)).fetchone()[0]

    def items(self, chat):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM collected WHERE chat=? ORDER BY id", (int(chat),))]

    def count(self, chat):
        with closing(self.db()) as con:
            return con.execute("SELECT COUNT(*) FROM collected WHERE chat=?", (int(chat),)).fetchone()[0]

    def pop_last(self, chat):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM collected WHERE id=(SELECT MAX(id) FROM collected WHERE chat=?)", (int(chat),))

    def clear(self, chat):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM collected WHERE chat=?", (int(chat),))

    def joined(self, chat, sep="\n\n———\n\n"):
        return sep.join(i["text"] for i in self.items(chat))

    def txt(self, chat):
        return self.joined(chat).encode("utf-8")

    def docx(self, chat, title="Зібрані тексти"):
        from docx import Document
        doc = Document()
        doc.add_heading(title, level=1)
        for n, i in enumerate(self.items(chat), 1):
            doc.add_paragraph(f"{n}. " + time.strftime("%d.%m %H:%M", time.localtime(i["ts"])) + (" · переслано" if i["fwd"] else "")).runs[0].italic = True
            for para in i["text"].split("\n"):
                doc.add_paragraph(para)
        bio = io.BytesIO()
        doc.save(bio)
        return bio.getvalue()


LIMIT = 20000      # знаків у «пам'яті» зведення: від першого непідсумованого репосту
WARN_AT = 16000    # попередити заздалегідь


class Batches:
    """Межі зведень: що вже увійшло в зведення (done_id), що ще ні; самостійні (авто) зведення."""

    def __init__(self, c: Collect):
        self.c = c

    def _state(self, chat):
        with closing(self.c.db()) as con, con:
            con.execute("INSERT OR IGNORE INTO col_state(chat) VALUES (?)", (int(chat),))
            return dict(con.execute("SELECT * FROM col_state WHERE chat=?", (int(chat),)).fetchone())

    def pending(self, chat):
        done = self._state(chat)["done_id"]
        with closing(self.c.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM collected WHERE chat=? AND id>? AND id NOT IN "
                                                 "(SELECT item FROM col_used WHERE chat=?) ORDER BY id", (int(chat), done, int(chat)))]

    def pending_chars(self, chat):
        return sum(len(i["text"]) for i in self.pending(chat))

    def take(self, chat, limit=LIMIT, only=None):
        """Непідсумовані повідомлення від найдавнішого, доки вміщається в limit (хоча б одне). only(item) — фільтр (тема)."""
        out, used = [], 0
        for i in self.pending(chat):
            if only and not only(i):
                continue
            if out and used + len(i["text"]) > limit:
                break
            out.append(i)
            used += len(i["text"])
        return out

    def warned(self, chat):
        return bool(self._state(chat)["warned"])

    def set_warned(self, chat, v=True):
        self._state(chat)
        with closing(self.c.db()) as con, con:
            con.execute("UPDATE col_state SET warned=? WHERE chat=?", (int(v), int(chat)))

    def mark(self, chat, items, text, auto):
        """Зведення готове: зберегти його й зсунути межу на останнє повідомлення, що в нього увійшло."""
        self._state(chat)
        with closing(self.c.db()) as con, con:
            con.executemany("INSERT OR IGNORE INTO col_used(chat, item) VALUES (?,?)", [(int(chat), i["id"]) for i in items])
            left = con.execute("SELECT MIN(id) FROM collected WHERE chat=? AND id NOT IN (SELECT item FROM col_used WHERE chat=?) AND id>"
                               "(SELECT done_id FROM col_state WHERE chat=?)", (int(chat), int(chat), int(chat))).fetchone()[0]
            top = (left - 1) if left else con.execute("SELECT COALESCE(MAX(id), 0) FROM collected WHERE chat=?", (int(chat),)).fetchone()[0]
            con.execute("UPDATE col_state SET done_id=MAX(done_id, ?), warned=0 WHERE chat=?", (top, int(chat)))
            con.execute("DELETE FROM col_used WHERE chat=? AND item<=?", (int(chat), top))
            return con.execute("INSERT INTO summaries(chat, ts, text, auto, first_id, last_id) VALUES (?,?,?,?,?,?)",
                               (int(chat), time.time(), text, int(bool(auto)), items[0]["id"], items[-1]["id"])).lastrowid

    def autos_since_manual(self, chat):
        """Самостійні зведення після останнього зведення на вимогу людини (їх треба додати до нового)."""
        with closing(self.c.db()) as con:
            last = con.execute("SELECT MAX(id) FROM summaries WHERE chat=? AND auto=0", (int(chat),)).fetchone()[0] or 0
            return [dict(r) for r in con.execute("SELECT * FROM summaries WHERE chat=? AND auto=1 AND id>? ORDER BY id", (int(chat), last))]

    def reset(self, chat):
        with closing(self.c.db()) as con, con:
            con.execute("DELETE FROM col_state WHERE chat=?", (int(chat),))
            con.execute("DELETE FROM summaries WHERE chat=?", (int(chat),))
            con.execute("DELETE FROM col_used WHERE chat=?", (int(chat),))


def docx_of(title, sections):
    """sections: [(заголовок, текст)] → Word-файл."""
    from docx import Document
    doc = Document()
    doc.add_heading(title, level=1)
    for head, body in sections:
        doc.add_heading(head, level=2)
        for para in body.split("\n"):
            doc.add_paragraph(para)
    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


TOPIC_PROMPT = ("Ось матеріали на одну тему — «{topic}», які користувач переслав у бот, від давніших до новіших. "
                "Зроби з них зведення саме на цю тему українською: короткий підсумок, далі події й факти з датами, місцями й цифрами. "
                "Лише факти з матеріалів, нічого не вигадуй; повтори обʼєднай; суперечності між джерелами познач. "
                "Сторонні теми, що випадково трапились у матеріалах, не додавай. Без технічних приписок каналів (підписки, адреси, «написати адміну»). "
                "Посилання на першоджерела збережи. Без markdown-зірочок.\n\nМАТЕРІАЛИ:\n")

SUMMARY_PROMPT = ("Ось матеріали, які користувач переслав у бот (репости з каналів і чатів), від давніших до новіших. "
                  "Зроби з них одне зведення українською: короткий загальний підсумок, далі головні події й факти групами за темами, "
                  "з датами, місцями й цифрами, якщо вони є. Лише факти з матеріалів, нічого не вигадуй; суперечності між джерелами познач. "
                  "Посилання з матеріалів збережи. Без markdown-зірочок.\n\nМАТЕРІАЛИ:\n")

CMD_RE = __import__("re").compile(r"(?i)^\s*(?:будь ласка,?\s*)?(готуй|підготуй|зроби|напиши|склади|давай|роби)\s+(зведення|звіт|підсумок|новину|зведення)\b")


def chunks(text, size=3900):
    """Довгий текст → частини для окремих повідомлень, по можливості на межі абзаців."""
    out = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


NEWS_PROMPT = ("Ось матеріали, які зібрав користувач (повідомлення й переслані дописи). Напиши з них одну цілісну новину українською: "
               "заголовок, лід і основний текст. Використовуй лише факти з матеріалів, нічого не вигадуй; суперечності познач. "
               "Посилання з матеріалів збережи як є. Без markdown-зірочок.\n\nМАТЕРІАЛИ:\n")
