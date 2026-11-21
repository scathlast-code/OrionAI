"""🪔 Традиції, 🔭 новини астрономії й ♈ новини астрології — ЛИШЕ з джерел: дослівні речення зі сторінок пошуку,
під кожним — посилання. ШІ нічого не пише й не переказує. Російські, білоруські, іранські ресурси відкинуто (websearch)."""
from __future__ import annotations

import datetime as dt
import re

MONTHS_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
MONTHS_NOM = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
MONTHS_LOC = ["січні", "лютому", "березні", "квітні", "травні", "червні", "липні", "серпні", "вересні", "жовтні", "листопаді", "грудні"]

TRAD_RE = re.compile(r"(?i)\b(?:звича\w*|традиці\w*|обряд\w*|заборон\w*|не можна|можна|господин\w*|колядув\w*|щедрув\w*|засіва\w*|"
                     r"ворожі\w*|пекли|печуть|готува\w*|прикраша\w*|вшанову\w*|ходили|ходять|молил\w*|святкува\w*|прийнято|належить)\b")
OMEN_RE = re.compile(r"(?i)^\s*(?:якщо|коли)\b.*\b(?:то|буде|будуть)\b")
ASTRO_RE = re.compile(r"(?i)\b(?:метеор\w*|зорепад\w*|комет\w*|затемнен\w*|повн[яію]\b|молодик\w*|суперм[іі]сяц\w*|парад планет|полярн\w* сяйв\w*|"
                      r"протистоян\w*|соєднан\w*|сонцестоян\w*|рівноден\w*|магнітн\w* бур\w*|планет\w*|Меркур\w*|Венер\w*|Марс\w*|Юпітер\w*|Сатурн\w*)")
ASTROL_RE = re.compile(r"(?i)\b(?:ретроград\w*|Меркур\w*|Венер\w*|Марс\w*|Юпітер\w*|Сатурн\w*|Уран\w*|Нептун\w*|Плутон\w*|коридор затемнень|"
                       r"знак\w* зодіаку|астролог\w*|гороскоп\w*)")


def sentences(snippet):
    """Уривок → повні речення без Markdown/маркерів; обірване в кінці відкидається; лише українською."""
    t = re.sub(r"#+\s*|\*\*|__|`", " ", snippet or "")
    t = re.sub(r"(?:^|\s)(?:[-–•·*]|\d+[.)])\s+", "\n", t)
    out = []
    for part in t.split("\n"):
        for x in re.findall(r"[^.!?;…]+[.!?;]", part):
            x = re.sub(r"\s+", " ", x).strip(" ,:—-[](){}…")
            x = x[:-1] + "." if x.endswith(";") else x
            if 25 <= len(x) <= 280 and not re.search(r"[ыэъё]", x, re.I) and re.search(r"[а-яіїєґ]", x, re.I):
                out.append(x[0].upper() + x[1:])
    return out


