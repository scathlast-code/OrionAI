"""📅 Теми дня: дні, тижні, місячники й роки тем — із Вікіпедії, каналів/чатів/RSS, вбудованого календаря й ваших записів.

Без ШІ: Вікіпедія (сторінки «6 листопада» → «Свята і пам'ятні дні») завантажується завчасно на весь рік (366 дат, повтор раз на пів року),
дописи каналів розбираються простими шаблонами («Сьогодні — Всесвітній день…»).
"""
from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import json
import logging
import re
import time

import httpx

from plugins import esc
from .store import normalize

log = logging.getLogger("orion.plugins.newsroom.obs")
MONTHS_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
MONTHS = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
KINDS = {"day": "📌 день", "week": "🗓 тиждень", "month": "📆 місяць / місячник", "year": "🎆 рік"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS observances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'day',
    start TEXT NOT NULL, finish TEXT NOT NULL,           -- MM-DD (щороку) або YYYY-MM-DD (разово)
    yearly INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL, key TEXT UNIQUE
);
"""

# Вбудоване: лише те, що стабільне з року в рік (дні беруться з Вікіпедії). Позначено «перевірте» там, де варто звірити.
SEED = [
    ("Рожевий жовтень — місяць обізнаності про рак грудей", "month", "10-01", "10-31", 1, "вбудований календар"),
    ("Movember — місяць чоловічого здоров'я", "month", "11-01", "11-30", 1, "вбудований календар"),
    ("Всесвітній тиждень обізнаності про протимікробну резистентність", "week", "11-18", "11-24", 1, "вбудований календар (ВООЗ)"),
    ("Міжнародний рік пасовищ і пастухів (ООН)", "year", "2026-01-01", "2026-12-31", 0, "вбудований календар (ООН, перевірте)"),
    ("Міжнародний рік волонтерів заради сталого розвитку (ООН)", "year", "2026-01-01", "2026-12-31", 0, "вбудований календар (ООН, перевірте)"),
    ("Міжнародний рік жінки-фермерки (ООН)", "year", "2026-01-01", "2026-12-31", 0, "вбудований календар (ООН, перевірте)"),
]

POST_RE = re.compile(
    r"(?:(?:сьогодні|сегодня|today)\s*[—–\-:,]?\s*(?:відзначається|відзначають|святкують|триває|стартує|починається)?\s*)?"
    r"((?:Всесвітній|Міжнародний|Всеукраїнський|Національний|Європейський|Всесвітня|Міжнародна|Всеукраїнська|Європейська)\s+"
    r"(?:день|тиждень|місяць|місячник|рік|декада)\s+[^.!?\n«»\"]{3,90}"
    r"|(?:День|Тиждень|Місячник|Місяць)\s+[а-яіїєґ'’\- ]{3,70}?(?=[.!?\n,]|$))", re.I)


def kind_of(title):
    t = title.lower()
    if "тижд" in t:
        return "week"
    if "місяч" in t or "місяць" in t:
        return "month"
    if re.search(r"\bрік\b|\bроку\b", t):
        return "year"
    return "day"


def strip_wiki(s):
    s = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    for _ in range(3):
        s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[https?://\S+\s([^\]]*)\]", r"\1", s)
    s = re.sub(r"'{2,}", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip(" .;:—-")


def parse_wikitext(wt, include_countries=False):
    """Розділ «Свята і пам'ятні дні» сторінки дати -> [(назва, ознака міжнародне/Україна)]."""
    m = re.search(r"^==\s*Свята[^=\n]*==\s*$", wt, re.M)
    if not m:
        return []
    rest = wt[m.end():]
    nxt = re.search(r"^==[^=].*==\s*$", rest, re.M)
    sec = rest[: nxt.start()] if nxt else rest
    out, scope = [], ""
    for line in sec.splitlines():
        h = re.match(r"^===+\s*(.+?)\s*===+", line)
        if h:
            scope = h.group(1).lower()
            continue
        if not line.startswith("*"):
            continue
        depth = len(line) - len(line.lstrip("*"))
        txt = strip_wiki(line.lstrip("*").strip())
        if not txt or len(txt) < 4:
            continue
        ukr = "україн" in txt.lower() or "україн" in scope
        intl = "міжнар" in scope or "всесвіт" in txt.lower() or "міжнародн" in txt.lower() or not scope
        if not (ukr or intl or include_countries):
            continue
        txt = re.sub(r"^(?:Україна|в Україні)\s*[:—-]\s*", "", txt)
        if depth > 1 and out:
            continue  # вкладені пояснення
        out.append((txt[:160], "🇺🇦" if ukr else "🌍"))
    return out


