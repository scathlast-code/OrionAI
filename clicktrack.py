"""📈 Облік кліків за UTM-посиланнями — як статистика Short.io чи Google Analytics.

Кожна мітка отримує коротке посилання (власне /r/<код> на домені бота або Short.io). Коли хтось
натискає його в будь-якій соцмережі, бот записує перехід і одразу перекидає на сайт:
звідки (соцмережа — за міткою, вбудованим браузером застосунку й referrer), тег, країна, місто,
пристрій, система, браузер, мова, час. Попередні перегляди посилань (робот Telegram, Facebook тощо)
рахуються окремо й у кліки не потрапляють. IP-адреси не зберігаються — лише відбиток для «унікальних»
(хеш IP + браузер + день). Вік і стать за кліком дізнатися неможливо: їх дає лише аналітика самого сайту.
"""
import hashlib
import os
import re
import secrets
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
from urllib.parse import urlsplit, parse_qs

import httpx
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
COUNTRY_UK = {"Ukraine": "Україна", "Poland": "Польща", "Germany": "Німеччина", "United States": "США", "United Kingdom": "Велика Британія",
              "Czechia": "Чехія", "Czech Republic": "Чехія", "Slovakia": "Словаччина", "Hungary": "Угорщина", "Romania": "Румунія",
              "Moldova": "Молдова", "Republic of Moldova": "Молдова", "Lithuania": "Литва", "Latvia": "Латвія", "Estonia": "Естонія",
              "Netherlands": "Нідерланди", "France": "Франція", "Italy": "Італія", "Spain": "Іспанія", "Portugal": "Португалія",
              "Canada": "Канада", "Austria": "Австрія", "Switzerland": "Швейцарія", "Ireland": "Ірландія", "Sweden": "Швеція",
              "Norway": "Норвегія", "Finland": "Фінляндія", "Denmark": "Данія", "Belgium": "Бельгія", "Bulgaria": "Болгарія",
              "Georgia": "Грузія", "Turkey": "Туреччина", "Türkiye": "Туреччина", "Israel": "Ізраїль", "Russia": "росія", "Belarus": "білорусь",
              "Kazakhstan": "Казахстан", "Japan": "Японія", "China": "Китай", "India": "Індія", "Australia": "Австралія", "Brazil": "Бразилія"}
CITY_UK = {"Kyiv": "Київ", "Kiev": "Київ", "Lviv": "Львів", "Kharkiv": "Харків", "Odesa": "Одеса", "Odessa": "Одеса", "Dnipro": "Дніпро",
           "Zaporizhzhia": "Запоріжжя", "Vinnytsia": "Вінниця", "Ivano-Frankivsk": "Івано-Франківськ", "Ternopil": "Тернопіль",
           "Lutsk": "Луцьк", "Rivne": "Рівне", "Uzhhorod": "Ужгород", "Chernivtsi": "Чернівці", "Khmelnytskyi": "Хмельницький",
           "Zhytomyr": "Житомир", "Poltava": "Полтава", "Sumy": "Суми", "Chernihiv": "Чернігів", "Cherkasy": "Черкаси",
           "Kropyvnytskyi": "Кропивницький", "Mykolaiv": "Миколаїв", "Kherson": "Херсон", "Bila Tserkva": "Біла Церква",
           "Kryvyi Rih": "Кривий Ріг", "Warsaw": "Варшава", "Krakow": "Краків", "Kraków": "Краків", "Wroclaw": "Вроцлав",
           "Prague": "Прага", "Berlin": "Берлін", "Vienna": "Відень", "London": "Лондон"}