def tidy(text, date=""):
    """Охайний рядок: без обривків «] … [», з пробілом перед тире; дата на початку, що збігається з показаною, — прибрана."""
    t = re.sub(r"\[\s*(?:\.\.\.|…)?\s*\]", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip(" ,:;—–-[](){}…»«\"'")
    t = re.sub(r"(?<=\S)\s*([—–])\s*(?=\S)", r" \1 ", t)
    if date:
        try:
            d = dt.date.fromisoformat(date)
            t = re.sub(rf"^(?:з\s+|до\s+)?{d.day}\s+{MONTHS_GEN[d.month - 1]}(?:\s+{d.year}(?:\s+року)?)?\s*[,:—–-]?\s*", "", t, flags=re.I)
        except ValueError:
            pass
    return (t[0].upper() + t[1:]) if t else t


def dates_in(text, year):
    out = []
    for d, m in re.findall(r"\b(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s+(" + "|".join(MONTHS_GEN) + r")\b", text or "", re.I):
        mi = MONTHS_GEN.index(m.lower()) + 1
        for y in (year, year + 1):
            try:
                out.append(dt.date(y, mi, int(d)))
                break
            except ValueError:
                pass
    return out


def _key(t):
    return re.sub(r"\W+", "", t.lower())[:70]


async def collect(search_items, queries, keep, limit=10):
    """Пошук за кількома запитами → [{text, url, host}] з реченнями, що проходять keep(речення)."""
    items, seen = [], set()
    for q in queries:
        try:
            rows = await search_items(q, limit=8)
        except Exception:  # noqa: BLE001
            continue
        for r in rows:
            host = r["url"].split("/")[2].removeprefix("www.") if "//" in r["url"] else ""
            for snt in sentences(r.get("snippet") or ""):
                k = _key(snt)
                if k in seen or not keep(snt):
                    continue
                seen.add(k)
                items.append({"text": snt, "url": r["url"], "host": host})
                if len(items) >= limit:
                    return items
    return items


async def traditions(search_items, d):
    """Народні традиції саме цього дня (не прикмети «якщо…то»)."""
    q = [f"{d.day} {MONTHS_GEN[d.month - 1]} традиції звичаї", f"{d.day} {MONTHS_GEN[d.month - 1]} що можна і не можна робити"]
    return await collect(search_items, q, lambda s: bool(TRAD_RE.search(s)) and not OMEN_RE.search(s), limit=8)


async def astro_news(search_items, d, horizon=45):
    """Найближчі астрономічні події з датою: метеорні потоки, комети, затемнення, повні, парад планет тощо."""
    y, m, nm = d.year, d.month, d.month % 12 + 1
    q = [f"астрономічний календар {MONTHS_NOM[m - 1]} {y}", f"що можна побачити на небі в {MONTHS_LOC[m - 1]} {y}",
         f"астрономічні події {MONTHS_NOM[nm - 1]} {y if nm > m else y + 1}", f"метеорний потік {MONTHS_NOM[m - 1]} {y}",
         f"комета {y} коли видно", f"затемнення {y} {y + 1} дата"]

    def keep(s):
        if not ASTRO_RE.search(s):
            return False
        ds = [x for x in dates_in(s, y) if d <= x <= d + dt.timedelta(days=horizon)]
        return bool(ds)
    items = await collect(search_items, q, keep, limit=14)
    for it in items:
        ds = [x for x in dates_in(it["text"], y) if d <= x <= d + dt.timedelta(days=horizon)]
        it["date"] = min(ds).isoformat() if ds else ""
    return sorted(items, key=lambda it: it["date"])


async def astrol_news(search_items, d, horizon=45):
    """Астрологічні новини з датою (ретроградні планети, коридор затемнень тощо) — для розваги."""
    y, m = d.year, d.month
    q = [f"ретроградний Меркурій {y}", f"ретроградні планети {MONTHS_NOM[m - 1]} {y}", f"астрологічний календар {MONTHS_NOM[m - 1]} {y}"]

    def keep(s):
        return bool(ASTROL_RE.search(s)) and any(d <= x <= d + dt.timedelta(days=horizon) for x in dates_in(s, y))  # лише найближчі
    items = await collect(search_items, q, keep, limit=8)
    for it in items:
        ds = [x for x in dates_in(it["text"], y) if x >= d]
        it["date"] = min(ds).isoformat() if ds else ""
    return sorted(items, key=lambda it: it["date"])


async def aurora(client_get):
    """🌌 Прогноз геомагнітної активності NOAA SWPC (Kp на 3 доби). В Україні сяйво можливе приблизно з Kp 7 (на півночі)."""
    url = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
    data = await client_get(url)
    rows = [r for r in data[1:] if len(r) >= 2]
    best = None
    for r in rows:
        try:
            kp, when = float(r[1]), r[0]
        except (ValueError, TypeError):
            continue
        if best is None or kp > best[0]:
            best = (kp, when)
    if not best:
        return None
    kp, when = best
    chance = ("можливе на півночі України" if kp >= 7 else "малоймовірне в Україні") if kp < 8 else "можливе по більшій частині України"
    return {"text": f"Полярне сяйво: найвищий прогнозний індекс Kp {kp:g} ({when[:16]} UTC) — {chance}.",
            "url": "https://www.swpc.noaa.gov/products/planetary-k-index", "host": "NOAA SWPC"}
