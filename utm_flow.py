"""🔗 UTM-мітки GreenPost за специфікацією «Система генерації та обліку UTM-міток для новинного трафіку».

Структура: URL?utm_content=[тег]&utm_medium=[формат]&utm_campaign=news&utm_source=[платформа]
- одразу всі 9 стандартних форматів, кожен в окремому блоці коду з ✅ після трьох перевірок;
- тег узгоджується: бот пропонує пронумеровані варіанти або приймає власний;
- реєстр міток за 3 дні (за датами й тегами), попередження про повторне посилання;
- статистика назавжди: тижні, місяці, роки; текстові графіки; експорт у Excel і Word;
- резервні формати — лише на прямий запит.
Чиста логіка без Telegram: її викликає команда /utm у bot.py.
"""
from __future__ import annotations

import io
import re
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlsplit

from plugins.newsroom.utm import RESERVE, STANDARD, Ledger, build_url

LABELS = {
    "telegram": "Telegram (пост)", "telegram_stories": "Telegram сторіс", "tumblr": "Tumblr",
    "facebook": "Facebook (пост)", "facebook_stories": "Facebook історії (Stories)", "facebook_reels": "Facebook Reels",
    "threads": "Threads", "instagram_stories": "Instagram сторіс", "twitter": "Twitter/X",
    "youtube_video": "YouTube (відео)", "youtube_cards": "YouTube (картки)", "instagram_bio": "Instagram (шапка профілю)",
    "tiktok_bio": "TikTok (шапка профілю)", "telegram_comments": "Telegram (коментарі)",
    "facebook_comments": "Facebook (коментарі)", "twitter_comments": "Twitter/X (коментарі)",
}
WINDOW = 3 * 86400
_STOP = {"na", "v", "u", "i", "ta", "z", "za", "do", "pro", "the", "a", "an", "of", "in", "on", "and", "to", "for", "html", "php", "news",
         "novyny", "article", "post", "ua", "uk", "en", "ru", "amp", "www"}


def clean_url(url: str) -> str:
    """Чисте посилання: без дужок, лапок і пробілів довкола (правило «Чистота посилань»)."""
    u = (url or "").strip()
    m = re.search(r"https?://[^\s<>\"'\]\)]+", u)
    return m.group(0).rstrip(".,;") if m else u.strip("[]()<>\"'«» ")


def clean_tag(tag: str) -> str:
    """Тег без пробілів і службових символів: слова через «_», малими літерами."""
    t = re.sub(r"[^\w\-]+", "_", (tag or "").strip().lower(), flags=re.UNICODE)
    return re.sub(r"_+", "_", t).strip("_-")[:60]


def suggest_tags(url: str, limit: int = 5) -> list[str]:
    """Варіанти utm_content з адреси статті (слова зі «слага»). Власний варіант завжди можна надіслати текстом."""
    parts = urlsplit(clean_url(url))
    segs = [s for s in unquote(parts.path).split("/") if s]
    slug = max(segs, key=len) if segs else ""
    words = [w for w in re.split(r"[-_.\s]+", slug.lower()) if w and not any(ch.isdigit() for ch in w) and len(w) > 1 and w not in _STOP]
    out: list[str] = []

    def add(ws):
        t = clean_tag("_".join(ws))
        if t and t not in out:
            out.append(t)

    if words:
        add(words[:1]); add(words[:2]); add(words[:3])
        if len(words) > 3:
            add([words[0], words[-1]])
        long = sorted(words, key=len, reverse=True)[:2]
        add(sorted(long, key=words.index))
    if len(segs) > 1:
        add([segs[0]])  # рубрика: /eco/…, /war/…
    return out[:limit] or ["news"]