NETWORK = {  # utm_source / платформа мітки → назва соцмережі
    "telegram": "Telegram", "tumblr": "Tumblr", "facebook": "Facebook", "threads": "Threads", "instagram": "Instagram",
    "twitter": "Twitter/X", "youtube": "YouTube", "tiktok": "TikTok",
}
APP_UA = [  # вбудовані браузери застосунків
    (r"Instagram", "Instagram"), (r"Barcelona", "Threads"), (r"FBAN|FBAV|FB_IAB|FBIOS|\[FB", "Facebook"), (r"Telegram", "Telegram"),
    (r"Twitter|TwitterAndroid", "Twitter/X"), (r"musical_ly|BytedanceWebview|TikTok", "TikTok"), (r"LinkedInApp", "LinkedIn"),
    (r"Viber", "Viber"), (r"WhatsApp", "WhatsApp"), (r"Tumblr", "Tumblr"), (r"YouTube", "YouTube"),
]
REF_HOST = [  # referrer → соцмережа
    (r"(^|\.)t\.me$|telegram", "Telegram"), (r"facebook\.com|fb\.com|fb\.me", "Facebook"), (r"instagram\.com", "Instagram"),
    (r"threads\.(net|com)", "Threads"), (r"(^|\.)t\.co$|twitter\.com|x\.com", "Twitter/X"), (r"tumblr\.com", "Tumblr"),
    (r"youtube\.com|youtu\.be", "YouTube"), (r"tiktok\.com", "TikTok"), (r"google\.", "Google"), (r"linkedin\.com|lnkd\.in", "LinkedIn"),
]
BOT_UA = re.compile(r"bot\b|crawler|spider|preview|facebookexternalhit|Facebot|TelegramBot|Twitterbot|WhatsApp/|Slackbot|Discordbot|"
                    r"vkShare|SkypeUriPreview|Embedly|Googlebot|bingbot|YandexBot|curl/|python-|httpx|Go-http|okhttp|HeadlessChrome|meta-externalagent", re.I)


def parse_ua(ua):
    """→ (пристрій, система, браузер, застосунок)."""
    ua = ua or ""
    app = next((name for rx, name in APP_UA if re.search(rx, ua)), "")
    if re.search(r"iPad|Tablet|SM-T|Tab(?!le)", ua):
        dev = "Планшет"
    elif re.search(r"Mobi|iPhone|Android", ua):
        dev = "Телефон"
    elif ua:
        dev = "Комп'ютер"
    else:
        dev = "Невідомо"
    os_ = ("iOS" if re.search(r"iPhone|iPad|iPod", ua) else "Android" if "Android" in ua else "Windows" if "Windows" in ua
           else "macOS" if "Mac OS X" in ua or "Macintosh" in ua else "ChromeOS" if "CrOS" in ua else "Linux" if "Linux" in ua else "Інша")
    br = (app + " (вбудований)" if app else "Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Samsung Internet" if "SamsungBrowser" in ua
          else "Firefox" if re.search(r"Firefox|FxiOS", ua) else "Chrome" if re.search(r"Chrome|CriOS", ua) else "Safari" if "Safari" in ua else "Інший")
    return dev, os_, br, app


def network_of(platform, app, ref_host):
    """Соцмережа переходу: за вбудованим браузером, referrer, інакше — за тим, для якої мережі робили мітку."""
    if app and app in NETWORK.values():
        return app
    for rx, name in REF_HOST:
        if ref_host and re.search(rx, ref_host, re.I):
            return name
    src = (platform or "").split("_")[0]
    return NETWORK.get(src, src or "Невідомо")