class ObservancesUI:
    def _oinit(self):
        if getattr(self, "_obs_ready", False):
            return
        with self.store.db() as con:
            con.executescript(SCHEMA)
            for t, k, a, b, y, src in SEED:
                con.execute("INSERT OR IGNORE INTO observances(title,kind,start,finish,yearly,source,added,key) VALUES(?,?,?,?,?,?,?,?)",
                            (t, k, a, b, y, src, time.time(), normalize(t)[:80] + "|" + a))
        self._obs_ready = True

    def today(self):
        tz = self.host.tz
        return dt.datetime.now(tz).date() if tz else dt.date.today()

    # ----- база
    def obs_add(self, title, kind, start, finish, yearly, source, url=""):
        self._oinit()
        title = re.sub(r"\s+", " ", title).strip(" .,;:—-")[:160]
        if len(title) < 5:
            return None
        key = normalize(title)[:80] + "|" + start
        with self.store.db() as con:
            cur = con.execute("INSERT OR IGNORE INTO observances(title,kind,start,finish,yearly,source,url,added,key) VALUES(?,?,?,?,?,?,?,?,?)",
                              (title, kind, start, finish, int(yearly), source[:120], url[:500], time.time(), key))
            return cur.lastrowid if cur.rowcount else None

    def obs_on(self, d: dt.date):
        """Теми, що припадають на дату d (дні цієї дати + тижні/місяці/роки, що тривають)."""
        self._oinit()
        md, ymd = d.strftime("%m-%d"), d.isoformat()
        with self.store.db() as con:
            con.row_factory = __import__("sqlite3").Row
            rows = [dict(r) for r in con.execute("SELECT * FROM observances WHERE hidden=0")]
        out = []
        for r in rows:
            if r["yearly"]:
                a, b = r["start"][-5:], r["finish"][-5:]
                hit = (a <= md <= b) if a <= b else (md >= a or md <= b)
            else:
                hit = r["start"] <= ymd <= r["finish"]
            if hit:
                out.append(r)
        order = {"day": 0, "week": 1, "month": 2, "year": 3}
        return sorted(out, key=lambda r: (order.get(r["kind"], 9), r["title"]))

    def obs(self, oid):
        with self.store.db() as con:
            con.row_factory = __import__("sqlite3").Row
            r = con.execute("SELECT * FROM observances WHERE id=?", (int(oid),)).fetchone()
        return dict(r) if r else None

    # ----- збирання
    async def fetch_wiki_day(self, d: dt.date, client=None):
        page = f"{d.day} {MONTHS_GEN[d.month - 1]}"
        url = "https://uk.wikipedia.org/w/api.php"
        params = {"action": "parse", "page": page, "prop": "wikitext", "format": "json", "formatversion": "2", "redirects": "1", "maxlag": "5"}
        own = client is None
        c = client or httpx.AsyncClient(timeout=20, headers={"User-Agent": "OrionAI-Newsroom/2.1 (https://github.com/scathlast-code/OrionAI; Telegram bot calendar) httpx", "Accept": "application/json"})
        try:
            r = await c.get(url, params=params)
            if r.status_code == 429 or "json" not in r.headers.get("content-type", ""):
                # Вікіпедія відповіла не JSON (обмеження частоти чи блок) — це не «порожній день», а збій: хай вирішує prefetch
                raise RuntimeError(f"HTTP {r.status_code}, {r.headers.get('content-type', '?')}")
            wt = (r.json().get("parse") or {}).get("wikitext") or ""
        finally:
            if own:
                await c.aclose()
        n = 0
        for title, flag in parse_wikitext(wt, bool(self.store.get("obs_countries"))):
            if self.obs_add(title, kind_of(title), d.strftime("%m-%d"), d.strftime("%m-%d"), 1, f"Вікіпедія {flag}",
                            f"https://uk.wikipedia.org/wiki/{page.replace(' ', '_')}"):
                n += 1
        return n

    async def prefetch(self, days=366, refresh_days=180):
        """Усі 366 дат року одразу (дні повторюються щороку, тож це покриває й наступні роки).
        Повторно дата оновлюється раз на refresh_days днів — щоб підхопити правки Вікіпедії."""
        done = self.store.get("obs_fetched_at") or {}
        if isinstance(done, list):
            done = {k: time.time() for k in done}
        now, n = time.time(), 0
        t0 = self.today()
        order = [t0 + dt.timedelta(days=i) for i in range(366)]          # спершу найближчі
        seen = set()
        fails, first_err = 0, ""
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "OrionAI-Newsroom/2.1 (https://github.com/scathlast-code/OrionAI; Telegram bot calendar) httpx", "Accept": "application/json"}) as c:
            for d in order[:days]:
                key = d.strftime("%m-%d")
                if key in seen or now - float(done.get(key, 0)) < refresh_days * 86400:
                    continue
                seen.add(key)
                try:
                    n += await self.fetch_wiki_day(d, c)
                    done[key] = now
                    fails = 0
                    if len(seen) % 20 == 0:
                        self.store.set("obs_fetched_at", done)
                except Exception as exc:  # noqa: BLE001
                    fails += 1
                    first_err = first_err or f"{key}: {exc}"
                    if fails >= 5:  # Вікіпедія зараз не відповідає — не засмічуємо журнал 366 рядками, спробуємо згодом
                        log.warning("wiki: 5 збоїв поспіль (перший — %s); календар допишеться пізніше", first_err)
                        break
                    await asyncio.sleep(3 * fails)
                await asyncio.sleep(1.0)
            if t0.year % 4 and "02-29" not in done:                       # 29 лютого — з високосного року
                try:
                    n += await self.fetch_wiki_day(dt.date(2028, 2, 29), c)
                    done["02-29"] = now
                except Exception:  # noqa: BLE001
                    pass
        self.store.set("obs_fetched_at", done)
        return n

    def obs_coverage(self):
        done = self.store.get("obs_fetched_at") or {}
        return len(done) if isinstance(done, dict) else len(done)

    def scan_post(self, text, source, url=""):
        """Шукає в дописі «Сьогодні — Всесвітній день …» тощо. Без ШІ."""
        if not text or not re.search(r"(?i)\b(день|тиждень|місячник|місяць|рік)\b", text):
            return 0
        low = text.lower()
        n = 0
        d = self.today()
        for m in POST_RE.finditer(text):
            title = m.group(1).strip()
            ctx = low[max(0, m.start() - 60): m.start() + 5]
            if not (re.search(r"сьогодні|сегодня|today|відзнача|триває|стартує|починається", ctx) or
                    re.match(r"(?i)(всесвітн|міжнародн|всеукраїнськ|європейськ)", title)):
                continue
            k = kind_of(title)
            if k == "day":
                a = b = d.strftime("%m-%d")
                yearly = 1
            else:
                a, b, yearly = d.isoformat(), (d + dt.timedelta(days=6 if k == "week" else 29 if k == "month" else 364)).isoformat(), 0
            if self.obs_add(title[:1].upper() + title[1:], k, a, b, yearly, f"канал: {source}"[:120], url):
                n += 1
        return n

    # ----- екрани
    def obs_line(self, r, with_dates=False):
        span = ""
        if with_dates or r["kind"] != "day":
            a, b = r["start"], r["finish"]
            fmt = lambda x: ".".join(reversed(x.split("-")[-2:])) if len(x) <= 5 or True else x
            span = f" · {fmt(a)}" + (f"–{fmt(b)}" if b != a else "")
        return f"{KINDS.get(r['kind'], '•').split()[0]} {esc(r['title'])}{span}"

    def obs_home(self, chat=None):
        d = self.today()
        rs = self.obs_on(d)
        days = [r for r in rs if r["kind"] == "day"]
        ongoing = [r for r in rs if r["kind"] != "day"]
        lines = [f"📅 <b>Теми дня · {d.day} {MONTHS_GEN[d.month - 1]}, {WD[d.weekday()]}</b>", ""]
        lines += [self.obs_line(r) for r in days[:20]] or ["Сьогоднішніх днів у базі немає — «🔄 Оновити з інтернету»."]
        if ongoing:
            lines += ["", "<b>Триває:</b>"] + [self.obs_line(r) for r in ongoing[:12]]
        on = self.store.get("obs_morning") is not False
        cov = self.obs_coverage()
        lines += ["", f"⏰ Ранкове сповіщення: {self.store.get('obs_time') or '07:30'}" + ("" if on else " (вимкнено)"),
                  f"📚 Календар із Вікіпедії: {cov}/366 днів року" + (" — завантажується у фоні" if cov < 366 else " ✅")]
        rows = [[(f"✍️ {r['title'][:30]}", self.cb("ob", r["id"]))] for r in (days + ongoing)[:6]]
        rows += [[("📅 Сьогодні", self.cb("obt", "day")), ("🗓 Тиждень", self.cb("obt", "week"))],
                 [("📆 Місяць", self.cb("obt", "month")), ("🎆 Рік", self.cb("obt", "year"))],
                 [("🗓 Календар", self.cb("obc", d.strftime("%Y%m"))), ("➕ Своя тема", self.cb("oba"))],
                 [("🔄 Оновити з інтернету", self.cb("obu")), (("⏰ " + (self.store.get("obs_time") or "07:30")) if on else "⏰ вимк.", self.cb("obm"))],
                 [("« Знайти й написати", self.cb("D"))]]
        return "\n".join(lines)[:4000], rows

    def obs_period(self, what):
        d = self.today()
        if what == "day":
            return self.obs_home()
        lines, rows = [], []
        if what == "week":
            lines.append("🗓 <b>Найближчі 7 днів</b>")
            for i in range(7):
                x = d + dt.timedelta(days=i)
                items = [r for r in self.obs_on(x) if r["kind"] == "day"]
                lines.append(f"\n<b>{x.day} {MONTHS_GEN[x.month - 1]}, {WD[x.weekday()]}</b>")
                lines += [self.obs_line(r) for r in items[:8]] or ["—"]
            weeks = {r["id"]: r for i in range(7) for r in self.obs_on(d + dt.timedelta(days=i)) if r["kind"] == "week"}
            if weeks:
                lines += ["", "<b>Тижні:</b>"] + [self.obs_line(r, True) for r in weeks.values()]
        elif what == "month":
            last = calendar.monthrange(d.year, d.month)[1]
            lines.append(f"📆 <b>{MONTHS[d.month - 1]} {d.year}</b>")
            months = {r["id"]: r for r in self.obs_on(d) if r["kind"] in ("month", "week")}
            if months:
                lines += ["<b>Місячники й тижні:</b>"] + [self.obs_line(r, True) for r in months.values()]
            for day in range(1, last + 1):
                x = dt.date(d.year, d.month, day)
                items = [r for r in self.obs_on(x) if r["kind"] == "day"]
                if items:
                    mark = "▶️ " if x == d else ("· " if x < d else "")
                    lines.append(f"\n{mark}<b>{day}</b>: " + "; ".join(esc(r["title"][:60]) for r in items[:4]) + (f" (+{len(items) - 4})" if len(items) > 4 else ""))
        else:
            lines.append(f"🎆 <b>{d.year}</b>")
            years = {r["id"]: r for r in self.obs_on(d) if r["kind"] == "year"}
            lines += ["<b>Роки тем:</b>"] + ([self.obs_line(r) for r in years.values()] or ["—"])
            lines.append("\n<b>Місячники за рік:</b>")
            for m in range(1, 13):
                ms = {r["id"]: r for r in self.obs_on(dt.date(d.year, m, 15)) if r["kind"] == "month"}
                if ms:
                    lines.append(f"{MONTHS[m - 1]}: " + "; ".join(esc(r["title"][:60]) for r in ms.values()))
        rows.append([("🗓 Календар", self.cb("obc", d.strftime("%Y%m"))), ("« Теми дня", self.cb("obh"))])
        return "\n".join(lines)[:4000], rows

    def obs_calendar(self, ym):
        y, m = int(ym[:4]), int(ym[4:6])
        today = self.today()
        rows = [[(w, "m:noop") for w in WD]]
        week = [(" ", "m:noop")] * dt.date(y, m, 1).weekday()
        for day in range(1, calendar.monthrange(y, m)[1] + 1):
            x = dt.date(y, m, day)
            n = len(self.obs_on(x))
            label = (f"[{day}]" if x == today else str(day)) + ("•" if n else "")
            week.append((label, self.cb("obd", x.strftime("%Y%m%d"))))
            if len(week) == 7:
                rows.append(week)
                week = []
        if week:
            rows.append(week + [(" ", "m:noop")] * (7 - len(week)))
        prev = (dt.date(y, m, 1) - dt.timedelta(days=1)).strftime("%Y%m")
        nxt = (dt.date(y, m, 28) + dt.timedelta(days=5)).strftime("%Y%m")
        rows.append([("◀️ " + MONTHS[int(prev[4:]) - 1], self.cb("obc", prev)), (MONTHS[int(nxt[4:]) - 1] + " ▶️", self.cb("obc", nxt))])
        rows.append([("« Теми дня", self.cb("obh"))])
        return (f"🗓 <b>{MONTHS[m - 1]} {y}</b>\n• — є теми; [число] — сьогодні. Торкніться дня, щоб побачити теми (минулі теж)."), rows

    def obs_day(self, ymd):
        x = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        rs = self.obs_on(x)
        when = "минуло" if x < self.today() else ("сьогодні" if x == self.today() else f"через {(x - self.today()).days} дн.")
        lines = [f"📅 <b>{x.day} {MONTHS_GEN[x.month - 1]} {x.year}</b> · {when}", ""] + ([self.obs_line(r) for r in rs] or ["Тем немає."])
        rows = [[(f"✍️ {r['title'][:30]}", self.cb("ob", r["id"]))] for r in rs[:8]]
        rows.append([("🗓 Календар", self.cb("obc", ymd[:6])), ("« Теми дня", self.cb("obh"))])
        return "\n".join(lines)[:4000], rows

    def obs_card(self, oid):
        r = self.obs(oid)
        if not r:
            return "Тему не знайдено.", [[("« Теми дня", self.cb("obh"))]]
        text = (f"{self.obs_line(r, True)}\n\n{KINDS.get(r['kind'])} · {'щороку' if r['yearly'] else 'разово'}\nДжерело: {esc(r['source'])}")
        rows = [[("📱 Коротко", self.cb("obw", oid, "short")), ("📰 Новина", self.cb("obw", oid, "news")), ("📚 Велика", self.cb("obw", oid, "big"))],
                [("➕ До зведення", self.cb("obz", oid)), ("🙈 Сховати тему", self.cb("obx", oid))]]
        if r["url"].startswith("http"):
            rows.append([("🔗 Джерело", "url:" + r["url"])])
        rows.append([("« Теми дня", self.cb("obh"))])
        return text, rows

    # ----- написання на тему дня
    async def write_observance(self, chat, oid, size):
        r = self.obs(oid)
        if not r:
            return
        from .desk_ui import Progress
        pg = await Progress(self.host, chat, f"Тема дня: {r['title'][:60]}").start("🔎 Крок 1 з 3: шукаю свіже про тему…")
        try:
            cands = await self.web_candidates([f"\"{r['title'][:80]}\""], 72)
            if not cands:
                cands = await self.web_candidates([r["title"][:80]], 24 * 30)
            did = None
            if cands:
                await pg.set(f"⭐ Крок 2 з 3: оцінюю знайдене ({len(cands)})…")
                scored = await self.score(cands)
                ids = await self.save_finds("mode", f"obs-{oid}", scored)
                fs = self.desk.finds(ids=ids, sort="prio", limit=5)
                if fs:
                    await pg.set("✍️ Крок 3 з 3: ШІ пише чернетку…")
                    did = await self.compose(chat, fs, size, size, f"📅 До теми: {r['title']}.")
                    return await pg.done("✅ Чернетку готово — нижче" if did else "ℹ️ Чернетку не написано", self.prompt_rows())
            await pg.set("✍️ Крок 3 з 3: свіжих новин немає — ШІ пише тематичний допис…")
            fake = [{"id": 0, "title": r["title"], "url": r["url"], "source": r["source"], "published": time.time(),
                     "snippet": f"{r['title']} ({KINDS.get(r['kind'])}). Свіжих новин не знайдено — напиши тематичний допис: що це за "
                                "день/тиждень, навіщо він, що може зробити читач. Без вигаданих цифр і подій.", "story_id": None}]
            did = await self.compose(chat, fake, size, size, "📅 Тематичний допис без свіжих новин — перевірте факти.")
            await pg.done("✅ Тематичний допис готовий — нижче" if did else "ℹ️ Чернетку не написано", self.prompt_rows())
        except Exception as exc:  # noqa: BLE001
            log.exception("write_observance")
            await pg.done(f"⚠️ Не вдалося: {esc(exc)[:300]}", self.prompt_rows())

    # ----- фон
    async def _obs_loop(self):
        await asyncio.sleep(50)
        sent_day = None
        while True:
            try:
                if self.on():
                    now = dt.datetime.now(self.host.tz) if self.host.tz else dt.datetime.now()
                    if now.hour == 3 or self.obs_coverage() < 300:
                        if self.store.get("obs_last_fetch") != now.date().isoformat():
                            await self.prefetch()
                            self.store.set("obs_last_fetch", now.date().isoformat())
                    hm = self.store.get("obs_time") or "07:30"
                    if self.store.get("obs_morning") is not False and now.strftime("%H:%M") == hm and sent_day != now.date():
                        sent_day = now.date()
                        text, rows = self.obs_home()
                        await self.host.notify_admins("🌅 " + text, rows)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("observances loop")
            await asyncio.sleep(30)

    # ----- маршрути
    async def obs_route(self, chat, a, x, y):
        if a == "obh":
            return None, self.obs_home(chat)
        if a == "obt":
            return None, self.obs_period(x)
        if a == "obc":
            return None, self.obs_calendar(x)
        if a == "obd":
            return None, self.obs_day(x)
        if a == "ob":
            return None, self.obs_card(x)
        if a == "obw":
            self._bg(self.write_observance(chat, int(x), y))
            return "✍️ Пишу…", ("✍️ <b>Пишу на тему дня</b>\nУ чаті з ботом видно хід роботи (крок і секунди), потім прийде чернетка. Якщо ШІ відмовиться — «📋 Промт для іншого ШІ» або «🔁 Іншою моделлю».", [[("« Теми дня", self.cb("obh"))]])
        if a == "obz":
            r = self.obs(x)
            n = self.add_collected(chat, "text", f"📅 Тема дня: {r['title']}", r["source"], r["url"])
            return f"➕ У зведенні: {n}", self.obs_card(x)
        if a == "obx":
            with self.store.db() as con:
                con.execute("UPDATE observances SET hidden=1 WHERE id=?", (int(x),))
            return "🙈 Сховано", self.obs_home(chat)
        if a == "obu":
            n = await self.prefetch()
            self._bg(self.prefetch(refresh_days=0 if self.obs_coverage() >= 366 else 180))
            return "🔄 Завантажую весь рік у фоні", self.obs_home(chat)
        if a == "obm":
            self.ask(chat, "obm")
            return None, ("⏰ О котрій надсилати теми дня? Напишіть час, напр. <code>07:30</code>, або «-», щоб вимкнути.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "oba":
            self.ask(chat, "oba")
            return None, ("➕ Своя тема: <code>Назва | дата або період | щороку/разово</code>\nНапр.:\n<code>День супу | 15.11</code>\n"
                          "<code>Тиждень інформування про грип | 01.11–07.11</code>\n<code>Місячник прибирання | 01.04–30.04 | щороку</code>",
                          [[("✖️ Скасувати", "m:cancel")]])
        return None, None

    async def obs_text(self, chat, key, data, text):
        t = text.strip()
        if key == "obm":
            if t in ("-", "—"):
                self.store.set("obs_morning", False)
            elif re.fullmatch(r"\d{1,2}[:.]\d{2}", t):
                h, m = re.split(r"[:.]", t)
                self.store.set("obs_time", f"{int(h):02d}:{m}")
                self.store.set("obs_morning", True)
            return "Збережено", self.obs_home(chat)
        if key == "oba":
            parts = [p.strip() for p in t.split("|")]
            dates = re.findall(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", parts[1] if len(parts) > 1 else "")
            if not parts[0] or not dates:
                return "Формат: Назва | 15.11 або 01.11–07.11", self.obs_home(chat)
            (d1, m1, y1), (d2, m2, y2) = dates[0], dates[-1]
            once = (len(parts) > 2 and "раз" in parts[2].lower()) or bool(y1)
            if once:
                yy = int(y1 or self.today().year)
                a, b = f"{yy}-{int(m1):02d}-{int(d1):02d}", f"{int(y2 or yy)}-{int(m2):02d}-{int(d2):02d}"
            else:
                a, b = f"{int(m1):02d}-{int(d1):02d}", f"{int(m2):02d}-{int(d2):02d}"
            self.obs_add(parts[0], kind_of(parts[0]) if a == b or kind_of(parts[0]) != "day" else "week", a, b, 0 if once else 1, "ваш запис")
            return "➕ Додано", self.obs_home(chat)
        return None, None
