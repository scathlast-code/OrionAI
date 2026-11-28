import asyncio
import sqlite3

import community as CMm


def make(tmp_path, ai):
    path = tmp_path / "c.db"

    def db():
        con = sqlite3.connect(path)
        con.row_factory = lambda cur, row: {d[0]: row[i] for i, d in enumerate(cur.description)}
        return con
    cm = CMm.Community(db, lambda: [1], ai=ai)
    cid = cm.register("tg", -100, "Обговорення", "supergroup")
    c = cm.chat(cid)
    c["cfg"]["rules"].update(spam=1, toxic=1)
    cm.save_cfg(cid, c["cfg"])
    return cm, cm.chat(cid)


def test_spam_by_markers_without_ai(tmp_path):
    cm, c = make(tmp_path, None)
    r = asyncio.run(cm.ai_violation(c, 5, "Заробіток 200$ в день, пишіть в лс!"))
    assert r == "spam"
    assert asyncio.run(cm.ai_violation(c, 5, "Дякую за новину, дуже корисно")) is None


def test_toxic_by_ai(tmp_path):
    async def ai(prompt):
        return '{"spam": false, "toxic": true, "why": "образа"}' if "дурень" in prompt else '{"spam": false, "toxic": false}'
    cm, c = make(tmp_path, ai)
    assert asyncio.run(cm.ai_violation(c, 5, "Автор статті дурень і нікчема")) == "toxic"
    assert asyncio.run(cm.ai_violation(c, 5, "Влада знову все провалила, ганьба")) is None


def test_looks_question():
    assert CMm.Community.looks_question("Коли відновлять світло в Ірпені?")
    assert not CMm.Community.looks_question("Дякую!")
