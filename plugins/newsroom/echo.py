"""📣 Відгомін наших новин: репости, пересилання, цитування, перекази, згадки й плагіат.

Головне правило редакції: переписаний текст — НЕ плагіат. Плагіатом вважається лише повністю або майже
ідентичний текст (≥ 85 % однакових 4-слівних фрагментів і близька довжина) без посилання на нас.
Переказ своїми словами — це «✍️ переказ» (з посиланням або без), а не плагіат.
"""
from __future__ import annotations

import re
import time

from .store import normalize, shingles

KINDS = {
    "repost": ("🔁", "Репост / пересилання"),
    "copy": ("📄", "Передрук із посиланням"),
    "plagiarism": ("🚩", "Плагіат (майже дослівно, без посилання)"),
    "quote": ("💬", "Цитування"),
    "retell": ("✍️", "Переказ"),
    "mention": ("🏷", "Згадка"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS echoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, ours_id INTEGER, source TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', excerpt TEXT NOT NULL DEFAULT '', score REAL NOT NULL DEFAULT 0, attributed INTEGER NOT NULL DEFAULT 0,
    seen INTEGER NOT NULL DEFAULT 0, key TEXT UNIQUE
);
"""


def longest_common_run(a: str, b: str, min_words=12) -> int:
    """Довжина найдовшого спільного відрізка слів (для цитат). 0 — якщо коротше за min_words."""
    wa, wb = normalize(a).split(), normalize(b).split()
    if len(wa) < min_words or len(wb) < min_words:
        return 0
    grams = {" ".join(wb[i:i + min_words]) for i in range(len(wb) - min_words + 1)}
    best, run = 0, 0
    for i in range(len(wa) - min_words + 1):
        if " ".join(wa[i:i + min_words]) in grams:
            run = run + 1 if run else min_words
            best = max(best, run)
        else:
            run = 0
    return best


def overlap(new: str, ours: str):
    sa, sb = shingles(new), shingles(ours)
    if not sa or not sb:
        return 0.0, 0.0
    inter = len(sa & sb)
    return inter / len(sa | sb), inter / len(sb)      # (схожість, яку частину нашого тексту містить новий)


STOP = set("який яка яке які цього цієї також через після перед понад близько можуть можна потрібно україни україна області".split())


def stems(text: str) -> set:
    """Грубі «основи» слів (перші 5 літер) і числа — щоб упізнати переказ тієї самої новини іншими словами."""
    out = set()
    for w in normalize(text).split():
        if w.isdigit() and len(w) >= 2:
            out.add("#" + w)
        elif len(w) >= 5 and w not in STOP:
            out.add(w[:5])
    return out


def stem_overlap(a: str, b: str) -> float:
    sa, sb = stems(a), stems(b)
    shared = len(sa & sb)
    if shared < 5:  # кілька спільних слів («висадили 800 дерев») — ще не та сама новина
        return 0.0
    return shared / min(len(sa), len(sb))


def attributed(text: str, url: str, brands: list[str]) -> bool:
    low = (text + " " + url).lower()
    return any(b and b.lower() in low for b in brands)


def classify(new_text: str, ours_text: str, brands: list[str], fwd_ours=False, url="") -> tuple[str | None, float]:
    """-> (вид, оцінка) або (None, 0). Детерміновано, без ШІ — щоб не плутати рерайт із плагіатом."""
    if fwd_ours:
        return "repost", 1.0
    sim, cont = overlap(new_text, ours_text)
    att = attributed(new_text, url, brands)
    ln_new, ln_ours = len(normalize(new_text)), max(1, len(normalize(ours_text)))
    near_identical = sim >= 0.85 or (cont >= 0.9 and 0.7 <= ln_new / ln_ours <= 1.4)
    if near_identical:
        return ("copy" if att else "plagiarism"), round(max(sim, cont), 2)
    run = longest_common_run(new_text, ours_text)
    if run >= 12:
        return "quote", round(cont, 2)
    st = stem_overlap(new_text, ours_text)
    if sim >= 0.12 or cont >= 0.2 or st >= 0.3:
        return "retell", round(max(sim, cont, st), 2)  # своїми словами — це переказ, ніколи не плагіат
    if att:
        return "mention", 0.0
    return None, 0.0


class Echo:
    def __init__(self, store, desk):
        self.store, self.desk = store, desk
        with store.db() as con:
            con.executescript(SCHEMA)

    def brands(self):
        own = [x.strip() for x in (self.store.get("brands") or "").split(",") if x.strip()]
        title = (self.store.get("publish_title") or "").split(" ")[0]
        uname = self.store.get("publish_username") or ""
        extra = [title] if len(title) >= 4 else []
        if uname:
            extra += [uname, "t.me/" + uname]
        return list(dict.fromkeys(own + extra))

    def our_refs(self):
        """Ідентифікатори наших каналів (username і id) — для розпізнавання пересилань."""
        refs = set()
        if self.store.get("publish_chat"):
            refs.add(str(self.store.get("publish_chat")))
        if self.store.get("publish_username"):
            refs.add(self.store.get("publish_username").lower())
        with self.store.db() as con:
            for r in con.execute("SELECT e.ref, e.chat_id FROM src_entries e JOIN src_lists l ON l.id=e.list_id WHERE l.auto='ours'"):
                refs |= {str(r[0]).lower(), str(r[1])}
        refs.discard("")
        return refs

    def check(self, text: str, source: str, url: str, fwd: str | None = None):
        """Перевіряє матеріал проти наших публікацій. -> запис відгомону або None."""
        text = (text or "").strip()
        if not text and not fwd:
            return None
        fwd_ours = bool(fwd) and str(fwd).lower() in self.our_refs()
        brands = self.brands()
        best = (None, 0.0, None)
        cands = self.candidates(text) if text else []
        if fwd_ours and not cands:
            best = ("repost", 1.0, None)
        for o in cands:
            kind, score = classify(text, o["text"], brands, fwd_ours, url)
            order = ["repost", "plagiarism", "copy", "quote", "retell", "mention"]
            if kind and (best[0] is None or order.index(kind) < order.index(best[0])):
                best = (kind, score, o)
        if best[0] is None and attributed(text, url, brands):
            best = ("mention", 0.0, None)
        if best[0] is None:
            return None
        kind, score, o = best
        key = f"{kind}:{url or normalize(text)[:120]}"
        try:
            with self.store.db() as con:
                cur = con.execute("INSERT INTO echoes(at,kind,ours_id,source,url,excerpt,score,attributed,key) VALUES(?,?,?,?,?,?,?,?,?)",
                                  (time.time(), kind, o["id"] if o else None, source[:120], url[:500], text[:600], score,
                                   int(attributed(text, url, brands)), key))
                eid = cur.lastrowid
        except Exception:  # noqa: BLE001 — уже записано
            return None
        return {"id": eid, "kind": kind, "score": score, "ours": o, "source": source, "url": url, "excerpt": text[:300],
                "attributed": attributed(text, url, brands)}

    def candidates(self, text, days=90, limit=5):
        """Наші публікації, схожі за фрагментами або за основами слів."""
        out = []
        with self.store.db() as con:
            con.row_factory = __import__("sqlite3").Row
            rows = [dict(r) for r in con.execute("SELECT * FROM ours_posts WHERE at>? ORDER BY id DESC LIMIT 1500", (time.time() - days * 86400,))]
        for o in rows:
            sim, cont = overlap(text, o["text"])
            sc = max(sim, cont, stem_overlap(text, o["text"]))
            if sc >= 0.12:
                out.append((sc, o))
        out.sort(key=lambda x: -x[0])
        return [o for _s, o in out[:limit]]

    def items(self, kind=None, limit=12, offset=0):
        q = "SELECT * FROM echoes" + (" WHERE kind=?" if kind else "") + " ORDER BY id DESC LIMIT ? OFFSET ?"
        with self.store.db() as con:
            con.row_factory = __import__("sqlite3").Row
            return [dict(r) for r in con.execute(q, ((kind,) if kind else ()) + (limit, offset))]

    def counts(self):
        with self.store.db() as con:
            return {k: n for k, n in con.execute("SELECT kind, COUNT(*) FROM echoes GROUP BY kind")}
