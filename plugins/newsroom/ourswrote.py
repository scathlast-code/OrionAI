"""🗂 «Ми вже про це писали»: схожі матеріали з наших власних публікацій (канали, сайт, соцмережі, які бот збирає
в «🏠 Наші ресурси», плюс усе, що опубліковано з бота). Щоб не повторюватися й додавати посилання на попереднє.
Плюс пошук по наших публікаціях за словами."""
from __future__ import annotations

import datetime as dt
import re

from plugins import esc

STOP = {"та", "і", "й", "в", "у", "на", "з", "із", "до", "що", "як", "за", "про", "для", "по", "від", "це", "не", "а", "але"}


def words(q):
    return [w for w in re.findall(r"[\w'’-]{3,}", (q or "").lower()) if w not in STOP][:8]


def stem(w):
    return w[:max(4, len(w) - 2)] if len(w) > 5 else w


def day(ts):
    try:
        return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def stems(text):
    """Змістові основи слів (перші 5 літер слів від 4 літер, без службових) — для схожості за темою, а не за формулюванням."""
    return {w[:5] for w in re.findall(r"[\w'’-]{4,}", (text or "").lower()) if w not in STOP and not w.isdigit()}


def topic_score(a, b):
    """-> (спільних основ, частка від меншого набору)."""
    if not a or not b:
        return 0, 0.0
    common = len(a & b)
    return common, common / min(len(a), len(b))


class OursWroteUI:
    """Мікс до «Новинарні»."""

    def ow_find(self, did):
        """Схожі наші публікації для новини (кеш на версію новини)."""
        d = self.store.draft(did)
        if not d:
            return []
        key = f"ow:{did}:{d.get('version')}"
        ids = self.store.get(key)
        if ids is None:
            import time
            mine = stems(d["headline"] + " " + d["body"][:1500])
            head = stems(d["headline"])
            scored = []
            for r in self.desk.q("SELECT id,title,text FROM ours_posts WHERE at>? ORDER BY id DESC LIMIT 4000", (time.time() - 730 * 86400,)):
                theirs = stems((r["title"] or "") + " " + r["text"][:1500])
                n, frac = topic_score(mine, theirs)
                hn = len(head & theirs)
                if (n >= 3 and frac >= 0.25) or hn >= 3:
                    scored.append((frac + 0.1 * hn, r["id"]))
            scored.sort(reverse=True)
            ids = [i for _s, i in scored[:6]]
            self.store.set(key, ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        got = {r["id"]: r for r in self.desk.q(f"SELECT * FROM ours_posts WHERE id IN ({marks})", tuple(ids))}
        return [got[i] for i in ids if i in got]

    def ow_line(self, i, r):
        title = esc((r["title"] or r["text"].split("\n")[0])[:110])
        views = f" · 👁 {r['views']}" if r.get("views") else ""
        link = f"<a href=\"{esc(r['url'])}\">{title}</a>" if r["url"] else title
        return f"{i}. {day(r['at'])} · {esc(r['source'][:30])}{views}\n{link}"

    def ow_screen(self, did):
        d = self.store.draft(did)
        if not d:
            return "Новину не знайдено.", [[("« Новинарня", self.cb())]]
        rows_ = self.ow_find(did)
        lines = [f"🗂 <b>Ми вже про це писали? · #{did}</b>", f"<b>{esc(d['headline'])}</b>", ""]
        rows = []
        if not rows_:
            lines.append("Схожих наших публікацій за 2 роки не знайдено. Бот звіряє з тим, що зібрано в «🏠 Наші ресурси», "
                         f"і з опублікованим з бота (зараз у базі {self.desk.ours_count()}).")
        else:
            lines.append("Схожі наші матеріали (найсхожіші вгорі):")
            for i, r in enumerate(rows_, 1):
                lines += ["", self.ow_line(i, r)]
            lines += ["", "«➕» — додати в кінець новини рядок «Раніше ми писали: …» з посиланням (нова версія)."]
            rows.append([(f"➕ {i}", self.cb("owa", did, r["id"])) for i, r in enumerate(rows_, 1) if r["url"]][:6])
        rows += [[("🔎 Пошук у наших публікаціях", self.cb("ows"))], [("« Новина", self.cb("d", did))]]
        return "\n".join(lines)[:4000], [r for r in rows if r]

    def ow_search(self, q):
        ws = words(q)
        if not ws:
            return []
        cond = " OR ".join("lower(title||' '||text) LIKE ?" for _ in ws)
        rows = self.desk.q(f"SELECT * FROM ours_posts WHERE {cond} ORDER BY at DESC LIMIT 400", tuple(f"%{stem(w)}%" for w in ws))
        scored = []
        for r in rows:
            low = (r["title"] + " " + r["text"]).lower()
            hit = sum(1 for w in ws if stem(w) in low)
            scored.append((hit, r["at"], r))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [r for h, _a, r in scored if h >= max(1, (len(ws) + 1) // 2)][:10]

    def ow_search_screen(self, q=""):
        lines = ["🔎 <b>Пошук у наших публікаціях</b>", ""]
        if not q:
            lines.append(f"У базі {self.desk.ours_count()} наших публікацій. Напишіть слова — знайду, коли й де ми про це писали.")
        else:
            res = self.ow_search(q)
            lines.append(f"За запитом «{esc(q)}»: {len(res) or 'нічого'}" + (" (свіжіші й точніші вгорі)" if res else ""))
            for i, r in enumerate(res, 1):
                lines += ["", self.ow_line(i, r)]
        return "\n".join(lines)[:4000], [[("✍️ Новий пошук", self.cb("owq"))], [("« Новинарня", self.cb())]]

    async def ow_route(self, chat, a, x, y):
        if a == "ow":
            return None, self.ow_screen(int(x))
        if a == "owa":
            did, oid = int(x), int(y)
            d = self.store.draft(did)
            r = (self.desk.q("SELECT * FROM ours_posts WHERE id=?", (oid,)) or [None])[0]
            if not d or not r or not r["url"]:
                return "Немає посилання", self.ow_screen(did)
            if r["url"] in d["body"]:
                return "Вже додано", self.ow_screen(did)
            title = (r["title"] or r["text"].split("\n")[0])[:120].strip()
            body = d["body"].rstrip() + f"\n\nРаніше ми писали: {title} — {r['url']}"
            self.store.update_draft(did, body=body, note="🗂 посилання на наше попереднє")
            return "➕ Додано посилання", self.ow_screen(did)
        if a == "ows":
            return None, self.ow_search_screen()
        if a == "owq":
            self.ask(chat, "owq")
            return None, ("🔎 Що шукаємо в наших публікаціях? Напишіть кілька слів, напр. <code>зубри Карпати</code>.",
                          [[("✖️ Скасувати", "m:cancel")]])
        return None, self.ow_search_screen()

    async def ow_text(self, chat, key, data, text):
        return None, self.ow_search_screen(text.strip()[:100])