def check(generated: str, original: str, tag: str, source: str, medium: str) -> bool:
    """Три перевірки мітки: 1) посилання коректне й чисте; 2) рівно чотири UTM-параметри з потрібними значеннями
    у потрібному порядку; 3) адреса сторінки та її власні параметри не змінились."""
    g, o = urlsplit(generated), urlsplit(clean_url(original))
    if g.scheme not in ("http", "https") or not g.netloc or re.search(r"[\s\[\]()\"'<>]", generated):
        return False
    q = parse_qsl(g.query, keep_blank_values=True)
    utm = [(k, v) for k, v in q if k.startswith("utm_")]
    if utm != [("utm_content", tag), ("utm_medium", medium), ("utm_campaign", "news"), ("utm_source", source)]:
        return False
    own = [(k, v) for k, v in q if not k.startswith("utm_")]
    base = [(k, v) for k, v in parse_qsl(o.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return (g.scheme, g.netloc, g.path, g.fragment) == (o.scheme, o.netloc, o.path, o.fragment) and own == base


def generate(ledger: Ledger, url: str, tag: str, *, reserve: bool = False, only: str | None = None) -> dict:
    """Створює мітки (усі стандартні, усі резервні або одну) і записує їх у реєстр.
    Повертає {'url','tag','duplicate', 'items': [(key, label, link, ok)]}."""
    url, tag = clean_url(url), clean_tag(tag)
    table = RESERVE if reserve else STANDARD
    if only:
        if only not in {**STANDARD, **RESERVE}:
            raise ValueError("Невідомий формат. Стандартні: " + ", ".join(STANDARD) + ". Резервні: " + ", ".join(RESERVE))
        table = {only: {**STANDARD, **RESERVE}[only]}
    duplicate = was_done(ledger, url)
    t0 = time.time()
    items = []
    for key, (source, medium) in table.items():
        link, _ = ledger.create(url, tag, key, reserve=True)
        items.append((key, LABELS.get(key, key), link, check(link, url, tag, source, medium)))
    # «Повтор» — це посилання, яке вже опрацьовували за останні 3 дні, а не друга платформа того самого набору.
    with ledger.db() as con:
        con.execute("UPDATE utm_events SET duplicate=? WHERE original=? AND created>=?", (int(duplicate), url, t0))
    return {"url": url, "tag": tag, "duplicate": duplicate, "items": items}


def was_done(ledger: Ledger, url: str) -> bool:
    with ledger.db() as con:
        return bool(con.execute("SELECT 1 FROM utm_events WHERE original=? AND created>=? LIMIT 1",
                                (clean_url(url), time.time() - WINDOW)).fetchone())


def previous(ledger: Ledger, url: str) -> list[tuple]:
    """Мітки цього посилання за останні 3 дні: [(platform, generated, tag)] — останній набір."""
    with ledger.db() as con:
        rows = con.execute("SELECT platform, generated, tag, created FROM utm_events WHERE original=? AND created>=? ORDER BY created DESC",
                           (clean_url(url), time.time() - WINDOW)).fetchall()
    seen, out = set(), []
    for p, g, t, _ in rows:
        if p not in seen:
            seen.add(p); out.append((p, g, t))
    return out


def registry(ledger: Ledger, tz) -> "OrderedDict[str, OrderedDict[str, list]]":
    """Реєстр нещодавніх міток: дата → тег → [(назва формату, посилання)], найновіше зверху, без повторів."""
    with ledger.db() as con:
        rows = con.execute("SELECT created, generated, tag, platform FROM utm_events WHERE created>=? ORDER BY created DESC",
                           (time.time() - WINDOW,)).fetchall()
    out: OrderedDict = OrderedDict()
    seen = set()
    for created, link, tag, platform in rows:
        if link in seen:
            continue
        seen.add(link)
        day = datetime.fromtimestamp(created, tz).strftime("%d.%m.%Y")
        out.setdefault(day, OrderedDict()).setdefault(tag, []).append((LABELS.get(platform, platform), link))
    return out


def stats(ledger: Ledger, tz) -> dict:
    """Довгостроковий облік (ніколи не видаляється): {'weeks','months','years': [(період, міток, посилань, повторів)],
    'platforms': [(назва, міток)], 'tags': [(тег, міток)], 'total', 'links'}."""
    with ledger.db() as con:
        rows = con.execute("SELECT created, original, platform, tag, duplicate FROM utm_events ORDER BY created").fetchall()
    per = {"weeks": OrderedDict(), "months": OrderedDict(), "years": OrderedDict()}
    plat, tags, links = {}, {}, set()
    for created, original, platform, tag, dup in rows:
        d = datetime.fromtimestamp(created, tz)
        iso = d.isocalendar()
        keys = {"weeks": f"{iso[0]}-т{iso[1]:02d}", "months": d.strftime("%Y-%m"), "years": d.strftime("%Y")}
        for kind, key in keys.items():
            b = per[kind].setdefault(key, {"n": 0, "links": set(), "dup": set()})
            b["n"] += 1; b["links"].add(original)
            if dup:
                b["dup"].add(original)
        plat[platform] = plat.get(platform, 0) + 1
        tags[tag] = tags.get(tag, 0) + 1
        links.add(original)
    fold = lambda d: [(k, v["n"], len(v["links"]), len(v["dup"])) for k, v in d.items()]
    return {"weeks": fold(per["weeks"]), "months": fold(per["months"]), "years": fold(per["years"]),
            "platforms": sorted(((LABELS.get(k, k), v) for k, v in plat.items()), key=lambda x: -x[1]),
            "tags": sorted(tags.items(), key=lambda x: -x[1]), "total": len(rows), "links": len(links)}


def bars(rows: list[tuple], width: int = 14, last: int = 8) -> str:
    """Текстовий графік: «період ████ число»."""
    rows = rows[-last:]
    top = max((r[1] for r in rows), default=0) or 1
    return "\n".join(f"{str(r[0]):<10} {'█' * max(1, round(r[1] / top * width))} {r[1]}" for r in rows) or "ще немає даних"


def stats_text(s: dict) -> str:
    """Статистика для чату (моноширинний блок із графіками)."""
    out = [f"Усього міток: {s['total']} · посилань: {s['links']}", "", "ТИЖНІ (мітки)", bars([(k, n) for k, n, _, _ in s["weeks"]]),
           "", "МІСЯЦІ (мітки)", bars([(k, n) for k, n, _, _ in s["months"]]),
           "", "РОКИ (мітки)", bars([(k, n) for k, n, _, _ in s["years"]]),
           "", "ЗА ПЛАТФОРМАМИ", bars(s["platforms"], last=16),
           "", "ТОП ТЕГІВ", bars(s["tags"][:8][::-1])]
    rep = sum(d for _, _, _, d in s["years"])
    if rep:
        out += ["", f"Повторно опрацьованих посилань: {rep}"]
    return "\n".join(out)


def export_xlsx(ledger: Ledger, tz) -> bytes:
    """Excel: окремі вкладки для тижнів, місяців і років з діаграмами, платформи, теги й повний журнал."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Font
    s = stats(ledger, tz)
    wb = Workbook()
    first = True
    for title, key in (("Тижні", "weeks"), ("Місяці", "months"), ("Роки", "years")):
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = title
        ws.append(["Період", "Міток", "Посилань", "Повторних посилань"])
        for row in s[key]:
            ws.append(list(row))
        _style(ws, Font)
        if s[key]:
            ch = BarChart(); ch.title = f"Мітки: {title.lower()}"; ch.y_axis.title = "Міток"; ch.height, ch.width = 8, 18
            ch.add_data(Reference(ws, min_col=2, min_row=1, max_row=len(s[key]) + 1), titles_from_data=True)
            ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=len(s[key]) + 1))
            ws.add_chart(ch, "F2")
    for title, rows, head in (("Платформи", s["platforms"], "Платформа"), ("Теги", s["tags"], "Тег")):
        ws = wb.create_sheet(title)
        ws.append([head, "Міток"])
        for row in rows:
            ws.append(list(row))
        _style(ws, Font)
        if rows:
            ch = BarChart(); ch.type = "bar"; ch.title = title; ch.height, ch.width = 9, 18
            n = min(len(rows), 20)
            ch.add_data(Reference(ws, min_col=2, min_row=1, max_row=n + 1), titles_from_data=True)
            ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=n + 1))
            ws.add_chart(ch, "D2")
    ws = wb.create_sheet("Журнал")
    ws.append(["Дата", "Час", "Тег", "Формат", "Повтор", "Мітка", "Початкове посилання"])
    with ledger.db() as con:
        for created, original, generated, tag, platform, dup in con.execute(
                "SELECT created, original, generated, tag, platform, duplicate FROM utm_events ORDER BY created DESC"):
            d = datetime.fromtimestamp(created, tz)
            ws.append([d.strftime("%d.%m.%Y"), d.strftime("%H:%M"), tag, LABELS.get(platform, platform), "так" if dup else "", generated, original])
    _style(ws, Font)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _style(ws, Font):
    for c in ws[1]:
        c.font = Font(bold=True)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(60, max(12, max(len(str(c.value or "")) for c in col) + 2))
    ws.freeze_panes = "A2"


def export_docx(ledger: Ledger, tz) -> bytes:
    """Word: звіт із текстом, таблицями за періодами, текстовими графіками і схемою побудови мітки."""
    from docx import Document
    from docx.shared import Pt
    s = stats(ledger, tz)
    doc = Document()
    doc.add_heading("UTM-мітки GreenPost — звіт", 0)
    doc.add_paragraph(f"Сформовано: {datetime.now(tz).strftime('%d.%m.%Y %H:%M')}. Усього міток: {s['total']}, посилань: {s['links']}.")
    doc.add_heading("Як будується мітка", 1)
    p = doc.add_paragraph()
    r = p.add_run("Посилання на новину\n      │\n      ▼\nутоджений тег (utm_content)\n      │\n      ▼\nформат (utm_medium) → кампанія news (utm_campaign) → платформа (utm_source)\n      │\n      ▼\nтри перевірки → ✅ готова мітка → реєстр на 3 дні + статистика назавжди".replace("утоджений", "узгоджений"))
    r.font.name = "Courier New"; r.font.size = Pt(9)
    doc.add_paragraph("[URL]?utm_content=[контент]&utm_medium=[формат]&utm_campaign=news&utm_source=[платформа]")
    for title, key in (("За тижнями", "weeks"), ("За місяцями", "months"), ("За роками", "years")):
        doc.add_heading(title, 1)
        t = doc.add_table(rows=1, cols=4); t.style = "Light Grid Accent 1"
        for i, h in enumerate(("Період", "Міток", "Посилань", "Повторних")):
            t.rows[0].cells[i].text = h
        for row in s[key]:
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = str(v)
        g = doc.add_paragraph().add_run(bars([(k, n) for k, n, _, _ in s[key]], last=12))
        g.font.name = "Courier New"; g.font.size = Pt(9)
    for title, rows in (("За платформами", s["platforms"]), ("Найчастіші теги", s["tags"][:15])):
        doc.add_heading(title, 1)
        g = doc.add_paragraph().add_run(bars(rows[::-1], last=20))
        g.font.name = "Courier New"; g.font.size = Pt(9)
    doc.add_heading("Мітки за останні 3 дні", 1)
    reg = registry(ledger, tz)
    if not reg:
        doc.add_paragraph("За останні 3 дні міток не було.")
    for day, tags in reg.items():
        doc.add_heading(day, 2)
        for tag, items in tags.items():
            doc.add_paragraph(f"Тег: {tag}").runs[0].bold = True
            for label, link in items:
                doc.add_paragraph(f"{label}: {link}", style="List Bullet")
    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()
