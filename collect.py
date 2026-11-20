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
