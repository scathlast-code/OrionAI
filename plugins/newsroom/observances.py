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
from contextlib import closing

import httpx

from plugins import esc
from . import daybook as DB
from . import dayextra as DX
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
CREATE TABLE IF NOT EXISTS daybook (
    id INTEGER PRIMARY KEY AUTOINCREMENT, md TEXT NOT NULL, cat TEXT NOT NULL, text TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0, owner INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL, key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS daybook_md ON daybook(md, cat);
CREATE TABLE IF NOT EXISTS obs_fav (user INTEGER NOT NULL, oid INTEGER NOT NULL, PRIMARY KEY(user, oid));
"""
FETCH_V = 2  # 2 — разом зі святами беремо й події, народились/померли, іменини, народний календар
WARN_DEFAULT = {"1": [1], "2": [4]}  # за скільки днів попереджати: важливі — за 1, дуже важливі — за 4

# Вбудоване: лише те, що стабільне з року в рік (дні беруться з Вікіпедії). Позначено «перевірте» там, де варто звірити.
SEED = [
    ("Рожевий жовтень — місяць обізнаності про рак грудей", "month", "10-01", "10-31", 1, "вбудований календар"),
    ("Movember — місяць чоловічого здоров'я", "month", "11-01", "11-30", 1, "вбудований календар"),
    ("Всесвітній тиждень обізнаності про протимікробну резистентність", "week", "11-18", "11-24", 1, "вбудований календар (ВООЗ)"),
    ("Міжнародний рік пасовищ і пастухів (ООН)", "year", "2026-01-01", "2026-12-31", 0, "вбудований календар (ООН, перевірте)"),
    ("Міжнародний рік волонтерів заради сталого розвитку (ООН)", "year", "2026-01-01", "2026-12-31", 0, "вбудований календар (ООН, перевірте)"),
    ("Міжнародний рік жінки-фермерки (ООН)", "year", "2026-01-01", "2026-12-31", 0, "вбудований календар (ООН, перевірте)"),
]
SEED_URLS = {  # офіційні сторінки вбудованих тем — щоб жоден рядок не був без джерела
    "Рожевий жовтень — місяць обізнаності про рак грудей": "https://www.who.int/news-room/fact-sheets/detail/breast-cancer",
    "Movember — місяць чоловічого здоров'я": "https://movember.com/",
    "Всесвітній тиждень обізнаності про протимікробну резистентність": "https://www.who.int/campaigns/world-amr-awareness-week",
    "Міжнародний рік пасовищ і пастухів (ООН)": "https://www.un.org/en/observances/international-years",
    "Міжнародний рік волонтерів заради сталого розвитку (ООН)": "https://www.un.org/en/observances/international-years",
    "Міжнародний рік жінки-фермерки (ООН)": "https://www.un.org/en/observances/international-years",
}

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


from footnotes import Notes, html_cut  # noqa: E402 — 📎 зноски-джерела [n]


CLAUSE_RE = re.compile(r"\s+(?:проголошен\w*|встановлен\w*|започатков\w*|запровадж\w*|відповідно до|згідно з|заснован\w*|відзначаєт\w*|"
                       r"відзначають|святкуєт\w*|ухвален\w*)\b.*$", re.I)


def clean_title(t):
    """🧹 Назва дня/свята без сміття з Вікіпедії: HTML-сутності (&nbsp;), пояснення «проголошено Резолюцією…»,
    крапка перед роком, подвійні пробіли. Перевіряється перед кожним показом і збереженням."""
    import html as _h
    t = str(t or "")
    for _ in range(3):
        t = _h.unescape(t)
    t = t.replace("\xa0", " ").replace("\u200b", "")
    year = re.search(r"\s*\((\d{4})\)\s*$", t)
    core = t[:year.start()] if year else t
    core = CLAUSE_RE.sub("", core)
    core = re.sub(r"\s+", " ", core).strip(" .,;:—-")
    return core + (f" ({year.group(1)})" if year else "")


def _src_label(url, fallback=""):
    u = str(url or "")
    if "wikipedia.org" in u:
        from urllib.parse import unquote
        page = unquote(u.split("/wiki/")[-1]).replace("_", " ")
        return "Вікіпедія (" + page.replace("#", " → ") + ")"
    return fallback


MONTHS_NOM = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]


class ObservancesUI:
    def _oinit(self):
        if getattr(self, "_obs_ready", False):
            return
        with closing(self.store.db()) as con, con:
            con.executescript(SCHEMA)
            cols = {r[1] for r in con.execute("PRAGMA table_info(observances)")}
            for col, ddl in (("imp", "INTEGER"), ("owner", "INTEGER NOT NULL DEFAULT 0"), ("why", "TEXT NOT NULL DEFAULT ''"),
                             ("since", "TEXT NOT NULL DEFAULT ''")):
                if col not in cols:
                    con.execute(f"ALTER TABLE observances ADD COLUMN {col} {ddl}")
            dcols = {r[1] for r in con.execute("PRAGMA table_info(daybook)")}
            if "imp" not in dcols:  # ❗ ручна позначка важливого пункту (NULL — автоматично за темою)
                con.execute("ALTER TABLE daybook ADD COLUMN imp INTEGER")
            split = not self.store.get("discover_split_v1")
            if split:  # відкриття — окремим розділом (раніше були в «Історії»)
                for rid, txt in con.execute("SELECT id, text FROM daybook WHERE cat='history'").fetchall():
                    if DB.is_discovery(txt):
                        con.execute("UPDATE OR IGNORE daybook SET cat='discover', key='discover'||substr(key, 8) WHERE id=?", (rid,))
            for t, k, a, b, y, src in SEED:
                con.execute("INSERT OR IGNORE INTO observances(title,kind,start,finish,yearly,source,url,added,key) VALUES(?,?,?,?,?,?,?,?,?)",
                            (t, k, a, b, y, src, SEED_URLS.get(t, ""), time.time(), normalize(t)[:80] + "|" + a))
                if SEED_URLS.get(t):
                    con.execute("UPDATE observances SET url=? WHERE title=? AND owner=0 AND (url IS NULL OR url='')", (SEED_URLS[t], t))
        if split:  # позначку пишемо ПІСЛЯ транзакції: інакше друге з'єднання до тієї ж бази чекає на неї — «database is locked»
            self.store.set("discover_split_v1", 1)
        self._obs_ready = True

    def today(self):
        tz = self.host.tz
        return dt.datetime.now(tz).date() if tz else dt.date.today()

    # ----- база
    def ensure_church(self):
        """⛪ Великі неперехідні церковні свята (новоюліанський календар ПЦУ й УГКЦ) — щороку; якщо свято вже є в списку дня — не дублюємо."""
        from . import holidays as H
        if self.store.get("church_v") == 1:
            return
        self._oinit()
        y = self.today().year
        for title, md, page, kw in H.CHURCH:
            d = dt.date(y, int(md[:2]), int(md[3:]))
            if any(re.search(kw, r["title"]) for r in self.obs_on(d)):
                continue
            self.obs_add(title, "day", md, md, 1, "Церковний календар (ПЦУ, УГКЦ)", H.W + page)
        self.store.set("church_v", 1)

    async def ensure_holidays(self):
        """🗓 Рухомі свята з переліку редакції на цей і наступний рік. Дата кожного щороку ЗВІРЯЄТЬСЯ з інтернетом
        (2+ незалежні джерела); розрахунок — лише запасний варіант, поки звірки немає (тоді — «дата уточнюється»)."""
        from . import holidays as H
        import websearch
        self._oinit()
        self.ensure_church()
        y = self.today().year
        done = self.store.get("hol_years3") or {}
        for yy in (y, y + 1):
            if done.get(str(yy)) == "ok":
                continue
            all_ok = True
            for title, a, b, kind, url, key in H.floating(yy):
                exact, srcs = (None, [])
                if key:
                    try:
                        exact, srcs = await H.verify_date(key, yy, a, websearch.search_items)
                    except Exception:  # noqa: BLE001
                        exact = None
                    await asyncio.sleep(0.3)
                if key and not exact:
                    all_ok = False
                shift = (exact - a) if exact else dt.timedelta(0)
                a2, b2 = a + shift, b + shift
                name = title if (exact or not key) else title + " — дата уточнюється"
                with closing(self.store.db()) as con, con:  # стара версія цього свята на цей рік — прибрати
                    con.execute("DELETE FROM observances WHERE source='Календар (розрахунок)' AND owner=0 AND start LIKE ? AND title LIKE ?",
                                (f"{yy}-%", title.split(" (")[0] + "%"))
                self.obs_add(name, kind, a2.isoformat(), b2.isoformat(), 0, "Календар (розрахунок)", (srcs[0] if srcs else url))
            done[str(yy)] = "ok" if all_ok else "partial"
        self.store.set("hol_years3", {k: v for k, v in done.items() if int(k) >= y - 1})

    async def ensure_extras(self, d):
        """🪔 Традиції, 🔭 найближчі астрономічні події, ♈ астрологічні новини, 🌌 сяйво — з джерел, кеш на день (сяйво — 3 год)."""
        from . import dayextra as X
        import websearch
        key = f"dayx:{d.isoformat()}"
        c = self.store.get(key) or {}
        if not c.get("done"):
            trad, tday = await X.traditions(websearch.search_items, d), d
            k = 1
            while not trad and k <= 5:  # на сьогодні нічого — найближчі дні (з позначкою «не на сьогодні»)
                tday = d + dt.timedelta(days=k)
                trad = await X.traditions(websearch.search_items, tday)
                k += 1
            c.update(trad=trad, trad_day=tday.isoformat() if trad else "", astro=await X.astro_news(websearch.search_items, d),
                     astrol=await X.astrol_news(websearch.search_items, d), done=1)
        if time.time() - (c.get("aurora_t") or 0) > 3 * 3600:
            async def get(url):
                async with httpx.AsyncClient(timeout=15) as cl:
                    r = await cl.get(url)
                    r.raise_for_status()
                    return r.json()
            try:
                c["aurora"] = await X.aurora(get)
            except Exception:  # noqa: BLE001
                c["aurora"] = c.get("aurora")
            c["aurora_t"] = time.time()
        self.store.set(key, c)
        return c

    def extras_bg(self, d):
        t = getattr(self, "_extras_task", None)
        if t and not t.done():
            return

        async def go():
            try:
                await self.ensure_extras(d)
            except Exception:  # noqa: BLE001
                log.info("традиції/астрономія: збір не вдався", exc_info=True)
        try:
            self._extras_task = asyncio.get_running_loop().create_task(go())
        except RuntimeError:
            pass

    # ----- 🗓 контент-план (лише читаємо таблицю — ШІ в неї нічого не пише)
    CPLAN_DEFAULT = "https://docs.google.com/spreadsheets/d/1yni79g_IYhe1czePtvZgivrU8iSh2C9U/edit"

    def cplan_urls(self):
        v = self.store.get("cplan_urls")
        return v if isinstance(v, list) else [self.CPLAN_DEFAULT]

    async def ensure_cplan(self, force=False):
        """Завантажуємо .xlsx кожного плану (не частіше ніж раз на 3 год) і зберігаємо {дата: [теми]}."""
        from . import contentplan as CP
        wait = 3 * 3600 if self.store.get("cplan_data") else 600  # не вдалося — пробуємо знову за 10 хв, а не за 3 год
        if not force and time.time() - (self.store.get("cplan_t") or 0) < wait:
            return self.store.get("cplan_data") or {}
        merged, status = {}, []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 OrionAI"}) as cl:
            for url in self.cplan_urls():
                for dl in CP.download_urls(url):
                    try:
                        r = await cl.get(dl)
                        if r.status_code != 200 or not r.content.startswith(b"PK"):
                            log.warning("контент-план: %s → HTTP %s, %s", dl, r.status_code, r.headers.get("content-type"))
                            status.append(f"{dl.split('/')[2]}: HTTP {r.status_code}, {(r.headers.get('content-type') or '?').split(';')[0]}")
                            continue
                        n0 = sum(len(v) for v in merged.values())
                        try:  # 💾 власна копія таблиці (остання версія) — на випадок, якщо доступ до неї зникне
                            keep = self.cplan_dir() / f"{CP.file_id(url)}.xlsx"
                            keep.write_bytes(r.content)
                        except Exception:  # noqa: BLE001
                            log.info("контент-план: копію не збережено", exc_info=True)
                        rep, cells = [], {}
                        for day, items in CP.parse(r.content, rep, cells).items():
                            for it in items:
                                it["src"] = url
                            merged.setdefault(day, []).extend(items)
                        status.append(f"{dl.split('/')[2]}: ✅ завантажено, тем {sum(len(v) for v in merged.values()) - n0}")
                        self.store.set("cplan_sheets", rep[-40:])
                        self.store.set("cplan_cells", {"id": CP.file_id(url), "cells": cells})
                        log.warning("контент-план, аркуші: %s", " | ".join(rep))
                        break
                    except Exception as exc:  # noqa: BLE001
                        log.warning("контент-план: не вдалось завантажити %s", dl, exc_info=True)
                        status.append(f"{dl.split('/')[2]}: {type(exc).__name__}: {str(exc)[:80]}")
        log.warning("контент-план: %d днів, %d тем", len(merged), sum(len(v) for v in merged.values()))
        prev = self.store.get("cplan_data") or {}
        if merged:
            ch = self.cplan_diff(prev, merged) if prev else []
            if ch:
                hist = (self.store.get("cplan_hist") or [])[-30:]
                hist.append({"t": time.time(), "changes": ch[:40]})
                self.store.set("cplan_hist", hist)
                self._cplan_changes = ch
            self.store.set("cplan_data", merged)
            try:
                n = self.cplan_sync(merged)
                if n:
                    log.warning("контент-план: додано в календар днів: %d", n)
            except Exception:  # noqa: BLE001
                log.warning("контент-план: синхронізація з календарем", exc_info=True)
        elif not prev:
            self.store.set("cplan_data", {})
        self.store.set("cplan_t", time.time())
        self.store.set("cplan_status", status)
        return merged

    def cplan_screen(self, chat, ymd=None):
        """🗓 Контент-план як окремий календар (лише теми плану): місяць сіткою, під ним — теми обраного дня."""
        data = self.store.get("cplan_data") or {}
        today = self.today()
        sel = today
        if ymd and len(ymd) >= 6 and ymd.isdigit():
            try:
                sel = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]) if len(ymd) == 8 else 1)
            except ValueError:
                pass
        day_mode = bool(ymd and len(ymd) == 8) or not ymd
        y, m = sel.year, sel.month
        rows = [[(w, "m:noop") for w in WD]]
        week = [(" ", "m:noop")] * dt.date(y, m, 1).weekday()
        for day in range(1, calendar.monthrange(y, m)[1] + 1):
            x = dt.date(y, m, day)
            n = len(data.get(x.isoformat(), []))
            label = (f"[{day}]" if x == today else f"·{day}·" if (day_mode and x == sel) else str(day)) + ("✍️" if n else "")
            week.append((label, self.cb("obcp", x.strftime("%Y%m%d"))))
            if len(week) == 7:
                rows.append(week)
                week = []
        if week:
            rows.append(week + [(" ", "m:noop")] * (7 - len(week)))
        prev = (dt.date(y, m, 1) - dt.timedelta(days=1)).strftime("%Y%m")
        nxt = (dt.date(y, m, 28) + dt.timedelta(days=5)).strftime("%Y%m")
        rows.append([("◀️ План: " + MONTHS[int(prev[4:]) - 1].lower(), self.cb("obcp", prev)),
                     ("План: " + MONTHS[int(nxt[4:]) - 1].lower() + " ▶️", self.cb("obcp", nxt))])
        rows.append([("📄 Відкрити оригінал", "url:" + self.cplan_urls()[0]), ("ℹ️ Стан", self.cb("obcps"))])
        rows.append([("🗓 Загальний календар", self.cb("obc", sel.strftime("%Y%m"))), ("« Теми дня", self.cb("obh", "m"))])
        month_n = sum(len(v) for k, v in data.items() if k.startswith(f"{y}-{m:02d}"))
        lines = [f"📋 <b>Контент-план · {MONTHS[m - 1]} {y}</b> · тем {month_n}",
                 "✍️ — є теми · [число] — сьогодні · ·число· — обраний день. Торкніться дня, щоб побачити його план.", ""]
        if day_mode and self.host.is_admin(chat):  # ✏️ правки людини — у саму таблицю, з підтвердженням
            items_ = data.get(sel.isoformat(), [])
            ed = [(f"✏️ {k + 1}", self.cb("obce", f"{sel:%Y%m%d}.{k}")) for k in range(min(len(items_), 8))]
            rows[-3:-3] = [ed[i:i + 4] for i in range(0, len(ed), 4)] + [
                [("➕ Тема дня", self.cb("obca", f"{sel:%Y%m%d}.topics")), ("➕ Сайт", self.cb("obca", f"{sel:%Y%m%d}.site")),
                 ("➕ SMM", self.cb("obca", f"{sel:%Y%m%d}.smm"))]]
        if day_mode:
            items = data.get(sel.isoformat(), [])
            lines.append(f"<b>{'Сьогодні' if sel == today else f'{sel.day} {MONTHS_GEN[sel.month - 1]}'}, {WD[sel.weekday()]} · {len(items)}</b>")
            for k, it in enumerate(items, 1):
                lines.append(f"{k}. {esc(it['title'])}" + (f" · ✍️ {esc(it['author'])}" if it.get("author") else "")
                             + (f" · 📍 {it['where']}" if it.get("where") else ""))
            if not items:
                lines.append("На цей день у плані порожньо.")
        t = self.store.get("cplan_t")
        lines += ["", f"<i>Таблиця: оновлено {dt.datetime.fromtimestamp(t, self.host.tz).strftime('%d.%m %H:%M') if t else '—'}, "
                      "перевіряю щопівгодини. У загальному календарі ці теми — разом з усіма днями.</i>"]
        return html_cut("\n".join(lines), 4000), rows

    # ----- ✏️ правки контент-плану людиною (ШІ нічого не вписує): правка → перегляд «було/стане» → підтвердження → запис
    def _cp_item(self, ymd, k):
        d = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        items = (self.store.get("cplan_data") or {}).get(d, [])
        return items[k] if 0 <= k < len(items) else None

    def cplan_setup_text(self):
        from . import gsheets as G
        em = G.email()
        return ("✏️ <b>Редагування контент-плану</b>\n\nЩоб правки потрапляли просто в таблицю, потрібно один раз:\n"
                "1. У Google Диску відкрити файл → «Файл» → «Зберегти як Google Таблицю» і надіслати мені нове посилання.\n"
                "2. Додати в Railway змінну <code>GOOGLE_SERVICE_ACCOUNT_JSON</code> — ключ сервісного акаунта Google (Sheets API).\n"
                + (f"3. Поділитися таблицею з <code>{esc(em)}</code> як редактор.\n" if em else
                   "3. Поділитися таблицею з адресою сервісного акаунта (бот покаже її, щойно ключ буде доданий) як редактор.\n")
                + "\nБот змінює лише ту клітинку, яку ви відредагували, і тільки після вашого «✅ Зберегти».")

    async def cplan_edit_route(self, chat, a, x):
        from . import gsheets as G
        if not self.host.is_admin(chat):
            return "Правити план можуть лише адміністратори", None
        if not G.creds():
            return None, (self.cplan_setup_text(), [[("« Контент-план", self.cb("obcp"))]])
        ymd, _, rest = (x or "").partition(".")
        back = [[("« План дня", self.cb("obcp", ymd))]]
        if a == "obce":
            it = self._cp_item(ymd, int(rest or 0))
            if not it or not it.get("cell"):
                return "Не знайшов цю тему в таблиці — оновіть план", None
            self.ask(chat, "obce", {"op": "edit", "ymd": ymd, "k": int(rest)})
            return None, (f"✏️ <b>Правка теми</b>\nЗараз у таблиці: <code>{esc(it['orig'])}</code>\n\n"
                          "Надішліть новий текст (як у таблиці, напр. «Назва - Автор»).",
                          [[("🗑 Прибрати цю тему", self.cb("obcd", x))]] + back)
        if a == "obca":
            self.ask(chat, "obce", {"op": "add", "ymd": ymd, "slot": rest or "topics"})
            where = {"topics": "теми дня", "site": "рядка «Сайт»", "smm": "рядка «smm»"}.get(rest, "теми дня")
            return None, (f"➕ <b>Нова тема для {where}</b> · {ymd[6:8]}.{ymd[4:6]}\nНадішліть текст (напр. «Назва - Автор»).", back)
        if a == "obcd":
            it = self._cp_item(ymd, int(rest or 0))
            if not it:
                return "Не знайдено", None
            self._cpe = {**getattr(self, "_cpe", {}), chat: {"op": "del", "ymd": ymd, "k": int(rest), "new": None}}
            return None, (f"🗑 Прибрати з таблиці тему\n<code>{esc(it['orig'])}</code>?",
                          [[("✅ Так, прибрати з таблиці", self.cb("obcw")), ("✖️ Ні", self.cb("obcx", ymd))]])
        if a == "obcx":
            getattr(self, "_cpe", {}).pop(chat, None)
            self.host.cancel_input(chat)
            return "Скасовано", self.cplan_screen(chat, ymd)
        if a == "obcw":
            p = getattr(self, "_cpe", {}).pop(chat, None)
            if not p:
                return "Немає правки для збереження", None
            return await self.cplan_write(chat, p)
        return None, self.cplan_screen(chat, ymd)

    def cplan_preview(self, chat, p):
        was = ""
        if p["op"] in ("edit", "del"):
            it = self._cp_item(p["ymd"], p["k"]) or {}
            was = it.get("orig", "")
        self._cpe = {**getattr(self, "_cpe", {}), chat: p}
        text = ("✏️ <b>Перевірте правку перед записом у таблицю</b>\n\n" + (f"Було: <code>{esc(was)}</code>\n" if was else "")
                + f"Стане: <code>{esc(p['new'])}</code>")
        return text, [[("✅ Зберегти в таблицю", self.cb("obcw")), ("✖️ Скасувати", self.cb("obcx", p["ymd"]))]]

    async def cplan_write(self, chat, p):
        from . import contentplan as CP, gsheets as G
        meta = self.store.get("cplan_cells") or {}
        sid = meta.get("id") or CP.file_id(self.cplan_urls()[0])
        d = f"{p['ymd'][:4]}-{p['ymd'][4:6]}-{p['ymd'][6:8]}"
        try:
            if p["op"] == "add":
                cell = (meta.get("cells") or {}).get(d, {}).get(p.get("slot") or "topics")
                if not cell:
                    return "Не знайшов клітинку цього дня в таблиці", None
                cur = await G.get_cell(sid, *cell)
                await G.set_cell(sid, *cell, (cur + "\n" if cur.strip() else "") + p["new"])
            else:
                it = self._cp_item(p["ymd"], p["k"])
                if not it:
                    return "Тема вже змінилась — оновіть план", None
                cur = await G.get_cell(sid, *it["cell"])  # свіжий вміст: не затираємо чужих правок
                new, ok = CP.replace_line(cur, it["orig"], p["new"])
                if not ok:
                    return "⚠️ У таблиці цей рядок уже змінили — оновіть план і спробуйте ще раз", self.cplan_screen(chat, p["ymd"])
                await G.set_cell(sid, *it["cell"], new)
        except Exception as exc:  # noqa: BLE001
            log.warning("контент-план: запис", exc_info=True)
            return None, (f"⚠️ Не вдалося записати: {esc(str(exc)[:200])}\n\n" + self.cplan_setup_text(), [[("« План дня", self.cb("obcp", p["ymd"]))]])
        await self.ensure_cplan(force=True)
        return "✅ Записано в таблицю", self.cplan_screen(chat, p["ymd"])

    def cplan_status(self, chat):
        """ℹ️ Технічний стан завантаження: відповіді Google, аркуші, остання зміна."""
        data = self.store.get("cplan_data") or {}
        t = self.store.get("cplan_t")
        lines = ["ℹ️ <b>Контент-план · стан</b>", "Лише читаю таблицю — нічого в неї не пишу.", "",
                 f"Останнє завантаження: {dt.datetime.fromtimestamp(t, self.host.tz).strftime('%d.%m %H:%M') if t else 'ще не було'}"]
        lines += ["  " + esc(x) for x in self.store.get("cplan_status") or []]
        lines.append(f"Розібрано днів: {len(data)}, тем: {sum(len(v) for v in data.values())}")
        hist = self.store.get("cplan_hist") or []
        if hist:
            lines.append(f"Остання зміна: {dt.datetime.fromtimestamp(hist[-1]['t'], self.host.tz).strftime('%d.%m %H:%M')} · змін {len(hist[-1]['changes'])}")
        sheets = self.store.get("cplan_sheets") or []
        if sheets:
            lines += ["", "<b>Аркуші</b>:"] + [esc(x) for x in sheets[-40:]]
        return html_cut("\n".join(lines), 4000), [[("🔄 Завантажити зараз", self.cb("obcpr")), ("🔗 Змінити посилання", self.cb("obcu"))],
                                                    [("« Контент-план", self.cb("obcp"))]]

    DAYLIKE_RE = re.compile(r"(?i)\b(?:день|дня|тиждень|місяць|місячник|рік|свято|декада|ніч|година)\b")

    def cplan_sync(self, data):
        """📅 Дні з контент-плану → у календар (від учора на рік уперед), якщо такого дня там ще немає. Джерело — «контент-план»."""
        self._oinit()
        t0 = self.today()
        lo, hi = (t0 - dt.timedelta(days=1)).isoformat(), (t0 + dt.timedelta(days=400)).isoformat()
        added = 0
        for day, items in data.items():
            if not (lo <= day <= hi):
                continue
            d = dt.date.fromisoformat(day)
            have = [self._tkey(r["title"]) for r in self.obs_on(d)]
            for it in items:
                if it.get("where") or not self.DAYLIKE_RE.search(it["title"]):
                    continue  # статті для сайту/SMM — не дні
                k = self._tkey(it["title"])
                if any(k == h or (len(k) > 8 and (k in h or h in k)) for h in have):
                    continue
                if self.obs_add(it["title"], kind_of(it["title"]), day, day, 0, "Контент-план", it.get("src") or self.cplan_urls()[0]):
                    added += 1
                    have.append(k)
        return added

    def cplan_dir(self):
        from pathlib import Path
        p = Path(getattr(self, "dir", None) or ".") / "contentplan"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def cplan_diff(old, new):
        """Що змінилось у плані (від учора й далі): [("+"|"−", дата, тема)]."""
        since = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        key = lambda i: f"{i['title']} · ✍️ {i['author']}" if i.get("author") else i["title"]  # noqa: E731
        out = []
        for day in sorted(set(old) | set(new)):
            if day < since:
                continue
            a = [key(i) for i in old.get(day, [])]
            b = [key(i) for i in new.get(day, [])]
            out += [("+", day, x) for x in b if x not in a] + [("−", day, x) for x in a if x not in b]
        return out

    async def cplan_watch(self):
        """👁 Стежимо за таблицею: щопівгодини завантажуємо й повідомляємо адміністраторам, що змінилось."""
        await asyncio.sleep(90)
        while True:
            try:
                self._cplan_changes = []
                await self.ensure_cplan(force=True)
                ch = getattr(self, "_cplan_changes", [])
                if ch:
                    lines = ["🗓 <b>Контент-план змінився</b>", ""]
                    for sign, day, x in ch[:25]:
                        lines.append(f"{'➕' if sign == '+' else '➖'} {day[8:]}.{day[5:7]} · {esc(x)}")
                    if len(ch) > 25:
                        lines.append(f"… ще змін: {len(ch) - 25}")
                    lines.append(f'\n<a href="{esc(self.cplan_urls()[0])}">Відкрити таблицю</a>')
                    for uid in dict.fromkeys(self.host.admins()):
                        await self.host.notify(uid, "\n".join(lines)[:4000], [[("📋 Контент-план", self.cb("obcp"))]])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("контент-план: стеження", exc_info=True)
            await asyncio.sleep(1800)

    def cplan_bg(self):
        t = getattr(self, "_cplan_task", None)
        if t and not t.done():
            return

        async def go():
            try:
                await self.ensure_cplan()
            except Exception:  # noqa: BLE001
                log.info("контент-план: збій", exc_info=True)
        try:
            self._cplan_task = asyncio.get_running_loop().create_task(go())
        except RuntimeError:
            pass

    def cplan_on(self, d):
        return (self.store.get("cplan_data") or {}).get(d.isoformat(), [])

    @staticmethod
    def _tkey(t):
        return re.sub(r"[^a-zа-яіїєґ0-9]+", " ", clean_title(t).lower()).strip()

    def holidays_bg(self):
        """Звірка рухомих свят — у фоні (пошук займає час), не частіше ніж раз на 6 год."""
        if time.time() - (getattr(self, "_hol_t", 0) or 0) < 6 * 3600:
            return
        self._hol_t = time.time()

        async def go():
            try:
                await self.ensure_holidays()
            except Exception:  # noqa: BLE001
                log.info("свята: звірка не вдалася", exc_info=True)
        try:
            asyncio.get_running_loop().create_task(go())
        except RuntimeError:
            pass

    def clean_db_titles(self):
        """Разово: вичистити вже збережені назви (сутності, пояснення з Вікіпедії, крапки перед роком)."""
        if self.store.get("titles_clean_v") == 1:
            return
        self._oinit()
        with closing(self.store.db()) as con, con:
            for rid, t in con.execute("SELECT id, title FROM observances").fetchall():
                c = clean_title(t)
                if c != t and len(c) >= 5:
                    con.execute("UPDATE observances SET title=? WHERE id=?", (c[:160], rid))
        self.store.set("titles_clean_v", 1)

    def obs_add(self, title, kind, start, finish, yearly, source, url="", owner=0):
        """Нова тема. Затінена (прихована) раніше не повертається: ключ той самий, INSERT OR IGNORE."""
        self._oinit()
        title = clean_title(title)[:160]
        if len(title) < 5:
            return None
        key = normalize(title)[:80] + "|" + start + (f"|u{owner}" if owner else "")
        with closing(self.store.db()) as con, con:
            cur = con.execute("INSERT OR IGNORE INTO observances(title,kind,start,finish,yearly,source,url,added,key,owner) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (title, kind, start, finish, int(yearly), source[:120], url[:500], time.time(), key, int(owner or 0)))
            return cur.lastrowid if cur.rowcount else None

    def obs_set(self, oid, **kw):
        with closing(self.store.db()) as con, con:
            con.execute(f"UPDATE observances SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), int(oid)))

    def favs(self, chat):
        if not chat:
            return set()
        self._oinit()
        with closing(self.store.db()) as con:
            return {r[0] for r in con.execute("SELECT oid FROM obs_fav WHERE user=?", (int(chat),))}

    def fav_toggle(self, chat, oid):
        with closing(self.store.db()) as con, con:
            if con.execute("DELETE FROM obs_fav WHERE user=? AND oid=?", (int(chat), int(oid))).rowcount:
                return False
            con.execute("INSERT INTO obs_fav(user, oid) VALUES(?,?)", (int(chat), int(oid)))
            return True

    # ----- довідник дня (події, народились, іменини…)
    def db_add(self, md, cat, text, url="", owner=0):
        text = re.sub(r"\s+", " ", text or "").strip()[:220]
        if len(text) < 3:
            return None
        key = f"{cat}|{md}|{normalize(text)[:90]}" + (f"|u{owner}" if owner else "")
        with closing(self.store.db()) as con, con:
            cur = con.execute("INSERT OR IGNORE INTO daybook(md,cat,text,url,owner,added,key) VALUES(?,?,?,?,?,?,?)",
                              (md, cat, text, url[:300], int(owner or 0), time.time(), key))
            return cur.lastrowid if cur.rowcount else None

    def db_items(self, md, cat, chat=None, hidden=False):
        self._oinit()
        with closing(self.store.db()) as con:
            rs = [dict(r) for r in con.execute("SELECT * FROM daybook WHERE md=? AND cat=? AND hidden=? AND owner IN (0, ?) ORDER BY owner DESC, id",
                                               (md, cat, 1 if hidden else 0, int(chat or 0)))]
        return rs

    @staticmethod
    def db_imp(it):
        """❗ Важливість пункту: ручна позначка, інакше — автоматично за темою (екологія, здоров'я, агро, енергетика)."""
        v = it.get("imp")
        return int(v) if v is not None else (1 if DB.IMPORTANT_RE.search(it.get("text") or "") or DB.VERY_RE.search(it.get("text") or "") else 0)

    def db_line(self, it, notes=None):
        """Пункт списку дня: ❗ (якщо наша тема) + текст + джерело-гіперпосилання одразу після речення."""
        mark = "❗ " if self.db_imp(it) else ""
        src = ""
        if notes is not None:
            src = notes.mark(it.get("url"), _src_label(it.get("url"))) if str(it.get("url") or "").startswith("http") else \
                (" · <i>ваш запис</i>" if it.get("owner") else "")
        text = re.sub(r"\s*\([^)]*[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af][^)]*\)", "", it["text"])  # ієрогліфи в дужках — геть
        emo = DB.topic_emoji(text) + " " if it.get("cat") in ("born", "died", "history", "discover") else ""
        body = esc(text)
        if mark and notes is not None:  # ❗ наша тема — жирним (у списку ще й цитатою)
            body = f"<b>{body}</b>"
        return ("👤 " if it.get("owner") else "") + mark + emo + body + src

    def db_get(self, iid):
        with closing(self.store.db()) as con:
            r = con.execute("SELECT * FROM daybook WHERE id=?", (int(iid),)).fetchone()
        return dict(r) if r else None

    def db_set(self, iid, **kw):
        with closing(self.store.db()) as con, con:
            con.execute(f"UPDATE daybook SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), int(iid)))

    def can_edit(self, chat, owner):
        """Спільне редагують усі; особисте — лише той, хто його створив."""
        return not owner or int(owner) == int(chat or 0)

    def obs_on(self, d: dt.date, chat=None, hidden=False):
        """Теми, що припадають на дату d (дні цієї дати + тижні/місяці/роки, що тривають): спільні й особисті цієї людини.
        Порядок: ‼️ дуже важливі, ❗ важливі, ⭐ обрані, далі решта."""
        self._oinit()
        md, ymd = d.strftime("%m-%d"), d.isoformat()
        with closing(self.store.db()) as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM observances WHERE hidden=? AND owner IN (0, ?)", (1 if hidden else 0, int(chat or 0)))]
        fav = self.favs(chat)
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
        for r in out:
            r["imp_"] = DB.importance(r)
            r["fav"] = r["id"] in fav
        return sorted(out, key=lambda r: (order.get(r["kind"], 9), -r["imp_"], not r["fav"], r["title"]))

    def obs(self, oid):
        self._oinit()
        with closing(self.store.db()) as con:
            r = con.execute("SELECT * FROM observances WHERE id=?", (int(oid),)).fetchone()
        if not r:
            return None
        r = dict(r)
        r["imp_"] = DB.importance(r)
        return r

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
        url = f"https://uk.wikipedia.org/wiki/{page.replace(' ', '_')}"
        for title, flag in parse_wikitext(wt, bool(self.store.get("obs_countries"))):
            if self.obs_add(title, kind_of(title), d.strftime("%m-%d"), d.strftime("%m-%d"), 1, f"Вікіпедія {flag}", url):
                n += 1
        anchors = {"history": "#Події", "born": "#Народились", "died": "#Померли", "names": "#Іменини", "lore": ""}
        for cat, items in DB.parse_sections(wt).items():
            for txt in items[:80]:
                self.db_add(d.strftime("%m-%d"), cat, txt, url + anchors.get(cat, ""))
        return n

    async def prefetch(self, days=366, refresh_days=180):
        """Усі 366 дат року одразу (дні повторюються щороку, тож це покриває й наступні роки).
        Повторно дата оновлюється раз на refresh_days днів — щоб підхопити правки Вікіпедії."""
        done = self.store.get("obs_fetched_at") or {}
        if isinstance(done, list):
            done = {k: time.time() for k in done}
        if (self.store.get("obs_fetch_v") or 1) < FETCH_V:  # нові розділи — перечитуємо рік (старе не дублюється)
            done = {}
            self.store.set("obs_fetched_at", done)
            self.store.set("obs_fetch_v", FETCH_V)
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
    def sourced(self, r):
        """Чи є в теми джерело: посилання, підтвердження з інтернету або це запис людини. Без джерела — не показуємо."""
        if r.get("owner") or str(r.get("url") or "").startswith("http"):
            return True
        v = self.verified(r.get("id"))
        return bool(v and v.get("urls"))

    def obs_line(self, r, with_dates=False, link=False, notes=None):
        span = ""
        if with_dates or r["kind"] != "day":
            fmt = lambda x: ".".join(reversed(x.split("-")[-2:]))
            span = f" · {fmt(r['start'])}" + (f"–{fmt(r['finish'])}" if r["finish"] != r["start"] else "")
        imp = r.get("imp_", DB.importance(r))
        marks = DB.IMP_MARK.get(imp, "") + ("⭐" if r.get("fav") else "") + ("👤" if r.get("owner") else "")
        title = esc(clean_title(r["title"]))
        notes = notes if notes is not None else Notes()  # джерело — завжди, на кожному екрані
        if notes is not None:  # 📎 джерела — у зносці під списком; ✅/⚠️ — скільки незалежних ресурсів підтверджують
            v = self.verified(r.get("id")) if not r.get("owner") else None
            check = (f" ✅{v['n']}" if v["n"] >= 2 else " ⚠️1") if v and (v.get("urls") or r.get("url")) else ""
            if r.get("source") == "Контент-план":
                try:
                    src = " · " + self.cplan_ref(dt.date.fromisoformat(r["start"]) if len(r["start"]) == 10 else self.today())
                except ValueError:
                    src = " · " + self.cplan_ref(self.today())
            else:
                src = notes.mark(r.get("url"), _src_label(r.get("url"), r.get("source") or ""))
            src += "".join(notes.mark(u, host) for u, host in (v.get("urls") or [])[:2]) if v else ""
            return f"{KINDS.get(r['kind'], '•').split()[0]}{marks} {title}{span}{check}{src}"
        if link and str(r.get("url", "")).startswith("http"):
            title = f'<a href="{esc(r["url"])}">{title}</a>'
        return f"{KINDS.get(r['kind'], '•').split()[0]}{marks} {title}{span}"

    @staticmethod
    def _ahead(items, d):
        """Лише події від цього дня й далі (минулі — не «найближчим часом»)."""
        return [it for it in (items or []) if not it.get("date") or it["date"] >= d.isoformat()]

    def wiki_day_url(self, d):
        return f"https://uk.wikipedia.org/wiki/{d.day}_{MONTHS_GEN[d.month - 1]}"

    def sections(self, d, chat=None, with_links=True, notes=None, compact=False):
        """[(ключ, заголовок, [рядки])] — порожні списки пропускаємо. notes — зноски-джерела [n] до кожної частини."""
        md = d.strftime("%m-%d")
        out = []
        rs = self.obs_on(d, chat)
        unsrc = []
        if notes is not None:  # 🔎 без джерела нічого не показуємо — лише те, що можна перевірити
            unsrc = [r for r in rs if not self.sourced(r)]
            rs = [r for r in rs if self.sourced(r)]
            if unsrc:
                self.verify_bg(d)
        days = [r for r in rs if r["kind"] == "day"]
        ongoing = [r for r in rs if r["kind"] != "day"]
        plan = self.cplan_on(d) if notes is not None else []
        by_t = {}
        for it in plan:
            by_t.setdefault(self._tkey(it["title"]), []).append(it)
        used = set()

        def who(r):  # день уже є в плані — дописуємо, хто й де пише (без дубля в розділі плану)
            k = self._tkey(r["title"])
            hit = by_t.get(k) or next((v for kk, v in by_t.items() if len(kk) > 8 and (kk in k or k in kk)), None)
            if not hit:
                return ""
            used.update(id(i) for i in hit)
            return (" · ✍️ " + ", ".join(esc((i["author"] or "") + (f" ({i['where']})" if i["where"] else "")).strip() or "у плані" for i in hit)
                    + ("" if r.get("source") == "Контент-план" else " · " + self.cplan_ref(d)))
        lines = [self.obs_line(r, link=with_links, notes=notes) + (who(r) if plan else "") for r in days[:25]]
        if ongoing:  # скільки триває — лише в підзаголовку «Триває», не в загальному рядку
            lines += (["", f"<b>⏳ Триває · {len(ongoing)}</b>"] if days else []) + [self.obs_line(r, link=with_links, notes=notes) for r in ongoing[:12]]
        if notes is not None and lines:
            if any(self.verified(r["id"]) for r in days[:25] + ongoing[:12]):
                lines.append("<i>✅N — скільки незалежних ресурсів підтверджують (посилання — одразу після рядка) · ⚠️1 — лише одне джерело, звіряйте</i>")
        if lines:
            out.append(("days", f"📌 {'Сьогодні відзначається' if d == self.today() else 'Цього дня відзначається'} · {len(days)}" if days else f"⏳ Триває · {len(ongoing)}", lines))
        rest = [i for i in plan if id(i) not in used]
        if rest and out and out[-1][0] == "days":  # 📝 що ще в плані на цей день (статті для сайту, SMM) — частина «днів»
            out[-1][2].extend(["", f"<b>📝 Ще в контент-плані · {len(rest)}</b>"] + [
                f"• {esc(i['title'])}" + "".join(f" · {x}" for x in ((f"✍️ {esc(i['author'])}" if i["author"] else ""),
                                                                    (f"📍 {i['where']}" if i["where"] else "")) if x) + " · " + self.cplan_ref(d)
                for i in rest[:12]])
        for cat in DB.LIST_CATS:
            items = self.db_items(md, cat, chat)
            if items:
                cap = {"history": 12, "born": 10, "died": 8, "discover": 10}.get(cat, 15)
                if compact:  # у зведенні дня — коротко: прикмети й традиції по 3, решта по 5 (повністю — у «📝 Списки»)
                    cap = 3 if cat in ("lore", "trad") else 5
                items = sorted(items, key=lambda it: -self.db_imp(it))  # ❗ важливе за нашими темами — першим
                per_item = notes is not None and len({it.get("url") or "" for it in items[:cap]}) > 1  # різні джерела — зноска під кожним
                body = []
                for it in items[:cap]:
                    body.append(self.db_line(it, notes))

                if len(items) > cap:
                    body.append(self.more_line(cat, len(items) - cap, d))
                out.append((cat, DB.CATS[cat][0], [x for x in body if x]))
        x = self.store.get(f"dayx:{d.isoformat()}") or {}
        if notes is not None and x.get("trad"):  # 🪔 традиції з джерел (якщо в списку дня їх ще немає)
            same = x.get("trad_day") == d.isoformat()
            tday = dt.date.fromisoformat(x["trad_day"]) if x.get("trad_day") else d
            body = [f"• {esc(DX.tidy(it['text']))}" + notes.mark(it["url"], it["host"]) for it in x["trad"][:3 if compact else 8]]
            head_ = DB.CATS["trad"][0] + ("" if same else f" · на {tday.day} {MONTHS_GEN[tday.month - 1]} (не на сьогодні)")
            if not any(k == "trad" for k, _t, _l in out):
                out.insert(1 if out and out[0][0] == "days" else 0, ("trad", head_, body))
        elif notes is not None and not x.get("done") and not any(k == "trad" for k, _t, _l in out):
            self.extras_bg(d)
        astro = [] if compact else DB.astro_lines(d, self.host.tz)
        if compact:  # лише найближчі події (3); немає — розділу немає
            if notes is not None:
                astro = [(f"📅 {it['date'][8:10]}.{it['date'][5:7]} · " if it.get("date") else "• ") + esc(DX.tidy(it["text"], it.get("date") or "")) + notes.mark(it["url"], it["host"])
                         for it in self._ahead(x.get("astro"), d)[:3]]
        elif notes is not None:
            astro = [ln + notes.mark("https://gml.noaa.gov/grad/solcalc/", "NOAA") for ln in astro]  # розрахунок за формулами NOAA
            if x.get("aurora"):
                astro.append("🌌 " + esc(x["aurora"]["text"]) + notes.mark(x["aurora"]["url"], x["aurora"]["host"]))
            if x.get("astro"):
                astro += ["", "<b>Найближчим часом:</b>"] + [
                    (f"📅 {it['date'][8:10]}.{it['date'][5:7]} · " if it.get("date") else "• ") + esc(DX.tidy(it["text"], it.get("date") or "")) + notes.mark(it["url"], it["host"])
                    for it in self._ahead(x["astro"], d)[:10]]
        if astro:
            out.append(("astro", DB.CATS["astro"][0], astro))
        astrol = [] if compact else DB.astrol_lines(d)
        if compact:
            if notes is not None:
                astrol = [(f"📅 {it['date'][8:10]}.{it['date'][5:7]} · " if it.get("date") else "• ") + esc(DX.tidy(it["text"], it.get("date") or "")) + notes.mark(it["url"], it["host"])
                          for it in self._ahead(x.get("astrol"), d)[:3]]
        elif notes is not None:
            srcs = ["https://uk.wikipedia.org/wiki/Знаки_зодіаку", "https://uk.wikipedia.org/wiki/Місячний_календар", ""]
            astrol = [ln + (notes.mark(u, "Вікіпедія") if u else "") for ln, u in zip(astrol, srcs)]
            if x.get("astrol"):
                astrol = astrol[:-1] + [(f"📅 {it['date'][8:10]}.{it['date'][5:7]} · " if it.get("date") else "• ") + esc(DX.tidy(it["text"], it.get("date") or ""))
                                        + notes.mark(it["url"], it["host"]) for it in self._ahead(x["astrol"], d)[:8]] + astrol[-1:]
        if astrol:
            out.append(("astrol", DB.CATS["astrol"][0], astrol))
        g = (self.store.get("garden_cache3") or {}).get(self.garden_key(d))
        if g:
            body = []
            for it in (g if isinstance(g, list) else [])[:3 if compact else None]:
                body.append("• " + (f"<b>{esc(it['head'])}</b> — " if it.get("head") else "") + esc(it["text"])
                            + (notes.mark(it["url"], it["url"].split("/")[2].removeprefix("www.")) if notes is not None else ""))
            if body:
                out.append(("garden", DB.CATS["garden"][0], body))
        return out

    NOUNS = {"lore": ("прикмета", "прикмети", "прикмет"), "trad": ("традиція", "традиції", "традицій"),
             "names": ("ім'я", "імені", "імен"), "born": ("людина", "людини", "людей"), "died": ("людина", "людини", "людей"),
             "history": ("подія", "події", "подій"), "discover": ("відкриття", "відкриття", "відкриттів")}

    @staticmethod
    def plural(n, forms):
        return forms[0] if n % 10 == 1 and n % 100 != 11 else forms[1] if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else forms[2]

    # ↗️ «відкрити X»: переносить до кнопки X (вона блимає), а вже кнопка відкриває розділ
    JUMP = {  # ключ → (назва в «відкрити …», підпис кнопки)
        "days": ("Дні", "📅 Дні"), "up": ("Дні", "📅 Дні"), "wx": ("Погоду", "🌤 Погода"), "lore": ("Прикмети", "📜 Прикмети"),
        "garden": ("Сад-город", "🌱 Сад-город"), "astro": ("Астрономію", "🔭 Астрономія дня"), "astrol": ("Астрологію", "♈ Астрологія"),
        "trad": ("Традиції", "🪔 Традиції"), "born": ("Народились", "🎂 Народились"), "died": ("Померли", "🕯 Померли"),
        "history": ("Історію", "🏛 Історія"), "discover": ("Відкриття", "🔬 Відкриття"), "cplan": ("Контент-план", "📋 Контент-план"),
    }

    def jump_link(self, key, d):
        name = self.JUMP.get(key, ("", ""))[0]
        if not name:
            return ""
        try:
            return f'<a href="https://t.me/{self.host.bot().username}?start=obb_{key}_{d:%Y%m%d}">відкрити {name}</a>'
        except Exception:  # noqa: BLE001 — бот ще не ініціалізований (тести)
            return f"відкрити {name}"

    def cplan_ref(self, d):
        """Джерело «контент-план» → переносить до кнопки «📋 Контент-план» (блимає); там план у боті й посилання на оригінал."""
        try:
            return f'<a href="https://t.me/{self.host.bot().username}?start=obb_cplan_{d:%Y%m%d}">контент-план</a>'
        except Exception:  # noqa: BLE001
            return "контент-план"

    def more_line(self, cat, n, d):
        """«… ще 11 прикмет · відкрити Прикмети»."""
        what = f"{n} {self.plural(n, self.NOUNS.get(cat, ('пункт', 'пункти', 'пунктів')))}"
        return f"… ще {what} · {self.jump_link(cat, d)}"

    def jump_target(self, key, d):
        """Куди веде кнопка розділу: (підпис, callback)."""
        ymd = f"{d:%Y%m%d}"
        label = self.JUMP.get(key, ("", key))[1]
        if key in ("days", "up"):
            return label, self.cb("obc", d.strftime("%Y%m"))
        if key == "wx":
            return label, "m:pl:wx"
        if key == "cplan":
            return label, self.cb("obcp", ymd)
        if key in ("astro", "astrol", "garden"):
            return label, self.cb("obsec", f"{key}.{ymd}")
        return label, self.cb("obl", f"{key}.{ymd}")

    def jump_rows(self, d, hot=None, on=True):
        keys = ["days", "wx", "lore", "garden", "astro", "astrol", "trad", "history", "born", "died", "discover", "cplan"]
        btns = []
        for k in keys:
            label, cbd = self.jump_target(k, d)
            if k == hot or (hot == "up" and k == "days"):
                label = f"👉 {label} 👈" if on else f"▫️ {label} ▫️"
            btns.append((label, cbd))
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        return rows + [[("« Теми дня", self.cb("obh"))]]

    async def jump(self, chat, key, ymd):
        """Із «відкрити X» у зведенні: окреме повідомлення з кнопками розділів, потрібна блимає кілька разів."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        try:
            d = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        except ValueError:
            d = self.today()

        def mk(on):
            return InlineKeyboardMarkup([[InlineKeyboardButton(t, url=c[4:]) if c.startswith("url:") else InlineKeyboardButton(t, callback_data=c)
                                          for t, c in row] for row in self.jump_rows(d, key, on)])
        name = self.JUMP.get(key, ("", key))[1]
        bot = self.host.bot()
        msg = await bot.send_message(chat, f"👇 Кнопка <b>{esc(name)}</b> — натисніть, щоб відкрити ({d.day} {MONTHS_GEN[d.month - 1]}).",
                                     parse_mode="HTML", reply_markup=mk(True))
        for k in range(6):  # блимає: 👉…👈 ↔ ▫️…▫️
            await asyncio.sleep(0.6)
            try:
                await bot.edit_message_reply_markup(chat, msg.message_id, reply_markup=mk(k % 2 == 1))
            except Exception:  # noqa: BLE001
                break
        return msg

    def obs_section(self, chat, key, ymd):
        """Повний розділ дня (астрономія, астрологія, сад-город) — з джерелом після кожного рядка."""
        d = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        notes = Notes()
        for k, title, lines in self.sections(d, chat, notes=notes):
            if k == key:
                return html_cut(f"<b>{title} · {d.day} {MONTHS_GEN[d.month - 1]}</b>\n\n" + "\n".join(lines), 4000), self.jump_rows(d)
        return f"<b>{DB.CATS.get(key, (key,))[0]}</b>\n\nНа цей день у джерелах поки нічого.", self.jump_rows(d)

    @staticmethod
    def garden_key(d):
        return f"{d.month:02d}-{min(3, (d.day - 1) // 10 + 1)}"

    def upcoming(self, chat=None, days=30, min_imp=1):
        """Найближчі важливі дні (без сьогодні): [(дата, тема)]."""
        t0, out, seen = self.today(), [], set()
        for i in range(1, days + 1):
            x = t0 + dt.timedelta(days=i)
            for r in self.obs_on(x, chat):
                if r["kind"] == "day" and r["imp_"] >= min_imp and r["id"] not in seen and self.sourced(r):
                    seen.add(r["id"])
                    out.append((x, r))
        return out

    def fit(self, head, parts, tail, limit=3900):
        """Склеює частини, поки вміщається в одне повідомлення Telegram (HTML не ріжемо посередині)."""
        import html as _h
        vis = lambda x: len(_h.unescape(re.sub(r"<[^>]+>", "", x)))  # noqa: E731 — ліміт Telegram рахує видимий текст, не теги
        text = head
        for p in parts:
            if vis(text) + vis(p) + vis(tail) + 2 > limit or len(text) + len(p) + len(tail) > 30000:
                continue  # не вміщається — пропускаємо цю частину, коротші нижче ще можуть увійти
            text += "\n" + p
        return text + tail

    def obs_home(self, chat=None, open_="", weather=""):
        """Зведення дня: дні й погода першими, далі згорнуті списки; ✍️ теми, 📝 списки й ⚙️ меню — «гармоніками».
        Джерело кожного рядка — гіперпосиланням одразу після нього."""
        d = self.today()
        try:
            self.clean_db_titles()
        except Exception:  # noqa: BLE001
            log.info("очищення назв не вдалося", exc_info=True)
        try:
            self.ensure_church()  # ⛪ великі церковні свята — одразу, без пошуку
        except Exception:  # noqa: BLE001
            log.info("церковні свята не додано", exc_info=True)
        self.holidays_bg()
        self.cplan_bg()
        notes = Notes()
        head = f"📅 <b>Теми дня · {d.day} {MONTHS_GEN[d.month - 1]}, {WD[d.weekday()]}</b>\n"
        secs = [x for x in self.sections(d, chat, notes=notes, compact=True) if x[0] not in ("names", "born", "died")]  # іменини, народились, померли — лише в «📝 Списки дня»
        if callable(weather):  # погода сама додає свою зноску (Open-Meteo) у спільний список
            weather = weather(notes)
        order = ["days", "wx", "lore", "garden", "astro", "astrol", "trad", "cplan"]  # дні → погода → прикмети, городникам, небо, традиції
        secs = sorted(secs + ([("wx", "", [weather])] if weather else []),
                      key=lambda s_: order.index(s_[0]) if s_[0] in order else len(order))
        parts = []
        for key, title, lines in secs:
            link = self.jump_link(key, d)
            if key == "wx":  # посилання — всередині розгортного блоку погоди
                w = lines[0]
                parts.append(w.replace("</blockquote>", f"\n↗️ {link}</blockquote>", 1) if link and "</blockquote>" in w else w + (f"\n↗️ {link}" if link else ""))
            else:
                if link and not any("відкрити " in ln for ln in lines[-1:]):
                    lines = lines + [f"↗️ {link}"]
                parts.append(f"<b>{title}</b>\n<blockquote expandable>" + "\n".join(lines) + "</blockquote>")
        up = self.upcoming(chat, 30)
        tail = ""
        if up:
            more = (f"\n… ще {len(up) - 10} · " if len(up) > 10 else "\n↗️ ") + self.jump_link("up", d)
            tail += f"\n<b>⚠️ Найближчі важливі дні · {len(up)}</b>\n<blockquote expandable>" + "\n".join(
                f"{x.day:02d}.{x.month:02d} · {(x - d).days} дн. · {self.obs_line(r, notes=notes)}" for x, r in up[:10]) + more + "</blockquote>"
        on = self.store.get("obs_morning") is not False
        cov = self.obs_coverage()
        hm = self.store.get("obs_time") or "07:30"
        try:
            who = "@" + self.host.bot().username
        except Exception:  # noqa: BLE001 — бот ще не ініціалізований або тести
            who = "OrionAI"
        tail += (f"\n<i>Підготував {esc(who)} · {dt.datetime.now(self.host.tz) if self.host.tz else dt.datetime.now():%d.%m %H:%M}"
                 f" · джерела: Вікіпедія, канали, ваші записи · 📚 {cov}/366{'' if cov >= 366 else ' вантажиться'}"
                 f" · ⏰ {hm if on else 'вимк.'}</i>")
        notes.cur = []
        text = self.fit(head, parts, tail)
        open_ = open_ or ""
        t_open, m_open, l_open = "t" in open_, "m" in open_, "l" in open_
        flip = lambda flag: "".join(sorted(set(open_) ^ {flag})) or "-"
        rs = self.obs_on(d, chat)
        topics = rs[:8]
        rows = []
        if topics:
            rows.append([(f"✍️ Написати на тему · {len(topics)} {'▾' if t_open else '▸'}", self.cb("obh", flip("t")))])
            if t_open:
                rows += [[(f"✍️ {clean_title(r['title'])}", self.cb("ob", r["id"]))] for r in topics]  # скорочує kb() розумно
        rows.append([(f"📝 Списки дня {'▾' if l_open else '▸'}", self.cb("obh", flip("l")))])
        if l_open:
            ymd = d.strftime("%Y%m%d")
            btns = [("📌 Відзначається", self.cb("obl", f"days.{ymd}"))]
            for k in ("wx", "lore", "garden", "astro", "astrol", "trad", "names", "born", "died", "history", "discover", "cplan"):
                if k == "names":
                    btns.append(("👤 Іменини", self.cb("obl", f"names.{ymd}")))
                else:
                    btns.append(self.jump_target(k, d))
            rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]  # по 2 — щоб назви вміщались повністю
            rows.append([("⚠️ Важливі попереду", self.cb("obup")), ("🔔 Попередження", self.cb("obws"))])
        rows.append([(f"⚙️ Меню {'▾' if m_open else '▸'}", self.cb("obh", flip("m")))])
        if m_open:
            rows += [[("📅 Сьогодні", self.cb("obt", "day")), ("🗓 Тиждень", self.cb("obt", "week"))],
                     [("📆 Місяць", self.cb("obt", "month")), ("🎆 Рік", self.cb("obt", "year"))],
                     [("🗓 Календар", self.cb("obc", d.strftime("%Y%m"))), ("➕ Своя тема", self.cb("oba"))],
                     [("🌱 Поради саду", self.cb("obg")), ("📜 Усі прикмети з джерелами", self.cb("obj"))],
                     [("📋 Контент-план", self.cb("obcp")), ("🔄 Оновити з інтернету", self.cb("obu"))],
                     [(("⏰ " + hm) if on else "⏰ вимк.", self.cb("obm"))]]
        rows.append([("« Знайти й написати", self.cb("D"))])  # джерела — зносками під кожною частиною, не кнопками
        return text, rows

    # ----- 🔎 перевірка днів кількома незалежними джерелами
    def verified(self, rid):
        return (self.store.get("obs_verify") or {}).get(str(rid))

    async def verify_days(self, d, limit=20):
        """Кожен день зі списку дати шукаємо в інтернеті: скільки незалежних ресурсів (крім Вікіпедії, без .ru/.by/.ir)
        згадують його. Результат — «✅ підтверджено N джерелами» або «⚠️ лише одне джерело», посилання — у зносці.
        Кеш на рік (дні повторюються щороку)."""
        import websearch
        cache = self.store.get("obs_verify") or {}
        year, done = str(d.year), 0
        for r in self.obs_on(d):
            if r.get("owner") or done >= limit:  # дні, тижні, місяці, роки — усе звіряємо
                continue
            c = cache.get(str(r["id"]))
            if c and c.get("y") == year:
                continue
            done += 1
            title = re.sub(r"\s*\(\d{4}\)\s*$", "", r["title"])
            words = [w for w in re.findall(r"[а-яіїєґa-z]{4,}", title.lower()) if w not in ("день", "всесвітній", "міжнародний")]
            try:
                rows = await websearch.search_items(f"«{title}» {d.day} {MONTHS_GEN[d.month - 1]}" if r["kind"] == "day" else f"«{title}» {d.year}")
            except Exception:  # noqa: BLE001
                continue
            urls, hosts = [], set()
            for x in rows:
                host = (x["url"].split("/")[2] if "//" in x["url"] else "").removeprefix("www.")
                hay = (x.get("title", "") + " " + x.get("snippet", "")).lower()
                hit = sum(1 for w in words if w[:-2] in hay) >= max(1, round(len(words) * 0.6))
                if hit and host and "wikipedia.org" not in host and host not in hosts:
                    hosts.add(host)
                    urls.append([x["url"], host])
            base = 1 if str(r.get("url", "")).startswith("http") else 0
            if not base and urls:  # теми без джерела отримують перше знайдене незалежне джерело
                self.obs_set(r["id"], url=urls[0][0][:500])
                urls = urls[1:]
                base = 1
            cache[str(r["id"])] = {"y": year, "n": base + len(urls), "urls": urls[:4]}
            self.store.set("obs_verify", cache)
            await asyncio.sleep(0.5)

    async def obs_digest(self, chat):
        """Ранкове зведення: як головний екран, але з погодою одразу після днів і з довантаженими прикметами/порадами."""
        d = self.today()
        wx_text = ""
        wx = self.host.extra.get("plugin", lambda pid: None)("wx")
        if wx and wx.on():
            try:
                data = await wx.fetch([(la, lo) for _n, la, lo, _r in __import__("plugins.weather", fromlist=["UA_POINTS"]).UA_POINTS])
                extra = await wx.countries_text(chat) if hasattr(wx, "countries_text") else ""
                if wx.ua_day(data, 0):
                    times = (data[0].get("daily") or {}).get("time") or []

                    def wx_text(notes, data=data, extra=extra, times=times):  # 3 дні: сьогодні, завтра, післязавтра
                        days_ = [f"<b>{wx.day_title(times[i], i)}</b>\n" + wx.ua_day(data, i, notes, flush=False)
                                 for i in range(min(3, len(times))) if wx.ua_day(data, i)]
                        return ("<b>🌤 Погода по Україні · 3 дні</b>\n<blockquote expandable>" + "\n\n".join(days_)
                                + "</blockquote>" + (("\n" + extra) if extra else ""))
            except Exception:  # noqa: BLE001
                log.info("obs digest: weather failed", exc_info=True)
        await self.ensure_lore(d)
        await self.ensure_garden(d)
        self.verify_bg(d)  # перевірка джерелами — у фоні, щоб зведення відкривалось одразу
        self.extras_bg(d)  # традиції, астрономія, астрологія — теж у фоні
        return self.obs_home(chat, weather=wx_text)

    def verify_bg(self, d):
        t = getattr(self, "_verify_task", None)
        if t and not t.done():
            return

        async def go():
            try:
                await self.verify_days(d)
            except Exception:  # noqa: BLE001
                log.info("перевірка днів не вдалася", exc_info=True)
        try:
            self._verify_task = asyncio.get_running_loop().create_task(go())
        except RuntimeError:
            pass

    async def ensure_lore(self, d):
        """Народні прикмети: розділ Вікіпедії; якщо там порожньо — дослівні уривки з українських сайтів із посиланнями
        (через плагін погоди). ШІ нічого не вигадує."""
        md = d.strftime("%m-%d")
        self._oinit()
        if self.store.get("lore_cache_v") != 4:  # згенероване ШІ й «сирі» уривки раніше — прибрати й зібрати наново
            self.store.set("lore_cache", {})
            self.store.set("garden_cache", {})
            with closing(self.store.db()) as con, con:
                con.execute("DELETE FROM daybook WHERE cat='lore' AND owner=0 AND url NOT LIKE '%wikipedia.org%'")
                for rid, txt in con.execute("SELECT id, text FROM daybook WHERE cat='lore' AND owner=0").fetchall():
                    if DB.NOT_LORE_RE.match(txt or ""):  # дні й свята, що потрапили в прикмети
                        con.execute("DELETE FROM daybook WHERE id=?", (rid,))
            self.store.set("lore_cache_v", 4)
        if self.db_items(md, "lore"):
            return
        wx = self.host.extra.get("plugin", lambda pid: None)("wx")
        if not wx:
            return
        try:
            items = await wx.lore(d)
        except Exception:  # noqa: BLE001
            return
        for it in items or []:
            if isinstance(it, dict) and str(it.get("url", "")).startswith("http"):
                self.db_add(md, "lore", it["text"], it["url"])

    @staticmethod
    def clean_snip(text, limit=420):
        """Уривок зі сторінки → охайний текст: без Markdown, «[…]», «Пов'язані теми», обірваних речень."""
        t = re.sub(r"#+\s*|\*\*|__|\[\s*(?:\.\.\.|…)\s*\]|\[[^\]]*\]\([^)]*\)", " ", text or "")
        t = re.split(r"(?i)пов['’]язані теми|читайте також|джерело:|фото:", t)[0]
        t = re.sub(r"\s+", " ", t).strip(" –—-")
        sents = re.findall(r"[^.!?…]+[.!?]", t)
        out = ""
        for x in sents:
            if len(out) + len(x) > limit:
                break
            out += x
        return out.strip() or ""

    async def ensure_garden(self, d, force=False):
        """🌱 Поради саду, городу, господарству й тваринам на цю пору — з українських джерел, GreenPost — першим (але не лише він).
        Уривки скорочуємо й пояснюємо (що робити, навіщо, коли) — лише фактами з уривка; посилання — після кожного пункту."""
        key = self.garden_key(d)
        cache = self.store.get("garden_cache3") or {}
        got = cache.get(key)
        if isinstance(got, list) and got and not force:
            return got
        part = ("на початку", "в середині", "наприкінці")[int(key[-1]) - 1]
        mon = MONTHS_NOM[d.month - 1]
        green, other = [], []
        try:
            import websearch
            for q in (f"site:greenpost.ua город сад {mon}", f"greenpost.ua що робити на городі {mon}",
                      f"що робити в саду і на городі {part} {mon}", f"роботи на присадибній ділянці {mon} поради"):
                for r in await websearch.search_items(q):
                    snip = self.clean_snip(r.get("snippet") or "")
                    url = r.get("url") or ""
                    if (len(snip) < 60 or len(re.findall(r"[іїєґ]", snip, re.I)) < 3 or any(url == x["url"] for x in green + other)
                            or re.search(r"youtube\.|youtu\.be|facebook\.|instagram\.|tiktok\.", url)):  # відео й соцмережі — не текстові джерела
                        continue
                    (green if "greenpost.ua" in url else other).append({"text": snip, "url": url, "title": (r.get("title") or "")[:80]})
        except Exception:  # noqa: BLE001
            log.info("поради саду: пошук не вдався", exc_info=True)
        items = green[:2] + other[: 6 - min(2, len(green))]  # GreenPost — першим, але завжди є й інші джерела
        if items:
            items = await self.adapt_tips(items, mon)
            cache[key] = items
            self.store.set("garden_cache3", cache)
        return items

    async def adapt_tips(self, items, mon):
        """ШІ скорочує й пояснює кожен уривок: «Що робити — навіщо, коли». Лише факти з уривка; не вдалося — охайний уривок."""
        src = "\n".join(f"{i}. {it['text']}" for i, it in enumerate(items, 1))
        prompt = ("Ось уривки з українських сайтів про роботи в саду й на городі (" + mon + "). Для КОЖНОГО уривку напиши один рядок "
                  "у форматі «N. Коротка назва дії — пояснення: що саме зробити, навіщо й коли (якщо це є в уривку)». "
                  "До 200 символів, українською, своїми словами, без Markdown. Використовуй ЛИШЕ факти з уривку, нічого не додавай. "
                  "Якщо в уривку немає корисної поради або вона повторює пораду з попереднього уривку — напиши «N. —».\n\n" + src)
        try:
            ask = getattr(self.host, "ask_ai", None)
            text, _m = await ask(prompt, label="🌱 Поради городникам") if ask else ("", None)
        except Exception:  # noqa: BLE001
            text = ""
        got = dict(re.findall(r"(?m)^\s*(\d+)[.)]\s*(.+?)\s*$", text or ""))
        out = []
        for i, it in enumerate(items, 1):
            line = re.sub(r"[*_#`]", "", got.get(str(i), "")).strip()
            if line in ("—", "-"):
                continue
            if line and " — " in line:
                head, _, tail = line.partition(" — ")
                out.append({**it, "head": head.strip()[:60], "text": tail.strip()[:220]})
            else:
                out.append(it)
        uniq = []
        for it in out:  # одна й та сама порада двічі — лишаємо першу (GreenPost іде першим)
            w = self._stems(it.get("head", "") + " " + it["text"])
            if any(len(w & self._stems(u.get("head", "") + " " + u["text"])) >= 0.5 * max(1, min(len(w), len(self._stems(u.get("head", "") + " " + u["text"])))) for u in uniq):
                continue
            uniq.append(it)
        return uniq[:3]

    @staticmethod
    def _stems(text):
        return {w[:5] for w in re.findall(r"[а-яіїєґ']{4,}", (text or "").lower())}

    def obs_list(self, chat, cat, ymd, page=0):
        """📝 Редагований список дня: торкніться пункту — змінити, затінити, зробити особистим."""
        d = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        md = d.strftime("%m-%d")
        title = "📌 Відзначається" if cat == "days" else DB.CATS[cat][0]
        notes = Notes()  # джерело — посиланням одразу після кожного пункту
        if cat == "days":
            rs_ = self.obs_on(d, chat)
            if any(not self.sourced(r) for r in rs_):
                self.verify_bg(d)  # без джерела не показуємо — шукаємо його у фоні
            items = [(r["id"], self.obs_line(r, notes=notes)) for r in rs_ if self.sourced(r)]
            hidden = len(self.obs_on(d, chat, hidden=True))
        else:
            items = [(it["id"], self.db_line(it, notes)) for it in self.db_items(md, cat, chat)]
            hidden = len(self.db_items(md, cat, chat, hidden=True))
        per = 8
        page = max(0, min(int(page or 0), max(0, (len(items) - 1) // per)))
        chunk = items[page * per:(page + 1) * per]
        lines = [f"<b>{title} · {d.day} {MONTHS_GEN[d.month - 1]}</b>", ""]
        lines += [(f"<blockquote>{page * per + i + 1}. {t}</blockquote>" if ("❗" in t[:8] or "‼️" in t[:12]) else f"{page * per + i + 1}. {t}")
                  for i, (_id, t) in enumerate(chunk)] or ["Порожньо."]  # ❗ важливе — окремою цитатою
        lines += ["", "Торкніться номера, щоб змінити. Затінене не повертається при оновленні; додане — повторюється щороку. "
                  "Спільне редагують усі, 👤 особисте — лише ви."]
        open_ = "ob" if cat == "days" else "obi"
        nums = [(str(page * per + i + 1), self.cb(open_, _id)) for i, (_id, _t) in enumerate(chunk)]
        rows = [nums[i:i + 4] for i in range(0, len(nums), 4)]
        nav = []
        if page > 0:
            nav.append(("◀️", self.cb("obl", f"{cat}.{ymd}", page - 1)))
        if (page + 1) * per < len(items):
            nav.append(("▶️", self.cb("obl", f"{cat}.{ymd}", page + 1)))
        if nav:
            rows.append(nav)
        rows.append([("➕ Спільне", self.cb("obn", f"{cat}.{ymd}", "0")), ("➕ Особисте", self.cb("obn", f"{cat}.{ymd}", "1"))])
        if hidden:
            rows.append([(f"🌑 Затінені · {hidden}", self.cb("obs", f"{cat}.{ymd}"))])
        rows.append([("« Теми дня", self.cb("obh", "l"))])
        from footnotes import html_cut
        return html_cut("\n".join(lines), 4000), rows

    def obs_shadow(self, chat, cat, ymd):
        d = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        if cat == "days":
            items = [(r["id"], r["title"]) for r in self.obs_on(d, chat, hidden=True)]
        else:
            items = [(it["id"], it["text"]) for it in self.db_items(d.strftime("%m-%d"), cat, chat, hidden=True)]
        lines = ["🌑 <b>Затінені</b> — не показуються й не повертаються при оновленні з інтернету.", ""]
        lines += [f"{i + 1}. {esc(t)}" for i, (_id, t) in enumerate(items[:20])] or ["Порожньо."]
        rows = [[(f"↩️ {i + 1}. {t[:30]}", self.cb("obr", f"{'o' if cat == 'days' else 'd'}{_id}", ymd))] for i, (_id, t) in enumerate(items[:20])]
        rows.append([("« Список", self.cb("obl", f"{cat}.{ymd}"))])
        return "\n".join(lines)[:4000], rows

    def db_card(self, chat, iid):
        it = self.db_get(iid)
        if not it:
            return "Не знайдено.", [[("« Теми дня", self.cb("obh"))]]
        m, dd = int(it["md"][:2]), int(it["md"][3:])
        ymd = f"{self.today().year}{m:02d}{dd:02d}"
        text = (f"{DB.CATS.get(it['cat'], ('•',))[0]} · {dd} {MONTHS_GEN[m - 1]}\n\n{esc(it['text'])}\n\n"
                + ("👤 Особисте — бачите лише ви." if it["owner"] else "👥 Спільне — бачать і редагують усі."))
        rows = []
        if self.can_edit(chat, it["owner"]):
            rows.append([("✏️ Змінити", self.cb("obe", f"d{iid}")), ("🌑 Затінити", self.cb("obx", f"d{iid}"))])
            auto = it.get("imp") is None
            rows.append([(("❗ Важливе" if self.db_imp(it) else "▫️ Звичайне") + (" (авто)" if auto else "") + " — змінити", self.cb("obq", iid))])
        if not it["owner"]:
            rows.append([("👤 Копію в особисті", self.cb("obp", f"d{iid}"))])
        if it["url"].startswith("http"):
            rows.append([("🔗 Джерело", "url:" + it["url"])])
        rows.append([("« Список", self.cb("obl", f"{it['cat']}.{ymd}"))])
        return text, rows

    def obs_upcoming(self, chat):
        d = self.today()
        up = self.upcoming(chat, 60)
        lines = [f"⚠️ <b>Важливі дні попереду · {len(up)} за 60 днів</b>", "‼️ дуже важливі · ❗ важливі", ""]
        lines += [f"{x.day:02d}.{x.month:02d} {WD[x.weekday()]} · через {(x - d).days} дн. · {self.obs_line(r, link=True)}" for x, r in up[:40]] or ["Немає."]
        rows = [[("🔔 Коли попереджати", self.cb("obws"))], [("« Теми дня", self.cb("obh", "l"))]]
        return "\n".join(lines)[:4000], rows

    def warn_cfg(self, chat):
        c = self.store.get(f"obs_warn:{chat}") or {}
        return {k: list(c.get(k, v)) for k, v in WARN_DEFAULT.items()}

    def obs_warn_screen(self, chat):
        c = self.warn_cfg(chat)
        opts = {"1": [0, 1, 2, 3, 7], "2": [1, 2, 3, 4, 7, 14]}
        lines = ["🔔 <b>Попередження про важливі дні</b>", "",
                 "Позначте, за скільки днів нагадувати (можна кілька). 0 — у сам день (окремо від ранкового зведення).",
                 f"❗ Важливі: {', '.join(map(str, sorted(c['1']))) or 'ні'} · ‼️ Дуже важливі: {', '.join(map(str, sorted(c['2']))) or 'ні'}",
                 "Гучно чи тихо — 🎛 Поведінка → 🔔 Сповіщення → «Попередження»."]
        rows = []
        for lvl, mark in (("1", "❗"), ("2", "‼️")):
            rows.append([((("✅" if n in c[lvl] else "▫️") + f"{mark}{n}"), self.cb("obwt", lvl, n)) for n in opts[lvl]])
        rows.append([("« Важливі попереду", self.cb("obup"))])
        return "\n".join(lines), rows

    def obs_period(self, what, chat=None):
        d = self.today()
        if what == "day":
            return self.obs_home(chat)
        lines, rows = [], []
        if what == "week":
            lines.append("🗓 <b>Найближчі 7 днів</b>")
            for i in range(7):
                x = d + dt.timedelta(days=i)
                items = [r for r in self.obs_on(x, chat) if r["kind"] == "day"]
                lines.append(f"\n<b>{x.day} {MONTHS_GEN[x.month - 1]}, {WD[x.weekday()]}</b>")
                lines += [self.obs_line(r) for r in items[:8]] or ["—"]
            weeks = {r["id"]: r for i in range(7) for r in self.obs_on(d + dt.timedelta(days=i), chat) if r["kind"] == "week"}
            if weeks:
                lines += ["", "<b>Тижні:</b>"] + [self.obs_line(r, True) for r in weeks.values()]
        elif what == "month":
            last = calendar.monthrange(d.year, d.month)[1]
            lines.append(f"📆 <b>{MONTHS[d.month - 1]} {d.year}</b>")
            months = {r["id"]: r for r in self.obs_on(d, chat) if r["kind"] in ("month", "week")}
            if months:
                lines += ["<b>Місячники й тижні:</b>"] + [self.obs_line(r, True) for r in months.values()]
            for day in range(1, last + 1):
                x = dt.date(d.year, d.month, day)
                items = [r for r in self.obs_on(x, chat) if r["kind"] == "day"]
                if items:
                    mark = "▶️ " if x == d else ("· " if x < d else "")
                    lines.append(f"\n{mark}<b>{day}</b>: " + "; ".join(DB.IMP_MARK[r["imp_"]] + esc(r["title"][:60]) for r in items[:4])
                                 + (f" (+{len(items) - 4})" if len(items) > 4 else ""))
        else:
            lines.append(f"🎆 <b>{d.year}</b>")
            years = {r["id"]: r for r in self.obs_on(d, chat) if r["kind"] == "year"}
            lines += ["<b>Роки тем:</b>"] + ([self.obs_line(r) for r in years.values()] or ["—"])
            lines.append("\n<b>Місячники за рік:</b>")
            for m in range(1, 13):
                ms = {r["id"]: r for r in self.obs_on(dt.date(d.year, m, 15), chat) if r["kind"] == "month"}
                if ms:
                    lines.append(f"{MONTHS[m - 1]}: " + "; ".join(esc(r["title"][:60]) for r in ms.values()))
        rows.append([("🗓 Календар", self.cb("obc", d.strftime("%Y%m"))), ("« Теми дня", self.cb("obh"))])
        return "\n".join(lines)[:4000], rows

    def obs_calendar(self, ym, chat=None):
        y, m = int(ym[:4]), int(ym[4:6])
        today = self.today()
        rows = [[(w, "m:noop") for w in WD]]
        week = [(" ", "m:noop")] * dt.date(y, m, 1).weekday()
        for day in range(1, calendar.monthrange(y, m)[1] + 1):
            x = dt.date(y, m, day)
            rs = self.obs_on(x, chat)
            top = max((r["imp_"] for r in rs if r["kind"] == "day"), default=-1)
            mark = "‼️" if top == 2 else "❗" if top == 1 else ("⭐" if any(r["fav"] for r in rs) else "•" if rs else "")
            label = (f"[{day}]" if x == today else str(day)) + mark
            week.append((label, self.cb("obd", x.strftime("%Y%m%d"))))
            if len(week) == 7:
                rows.append(week)
                week = []
        if week:
            rows.append(week + [(" ", "m:noop")] * (7 - len(week)))
        prev = (dt.date(y, m, 1) - dt.timedelta(days=1)).strftime("%Y%m")
        nxt = (dt.date(y, m, 28) + dt.timedelta(days=5)).strftime("%Y%m")
        rows.append([("◀️ " + MONTHS[int(prev[4:]) - 1], self.cb("obc", prev)), (MONTHS[int(nxt[4:]) - 1] + " ▶️", self.cb("obc", nxt))])
        rows.append([("➕ Свій день", self.cb("oba")), ("« Теми дня", self.cb("obh"))])
        return (f"🗓 <b>{MONTHS[m - 1]} {y}</b>\n‼️ дуже важливий · ❗ важливий · ⭐ обраний · • є теми · [число] — сьогодні.\n"
                "Торкніться дня — теми, списки, додавання й редагування."), rows

    def obs_day(self, ymd, chat=None):
        x = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        rs = self.obs_on(x, chat)
        when = "минуло" if x < self.today() else ("сьогодні" if x == self.today() else f"через {(x - self.today()).days} дн.")
        lines = [f'📅 <b><a href="{self.wiki_day_url(x)}">{x.day} {MONTHS_GEN[x.month - 1]}</a> {x.year}</b> · {when}', ""]
        lines += [self.obs_line(r, link=True) for r in rs] or ["Тем немає."]
        rows = [[(f"✍️ {clean_title(r['title'])}", self.cb("ob", r["id"]))] for r in rs[:6]]
        rows.append([("📝 Список дня", self.cb("obl", f"days.{ymd}")), ("➕ До цього дня", self.cb("obn", f"days.{ymd}", "0"))])
        rows.append([("🗓 Календар", self.cb("obc", ymd[:6])), ("« Теми дня", self.cb("obh"))])
        return "\n".join(lines)[:4000], rows

    def obs_card(self, oid, chat=None):
        r = self.obs(oid)
        if not r:
            return "Тему не знайдено.", [[("« Теми дня", self.cb("obh"))]]
        r["fav"] = int(oid) in self.favs(chat)
        why, since = DB.why_since(r)
        text = (f"{self.obs_line(r, True, link=True)}\n\n{KINDS.get(r['kind'])} · {'щороку' if r['yearly'] else 'разово'}"
                f" · {DB.IMP_NAMES[r['imp_']]}" + (" (авто)" if r.get("imp") is None and r["imp_"] else "")
                + f"\n📍 Чому сьогодні: {esc(why) or '—'}" + (f"\n🕰 Відзначають з {esc(since)} р." if since else "")
                + f"\n{'👤 Особиста — бачите лише ви' if r.get('owner') else '👥 Спільна — редагують усі'}"
                + f"\nДжерело: {esc(r['source'])}")
        edit = self.can_edit(chat, r.get("owner"))
        rows = [[("📱 Коротко", self.cb("obw", oid, "short")), ("📰 Новина", self.cb("obw", oid, "news")), ("📚 Велика", self.cb("obw", oid, "big"))],
                [(("⭐ В обраному" if r["fav"] else "☆ В обране"), self.cb("obf", oid)), ("❗ Важливість", self.cb("obv", oid))]]
        if edit:
            rows.append([("✏️ Назва теми", self.cb("obe", f"o{oid}")), ("📍 Чому/відколи", self.cb("oby", oid))])
        rows.append([("➕ До зведення", self.cb("obz", oid))] + ([("🌑 Затінити", self.cb("obx", f"o{oid}"))] if edit else []))
        if not r.get("owner"):
            rows.append([("👤 Копію в особисті", self.cb("obp", f"o{oid}"))])
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
        __import__("notify").KIND.set("morning")  # 🔔 тип сповіщень для режимів людини
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
                    today = now.date().isoformat()
                    if (self.store.get("obs_morning") is not False and now.strftime("%H:%M") == hm and sent_day != now.date()
                            and self.store.get("obs_sent") != today):  # один раз на день, навіть після перезапуску
                        sent_day = now.date()
                        self.store.set("obs_sent", today)
                        await self.send_digest()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("observances loop")
            await asyncio.sleep(30)

    async def send_digest(self):
        """Кожному — своє зведення (зі своїми особистими темами й обраним) і, окремо, попередження про важливі дні."""
        import notify
        d = self.today()
        try:  # ранкова розсилка — уже з перевіркою днів кількома джерелами
            await self.verify_days(d)
        except Exception:  # noqa: BLE001
            log.info("перевірка днів не вдалася", exc_info=True)
        for uid in dict.fromkeys(self.host.admins()):
            try:
                with notify.kind("morning"):
                    if notify.wanted(uid, "morning"):
                        text, rows = await self.obs_digest(uid)
                        await self.host.notify(uid, "🌅 " + text, rows)
                with notify.kind("warn"):
                    if not notify.wanted(uid, "warn"):
                        continue
                    cfg = self.warn_cfg(uid)
                    soon = []
                    for i in sorted({n for v in cfg.values() for n in v}):
                        x = d + dt.timedelta(days=i)
                        for r in self.obs_on(x, uid):
                            if r["kind"] == "day" and r["imp_"] >= 1 and i in cfg[str(r["imp_"])]:
                                soon.append((i, x, r))
                    if soon:
                        lines = ["⚠️ <b>Попередження про важливі дні</b>", ""]
                        lines += [f"{'Сьогодні' if i == 0 else f'Через {i} дн.'} · {x.day:02d}.{x.month:02d} · {self.obs_line(r, link=True)}" for i, x, r in soon]
                        await self.host.notify(uid, "\n".join(lines)[:4000],
                                               [[("⚠️ Важливі попереду", self.cb("obup")), ("🔔 Коли попереджати", self.cb("obws"))]])
            except Exception:  # noqa: BLE001
                log.exception("obs digest for %s", uid)

    # ----- маршрути
    @staticmethod
    def _cy(x):
        cat, _, ymd = (x or "").partition(".")
        return cat or "days", ymd or dt.date.today().strftime("%Y%m%d")

    async def obs_route(self, chat, a, x, y):
        if a == "obh":
            return None, self.obs_home(chat, "" if x in ("", "-") else x)
        if a == "obt":
            return None, self.obs_period(x, chat)
        if a == "obc":
            return None, self.obs_calendar(x, chat)
        if a == "obd":
            return None, self.obs_day(x, chat)
        if a == "ob":
            return None, self.obs_card(x, chat)
        if a == "obi":
            return None, self.db_card(chat, x)
        if a == "obq":  # ❗ ручна позначка: авто → важливе → звичайне → авто
            it = self.db_get(x)
            if it and self.can_edit(chat, it["owner"]):
                nxt = {None: 1, 1: 0, 0: None}[it.get("imp") if it.get("imp") in (0, 1) else None]
                with closing(self.store.db()) as con, con:
                    con.execute("UPDATE daybook SET imp=? WHERE id=?", (nxt, int(x)))
            return None, self.db_card(chat, x)
        if a == "obl":
            return None, self.obs_list(chat, *self._cy(x), y or 0)
        if a == "obs":
            return None, self.obs_shadow(chat, *self._cy(x))
        if a == "obup":
            return None, self.obs_upcoming(chat)
        if a == "obcp":
            return None, self.cplan_screen(chat, x)
        if a == "obcps":
            return None, self.cplan_status(chat)
        if a == "obcu":
            if not self.host.is_admin(chat):
                return "Лише адміністратори", None
            self.ask(chat, "obcu")
            return None, ("🔗 Надішліть посилання на контент-план (Google Таблицю або файл у Google Диску).", [[("« Стан", self.cb("obcps"))]])
        if a in ("obce", "obca", "obcd", "obcw", "obcx"):
            return await self.cplan_edit_route(chat, a, x)
        if a == "obsec":
            k, _, ymd = (x or "").partition(".")
            return None, self.obs_section(chat, k, ymd or self.today().strftime("%Y%m%d"))
        if a == "obcpr":
            try:
                await self.ensure_cplan(force=True)
            except Exception as exc:  # noqa: BLE001
                self.store.set("cplan_status", [f"{type(exc).__name__}: {str(exc)[:120]}"])
            return None, self.cplan_status(chat)
        if a == "obws":
            return None, self.obs_warn_screen(chat)
        if a == "obwt":
            c = self.warn_cfg(chat)
            n = int(y or 0)
            c[x] = sorted(set(c.get(x, [])) ^ {n})
            self.store.set(f"obs_warn:{chat}", c)
            return None, self.obs_warn_screen(chat)
        if a == "obw":
            self._bg(self.write_observance(chat, int(x), y))
            return "✍️ Пишу…", ("✍️ <b>Пишу на тему дня</b>\nУ чаті з ботом видно хід роботи (крок і секунди), потім прийде чернетка. Якщо ШІ відмовиться — «📋 Промт для іншого ШІ» або «🔁 Іншою моделлю».", [[("« Теми дня", self.cb("obh"))]])
        if a == "obz":
            r = self.obs(x)
            n = self.add_collected(chat, "text", f"📅 Тема дня: {r['title']}", r["source"], r["url"])
            return f"➕ У зведенні: {n}", self.obs_card(x, chat)
        if a == "obf":
            on = self.fav_toggle(chat, x)
            return ("⭐ В обраному" if on else "Прибрано з обраного"), self.obs_card(x, chat)
        if a == "obv":  # важливість: авто → звичайна → важлива → дуже важлива → авто
            r = self.obs(x)
            cur = r.get("imp")
            nxt = {None: 0, 0: 1, 1: 2, 2: None}[cur if cur in (0, 1, 2) else None]
            self.obs_set(x, imp=nxt)
            r = self.obs(x)
            return f"Важливість: {DB.IMP_NAMES[r['imp_']]}" + (" (авто)" if nxt is None else ""), self.obs_card(x, chat)
        if a in ("obx", "obr", "obp", "obe"):
            kind, iid = (x[:1], x[1:]) if x[:1] in ("o", "d") else ("o", x)
            item = self.obs(iid) if kind == "o" else self.db_get(iid)
            if not item:
                return "Не знайдено", self.obs_home(chat)
            if a == "obp":  # копія в особисті
                if kind == "o":
                    new = self.obs_add(item["title"], item["kind"], item["start"], item["finish"], item["yearly"], "особиста копія", item["url"], owner=chat)
                    return "👤 Додано в особисті", self.obs_card(new, chat) if new else self.obs_card(iid, chat)
                new = self.db_add(item["md"], item["cat"], item["text"], item["url"], owner=chat)
                return "👤 Додано в особисті", self.db_card(chat, new or iid)
            if not self.can_edit(chat, item.get("owner")):
                return "Це особисте — змінює лише автор", self.obs_home(chat)
            if a == "obe":
                self.ask(chat, "obe", [kind, int(iid)])
                return None, ("✏️ Напишіть новий текст.", [[("✖️ Скасувати", "m:cancel")]])
            hide = 1 if a == "obx" else 0
            (self.obs_set if kind == "o" else self.db_set)(iid, hidden=hide)
            if a == "obr":
                cat = "days" if kind == "o" else item["cat"]
                return "↩️ Повернуто", self.obs_shadow(chat, cat, y or self.today().strftime("%Y%m%d"))
            return "🌑 Затінено — не повернеться при оновленні", self.obs_home(chat, "l")
        if a == "oby":
            self.ask(chat, "oby", int(x))
            return None, ("📍 Чому саме цього дня й відколи? Формат: <code>пояснення | рік</code>\nНапр.: "
                          "<code>щороку в другий вівторок червня | 2004</code>", [[("✖️ Скасувати", "m:cancel")]])
        if a == "obn":
            cat, ymd = self._cy(x)
            self.ask(chat, "obn", [cat, ymd, y == "1"])
            who = "👤 особисте (бачите лише ви)" if y == "1" else "👥 спільне (бачать і редагують усі)"
            return None, (f"➕ Новий пункт, {who}. Повторюватиметься щороку цього дня.\nНапишіть текст"
                          + (" (для днів можна додати «| !» — важливий, «| !!» — дуже важливий)." if cat == "days" else "."),
                          [[("✖️ Скасувати", "m:cancel")]])
        if a == "obg":
            items = await self.ensure_garden(self.today(), force=True)
            body = "\n\n".join(f"• {esc(it['text'])} · <a href=\"{esc(it['url'])}\">{esc(it['url'].split('/')[2].removeprefix('www.'))}</a>"
                                for it in items) or "Порад у джерелах зараз не знайшлося — вигадувати їх бот не буде. Спробуйте згодом."
            return None, ("🌱 <b>Сад, город, господарство, тварини</b>\n\n" + body
                          + "\n\n<i>Дослівні уривки з українських сайтів; звіряйте з місцевими умовами.</i>",
                          [[("🔄 Пошукати ще", self.cb("obg")), ("« Теми дня", self.cb("obh"))]])
        if a == "obj":
            d = self.today()
            await self.ensure_lore(d)
            notes = Notes()
            parts = []
            for it in self.db_items(d.strftime("%m-%d"), "lore", chat):
                parts.append(esc(it["text"]) + (notes.mark(it.get("url"), _src_label(it.get("url"))) if it.get("url") else (" · <i>ваш запис</i>" if it.get("owner") else "")))
            body = "\n\n".join(parts) or "Для цього дня прикмет у джерелах не знайшлося — вигадувати їх бот не буде."
            return None, (f"📜 <b>Народний календар · {d.day} {MONTHS_GEN[d.month - 1]}</b>\n\n{body}"[:4000],
                          [[("📝 Редагувати", self.cb("obl", f"lore.{d.strftime('%Y%m%d')}")), ("« Теми дня", self.cb("obh"))]])
        if a == "obu":
            n = await self.prefetch()
            self._bg(self.prefetch(refresh_days=0 if self.obs_coverage() >= 366 else 180))
            return "🔄 Завантажую весь рік у фоні", self.obs_home(chat)
        if a == "obm":
            self.ask(chat, "obm")
            return None, ("⏰ О котрій надсилати теми дня? Напишіть час, напр. <code>07:30</code>, або «-», щоб вимкнути.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "oba":
            self.ask(chat, "oba")
            return None, ("➕ Своя тема: <code>Назва | дата або період | щороку/разово | !/!!</code>\nНапр.:\n<code>День супу | 15.11</code>\n"
                          "<code>Тиждень інформування про грип | 01.11–07.11</code>\n<code>Місячник прибирання | 01.04–30.04 | щороку</code>\n"
                          "<code>День родини | 12.06 | щороку | !!</code> — ‼️ дуже важливий\n"
                          "Додайте «особисто», щоб бачили лише ви.",
                          [[("✖️ Скасувати", "m:cancel")]])
        return None, None

    async def obs_text(self, chat, key, data, text):
        t = text.strip()
        if key == "obcu":
            from . import contentplan as CP
            m = re.search(r"https://(?:docs|drive)\.google\.com/\S+", t)
            if not m or not CP.file_id(m.group(0)):
                return "Це не схоже на посилання Google Таблиці чи Диску", None
            self.store.set("cplan_urls", [m.group(0)])
            self.store.set("cplan_data", {})
            await self.ensure_cplan(force=True)
            return "🔗 Посилання збережено", self.cplan_status(chat)
        if key == "obce":  # ✏️ текст правки контент-плану — спершу перегляд, запис лише після «✅ Зберегти»
            return None, self.cplan_preview(chat, {**(data or {}), "new": t[:500]})
        if key == "obm":
            if t in ("-", "—"):
                self.store.set("obs_morning", False)
            elif re.fullmatch(r"\d{1,2}[:.]\d{2}", t):
                h, m = re.split(r"[:.]", t)
                self.store.set("obs_time", f"{int(h):02d}:{m}")
                self.store.set("obs_morning", True)
            return "Збережено", self.obs_home(chat)
        if key == "obe":
            kind, iid = data
            if kind == "o":
                self.obs_set(iid, title=t[:160])
                return "✏️ Змінено", self.obs_card(iid, chat)
            self.db_set(iid, text=t[:220])
            return "✏️ Змінено", self.db_card(chat, iid)
        if key == "oby":
            why, _, since = t.partition("|")
            self.obs_set(data, why=why.strip()[:120], since=re.sub(r"\D", "", since)[:4])
            return "📍 Збережено", self.obs_card(data, chat)
        if key == "obn":
            cat, ymd, personal = data
            md = f"{ymd[4:6]}-{ymd[6:8]}"
            owner = chat if personal else 0
            if cat == "days":
                title, _, flag = t.partition("|")
                oid = self.obs_add(title, kind_of(title) if kind_of(title) == "day" else "day", md, md, 1, "ваш запис", owner=owner)
                if oid and flag.strip() in ("!", "!!"):
                    self.obs_set(oid, imp=len(flag.strip()))
            else:
                self.db_add(md, cat, t, owner=owner)
            return "➕ Додано — повторюватиметься щороку", self.obs_list(chat, cat, ymd)
        if key == "oba":
            parts = [p.strip() for p in t.split("|")]
            dates = re.findall(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", parts[1] if len(parts) > 1 else "")
            if not parts[0] or not dates:
                return "Формат: Назва | 15.11 або 01.11–07.11", self.obs_home(chat)
            (d1, m1, y1), (d2, m2, y2) = dates[0], dates[-1]
            rest = " ".join(parts[2:]).lower()
            once = "раз" in rest or bool(y1)
            if once:
                yy = int(y1 or self.today().year)
                a, b = f"{yy}-{int(m1):02d}-{int(d1):02d}", f"{int(y2 or yy)}-{int(m2):02d}-{int(d2):02d}"
            else:
                a, b = f"{int(m1):02d}-{int(d1):02d}", f"{int(m2):02d}-{int(d2):02d}"
            oid = self.obs_add(parts[0], kind_of(parts[0]) if a == b or kind_of(parts[0]) != "day" else "week", a, b, 0 if once else 1,
                               "ваш запис", owner=chat if "особист" in rest else 0)
            imp = re.search(r"(!!|!)", rest)
            if oid and imp:
                self.obs_set(oid, imp=len(imp.group(1)))
            return "➕ Додано", self.obs_home(chat)
        return None, None