class Tracker:
    def __init__(self, path):
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._geo = {}
        with closing(self.db()) as con, con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, full_url TEXT NOT NULL, base_url TEXT NOT NULL, platform TEXT NOT NULL,
                    tag TEXT NOT NULL, created REAL NOT NULL, provider TEXT NOT NULL DEFAULT 'own', short_url TEXT, ext_id TEXT);
                CREATE INDEX IF NOT EXISTS links_full ON links(full_url);
                CREATE TABLE IF NOT EXISTS hits (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, ts REAL NOT NULL, network TEXT, country TEXT,
                    city TEXT, region TEXT, device TEXT, os TEXT, browser TEXT, lang TEXT, referrer TEXT, bot INTEGER NOT NULL DEFAULT 0, uniq TEXT);
                CREATE INDEX IF NOT EXISTS hits_code ON hits(code, ts);
                CREATE TABLE IF NOT EXISTS ext_stats (code TEXT NOT NULL, dim TEXT NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL, synced REAL NOT NULL,
                    PRIMARY KEY (code, dim, key));
            """)

    def db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    # ---------------------------------------------------------------- посилання
    def link_for(self, full_url, base_url, platform, tag):
        """Коротке власне посилання для мітки (одне на мітку — повторний виклик повертає той самий код)."""
        with closing(self.db()) as con, con:
            row = con.execute("SELECT code FROM links WHERE full_url=? AND provider='own'", (full_url,)).fetchone()
            if row:
                return row["code"]
            while True:
                code = secrets.token_urlsafe(5).replace("-", "x").replace("_", "z")[:7]
                if not con.execute("SELECT 1 FROM links WHERE code=?", (code,)).fetchone():
                    break
            con.execute("INSERT INTO links(code, full_url, base_url, platform, tag, created) VALUES (?,?,?,?,?,?)",
                        (code, full_url, base_url, platform, tag, time.time()))
            return code

    def add_external(self, full_url, base_url, platform, tag, short_url, ext_id):
        with closing(self.db()) as con, con:
            row = con.execute("SELECT code FROM links WHERE full_url=? AND provider='shortio'", (full_url,)).fetchone()
            if row:
                return row["code"]
            code = "s" + (ext_id or secrets.token_hex(4))
            con.execute("INSERT OR REPLACE INTO links(code, full_url, base_url, platform, tag, created, provider, short_url, ext_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (code, full_url, base_url, platform, tag, time.time(), "shortio", short_url, ext_id))
            return code

    def target(self, code):
        with closing(self.db()) as con:
            row = con.execute("SELECT full_url FROM links WHERE code=? AND provider='own'", (code,)).fetchone()
        return row["full_url"] if row else None

    # ---------------------------------------------------------------- кліки
    async def geo(self, ip):
        """Країна й місто за IP (безкоштовні сервіси, з кешем). Сама IP-адреса ніде не зберігається."""
        if not ip or ip.startswith(("10.", "127.", "192.168.", "172.16.", "::1", "fc", "fd")):
            return ("", "", "")
        if ip in self._geo:
            return self._geo[ip]
        res = ("", "", "")
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                r = await c.get(f"https://ipwho.is/{ip}", params={"fields": "success,country,city,region"})
                j = r.json()
                if j.get("success"):
                    res = (j.get("country") or "", j.get("city") or "", j.get("region") or "")
                else:
                    r = await c.get(f"http://ip-api.com/json/{ip}", params={"fields": "status,country,city,regionName"})
                    j = r.json()
                    if j.get("status") == "success":
                        res = (j.get("country") or "", j.get("city") or "", j.get("regionName") or "")
        except Exception:  # noqa: BLE001 — без геоданих клік однаково рахується
            pass
        res = (COUNTRY_UK.get(res[0], res[0]), CITY_UK.get(res[1], res[1]), res[2])
        if len(self._geo) > 5000:
            self._geo.clear()
        self._geo[ip] = res
        return res

    async def record(self, code, ip, ua, referrer="", lang=""):
        with closing(self.db()) as con:
            link = con.execute("SELECT platform FROM links WHERE code=?", (code,)).fetchone()
        if not link:
            return
        is_bot = bool(BOT_UA.search(ua or "")) or not ua
        dev, os_, br, app = parse_ua(ua)
        ref_host = (urlsplit(referrer).hostname or "") if referrer else ""
        country, city, region = ("", "", "") if is_bot else await self.geo(ip)
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        uniq = hashlib.sha256(f"{ip}|{ua}|{day}|{code}".encode()).hexdigest()[:16]
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO hits(code, ts, network, country, city, region, device, os, browser, lang, referrer, bot, uniq) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (code, time.time(), network_of(link["platform"], app, ref_host), country, city, region, dev, os_, br,
                         (lang or "")[:2].lower(), ref_host, int(is_bot), uniq))

    # ---------------------------------------------------------------- Short.io
    def save_external(self, code, dim, counts):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM ext_stats WHERE code=? AND dim=?", (code, dim))
            con.executemany("INSERT INTO ext_stats(code, dim, key, n, synced) VALUES (?,?,?,?,?)",
                            [(code, dim, str(k)[:80], int(v), time.time()) for k, v in counts.items() if int(v or 0) > 0])

    def external_links(self):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM links WHERE provider='shortio' AND ext_id IS NOT NULL AND created>?",
                                                 (time.time() - 90 * 86400,))]

    # ---------------------------------------------------------------- звіт
    def report(self, days=None, tag=None, network=None):
        """Зведення як у Short.io / Google Analytics: кліки, унікальні, по днях, соцмережах, тегах, країнах, містах, пристроях…"""
        since = time.time() - days * 86400 if days else 0
        q = ("SELECT h.*, l.tag, l.platform, l.base_url FROM hits h JOIN links l ON l.code=h.code WHERE h.ts>=?")
        args = [since]
        if tag:
            q += " AND l.tag=?"
            args.append(tag)
        with closing(self.db()) as con:
            rows = [dict(r) for r in con.execute(q, args)]
            ext = [dict(r) for r in con.execute("SELECT e.dim, e.key, e.n, l.tag, l.platform, l.base_url FROM ext_stats e JOIN links l ON l.code=e.code"
                                                 + (" WHERE l.tag=?" if tag else ""), [tag] if tag else [])]
            nlinks = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        if network:
            rows = [r for r in rows if r["network"] == network]
        clicks = [r for r in rows if not r["bot"]]
        out = {"days": days, "only_tag": tag, "links": nlinks, "clicks": len(clicks), "unique": len({r["uniq"] for r in clicks}),
               "previews": len(rows) - len(clicks)}
        for key, col in (("network", "network"), ("tag", "tag"), ("country", "country"), ("city", "city"), ("device", "device"),
                         ("os", "os"), ("browser", "browser"), ("lang", "lang"), ("referrer", "referrer")):
            out[key] = Counter((r[col] or "—") for r in clicks)
        out["article"] = Counter(r["base_url"] for r in clicks)
        out["format"] = Counter(r["platform"] for r in clicks)
        out["day"] = Counter(datetime.fromtimestamp(r["ts"], TZ).strftime("%Y-%m-%d") for r in clicks)
        out["hour"] = Counter(datetime.fromtimestamp(r["ts"], TZ).strftime("%H") for r in clicks)
        # Short.io: додаємо його кліки (за весь час — так віддає API)
        shortio = Counter()
        for e in ext:
            if e["dim"] == "clicks":
                shortio[e["platform"]] += e["n"]
                out["network"][network_of(e["platform"], "", "")] += e["n"]
                out["tag"][e["tag"]] += e["n"]
                out["article"][e["base_url"]] += e["n"]
                out["format"][e["platform"]] += e["n"]
            elif e["dim"] in ("country", "city", "browser", "os", "referrer"):
                out[e["dim"]][e["key"]] += e["n"]
        out["shortio"] = sum(shortio.values())
        out["clicks"] += out["shortio"]
        return out


def bar(n, top, width=12):
    return "▇" * max(1, round(n / top * width)) if n and top else ""


def report_html(rep, esc, labels=None, limit=8):
    """Звіт текстом для чату й застосунку."""
    labels = labels or {}
    period = f"за {rep['days']} дн." if rep["days"] else "за весь час"
    lines = [f"📈 <b>Кліки за UTM-посиланнями</b> · {period}" + (f" · тег <code>{esc(rep['only_tag'])}</code>" if rep.get("only_tag") else ""),
             f"Кліків: <b>{rep['clicks']}</b> · унікальних: <b>{rep['unique']}</b>"
             + (f" (з них Short.io: {rep['shortio']})" if rep.get("shortio") else "")
             + f" · посилань: {rep['links']}",
             f"<i>Попередніх переглядів роботами соцмереж (не враховано): {rep['previews']}</i>" if rep["previews"] else ""]
    if not rep["clicks"]:
        lines.append("\nКліків поки немає. Публікуйте <b>короткі посилання</b> з блоку міток — кожен перехід з'явиться тут.")
        return "\n".join(x for x in lines if x)

    def section(title, counter, fmt=lambda k: k):
        items = counter.most_common(limit)
        if not items:
            return
        top = items[0][1]
        lines.append(f"\n<b>{title}</b>")
        for k, n in items:
            lines.append(f"{esc(str(fmt(k)))[:60]} — <b>{n}</b> {bar(n, top)}")

    section("🌐 Соцмережі", rep["network"])
    section("🏷 Теги (utm_content)", rep["tag"])
    section("🧩 Формати", rep["format"], lambda k: labels.get(k, k))
    section("📰 Статті", rep["article"], lambda k: (urlsplit(k).path or k).strip("/")[-50:] or k)
    section("🗺 Країни", rep["country"])
    section("🏙 Міста", rep["city"])
    section("📱 Пристрої", rep["device"])
    section("💻 Системи", rep["os"])
    section("🧭 Браузери", rep["browser"])
    section("🗣 Мова пристрою", rep["lang"])
    days = sorted(rep["day"].items())[-14:]
    if days:
        top = max(n for _, n in days)
        lines.append("\n<b>📅 По днях</b>")
        lines += [f"{d[8:]}.{d[5:7]} — <b>{n}</b> {bar(n, top)}" for d, n in days]
    if rep["hour"]:
        h = rep["hour"].most_common(3)
        lines.append("\n⏰ Найактивніші години: " + ", ".join(f"{k}:00 ({n})" for k, n in h))
    lines.append("\n<i>Вік і стать за кліком не видно ніде — їх показує лише Google Analytics самого сайту.</i>")
    return "\n".join(x for x in lines if x)


def export_xlsx(rep, labels=None):
    """Excel як у Google Analytics: аркуш на кожен розріз + графіки."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.styles import Font
    labels = labels or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "Огляд"
    ws.append(["Кліки за UTM-посиланнями", f"за {rep['days']} дн." if rep["days"] else "за весь час"])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in (("Кліків", rep["clicks"]), ("Унікальних", rep["unique"]), ("З них Short.io", rep.get("shortio", 0)),
                 ("Попередні перегляди роботами", rep["previews"]), ("Посилань", rep["links"])):
        ws.append([k, v])
    ws.column_dimensions["A"].width = 34
    days = sorted(rep["day"].items())
    if days:
        d = wb.create_sheet("По днях")
        d.append(["День", "Кліки"])
        for k, n in days:
            d.append([k, n])
        ch = LineChart()
        ch.title, ch.height, ch.width = "Кліки по днях", 8, 18
        ch.add_data(Reference(d, min_col=2, min_row=1, max_row=len(days) + 1), titles_from_data=True)
        ch.set_categories(Reference(d, min_col=1, min_row=2, max_row=len(days) + 1))
        d.add_chart(ch, "D2")
    for title, key, fmt in (("Соцмережі", "network", None), ("Теги", "tag", None), ("Формати", "format", lambda k: labels.get(k, k)),
                            ("Статті", "article", None), ("Країни", "country", None), ("Міста", "city", None), ("Пристрої", "device", None),
                            ("Системи", "os", None), ("Браузери", "browser", None), ("Мова", "lang", None), ("Referrer", "referrer", None)):
        items = rep[key].most_common(100)
        if not items:
            continue
        s = wb.create_sheet(title)
        s.append([title, "Кліки"])
        s["A1"].font = s["B1"].font = Font(bold=True)
        for k, n in items:
            s.append([fmt(k) if fmt else k, n])
        s.column_dimensions["A"].width = 48
        ch = BarChart()
        ch.type, ch.title, ch.height, ch.width = "bar", title, max(6, min(18, len(items[:15]) * 0.8)), 16
        ch.add_data(Reference(s, min_col=2, min_row=1, max_row=min(len(items), 15) + 1), titles_from_data=True)
        ch.set_categories(Reference(s, min_col=1, min_row=2, max_row=min(len(items), 15) + 1))
        ch.legend = None
        s.add_chart(ch, "D2")
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def base_of(full_url):
    """Посилання без UTM-параметрів — щоб групувати кліки за статтями."""
    p = urlsplit(full_url)
    keep = "&".join(f"{k}={v[0]}" for k, v in parse_qs(p.query).items() if not k.startswith("utm_"))
    return f"{p.scheme}://{p.netloc}{p.path}" + (f"?{keep}" if keep else "")
