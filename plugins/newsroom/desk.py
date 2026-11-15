"""🧭 Редакційний пульт ньюзруму: наші ресурси, історії (кластери), пошук в інтернеті, оцінка,
теги, тематичні кнопки, блокування, антиспам, біг ньюз за шаблонами, Word UA+RU."""
from __future__ import annotations

import html as _html
import io
import json
import re
import time
import zipfile
from contextlib import closing
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

from .store import normalize, shingles, similarity

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL, ours INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS story_refs (           -- усе, що належить історії: наші пости, матеріали, знахідки
    id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, kind TEXT NOT NULL,   -- ours | item | find | draft
    ref_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', relation TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '', at REAL NOT NULL, text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS story_refs_s ON story_refs(story_id);
CREATE TABLE IF NOT EXISTS ours_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, at REAL NOT NULL, story_id INTEGER, key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS finds (
    id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',   -- tag | mode | manual
    title TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
    published REAL NOT NULL DEFAULT 0, snippet TEXT NOT NULL DEFAULT '', score TEXT NOT NULL DEFAULT '{}',
    priority REAL NOT NULL DEFAULT 0, story_id INTEGER, folder TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new',
    note TEXT NOT NULL DEFAULT '', found REAL NOT NULL, key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    interval INTEGER NOT NULL DEFAULT 3600, last_run REAL NOT NULL DEFAULT 0, sort TEXT NOT NULL DEFAULT 'time', found INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, title TEXT NOT NULL, keywords TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topic_profiles (
    topic_key TEXT PRIMARY KEY, tone TEXT NOT NULL DEFAULT '', voice TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL, at REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    UNIQUE(kind, value)
);
CREATE TABLE IF NOT EXISTS spamlog (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, reason TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', text TEXT NOT NULL
);
"""

DEFAULT_TOPICS = [
    ("war", "⚔️ Війна", "війна, фронт, обстріл, атака, дрон, ракет, ЗСУ, окупант, ППО, загибл, поранен, вибух, евакуац"),
    ("astronomy", "🔭 Астрономія", "астрономія, космос, телескоп, планети, зорі, NASA, ESA, астероїд"),
    ("astrology", "✨ Астрологія", "астрологія, гороскоп, знак зодіаку, ретроград, натальна карта"),
    ("geomagnetic", "🧲 Магнітні бурі", "геомагнітна буря, магнітні бурі, Kp-індекс, сонячна активність, NOAA SWPC"),
    ("geomagnetic_now", "Зараз", "геомагнітна активність зараз, Kp index current"),
    ("geomagnetic_tomorrow", "Завтра", "магнітні бурі завтра, NOAA 3-day forecast"),
    ("geomagnetic_3days", "Прогноз на 3 дні", "NOAA SWPC 3-day geomagnetic forecast"),
    ("geomagnetic_week", "Тиждень", "геомагнітний прогноз на тиждень, Kp"),
    ("geomagnetic_month", "Місяць · орієнтовно", "NOAA 27-day outlook, geomagnetic activity"),
    ("weather", "🌦 Погода", "погода, прогноз, температура, опади, вітер, синоптик"),
    ("weather_tomorrow", "Завтра", "прогноз погоди завтра, погода завтра"),
    ("weather_week", "Тиждень", "прогноз погоди на тиждень"),
    ("weather_month", "Місяць", "прогноз погоди на місяць"),
    ("weather_season", "Сезон", "сезонний прогноз погоди"),
    ("animals", "🐾 Права тварин", "тварин, зоозахисн, безпритульн, жорстоке поводження, притулок, догхантер, цирк, зоопарк, собак, кот"),
    ("agro", "🌾 Агросектор", "агро, аграрії, врожай, посівна, фермери, зерно, тваринництво, АПК"),
    ("agro_producers", "Аграрії", "аграрії, агропідприємства, агровиробники"),
    ("agro_farming", "Фермерство", "фермерство, фермери, господарства"),
    ("agro_support", "Державна допомога", "допомога аграріям, гранти фермерам, дотації"),
    ("agro_sowing", "Посівна", "посівна, сівба, посіви"),
    ("agro_harvest", "Жнива", "жнива, збір врожаю, урожай"),
    ("energy", "⚡️ Енергетика", "енергетик, електроенергі, відключення світла, графік, енергосистем, сонячні електростанції, ВДЕ, газ, Укренерго"),
    ("energy_blackout", "Блекаут", "блекаут, відключення, аварія мережі, графіки світла"),
    ("energy_alternative", "Альтернативна енергія", "ВДЕ, сонячна, вітрова, біоенергетика"),
    ("energy_saving", "Енергоощадність", "економія енергії, енергозбереження"),
    ("energy_efficiency", "Енергоефективність", "енергоефективність, модернізація, утеплення"),
    ("energy_fuel", "Паливо", "паливо, бензин, дизель, АЗС"),
    ("energy_hydrocarbons", "Вуглеводні", "нафта, газ, вуглеводні, видобуток"),
    ("health", "🩺 Здоровʼя", "здоровʼя, МОЗ, лікарня, медицина, епідемія, вакцинація, захворювання"),
    ("health_medicine", "Медицина", "медицина, лікарні, лікування, МОЗ"),
    ("health_diseases", "Хвороби", "захворювання, епідемія, симптоми, профілактика"),
    ("health_lifestyle", "Стиль життя", "сон, звички, спосіб життя, добробут"),
    ("health_sport", "Спорт", "фізична активність, тренування, спорт"),
    ("health_nutrition", "Харчування", "харчування, продукти, дієта, раціон"),
    ("eco", "🌿 Екологія", "еколог, екоакц, довкілля, забруднення, сміття, сортування, дерев, озелен, клімат, ліси, вирубка, Держекоінспекція, викиди"),
]

MODES = {
    "hot": ("🔥 Гарячі", "щойно сталося й важливо зараз"),
    "ever": ("♾ Вічні", "теми без строку давності, які завжди цікаві"),
    "prov": ("🌶 Провокації", "очевидне, але гостре: «я так і знав» / «чому я не здогадався»"),
    "blog": ("✍️ Блоги", "авторські колонки, думки, розслідування"),
}
SIZES = {"short": ("📱 Коротко", "пост для соцмереж до 600 символів"), "news": ("📰 Новина", "звичайна новина 1200–2200 символів"),
         "big": ("📚 Велика", "стаття 4000–7000 символів із підзаголовками")}
BLOCK_KINDS = {"word": "🔤 слово / тема", "person": "👤 людина", "source": "📡 джерело / канал", "domain": "🌐 сайт"}
FOLDERS = {"fav": "⭐ Обране", "later": "⏳ Пізніше", "arch": "🗄 Архів"}

SPAM_WORDS = ["промокод", "знижк", "розіграш", "розыгрыш", "казино", "ставк", "букмекер", "заробіток", "заработок", "пасивний дохід",
              "крипто-сигнал", "інвестуй", "підпишись на", "переходь за посиланням", "реклама", "#реклама", "партнерський матеріал",
              "купити", "замовити", "акція", "розпродаж", "giveaway", "airdrop", "18+", "онлайн-казино", "кредит без"]

TEMPLATES = {
    "short": ("📱 Соцмережі", "Пост для соцмереж до 600 символів: перше речення — гачок із головним фактом; 2–4 короткі речення; "
                             "без клікбейту; в кінці 2–3 хештеги за темою. Посилання на першоджерело — окремим рядком."),
    "news": ("📰 Новина", "Класична новина: заголовок; лід (що, де, коли, хто); деталі за спаданням важливості; цитата з атрибуцією, "
                         "якщо є; контекст одним абзацом; що буде далі."),
    "big": ("📚 Біг ньюз / стаття", "Органічна велика стаття з кількох джерел: заголовок; лід; 3–5 розділів із підзаголовками "
                                    "(що сталося; що кажуть різні сторони; цифри й факти; контекст і передісторія; що далі); "
                                    "розбіжності між джерелами називати прямо; наприкінці — «Джерела» списком."),
    "update": ("🔁 Оновлення", "Оновлення до вже опублікованої новини: що нового в першому реченні; що було відомо раніше (коротко, "
                              "з посиланням на нашу попередню публікацію); що змінилося; що досі не підтверджено."),
    "refute": ("❌ Спростування", "Спростування: що стверджувалося і де; що насправді (лише з підтвердженими джерелами); хто й як "
                                 "це встановив; висновок без емоцій. Якщо доказів спростування немає — так і напиши в notes і skip=true."),
}


def now():
    return time.time()


def domain(url):
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


# ==================================================================== антиспам
def spam_score(text: str) -> tuple[int, list]:
    t = (text or "").lower()
    reasons = []
    hits = [w for w in SPAM_WORDS if w in t]
    if hits:
        reasons.append("слова: " + ", ".join(hits[:4]))
    links = len(re.findall(r"https?://|t\.me/", t))
    if links >= 4:
        reasons.append(f"посилань {links}")
    emoji = len(re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", text or ""))
    if emoji >= 12:
        reasons.append(f"емодзі {emoji}")
    letters = re.findall(r"[A-Za-zА-ЯІЇЄҐа-яіїєґ]", text or "")
    caps = sum(1 for c in letters if c.isupper())
    if len(letters) > 40 and caps / len(letters) > 0.5:
        reasons.append("капслок")
    if re.search(r"(\+?\d[\d\s\-()]{9,}\d).*(пиш|телефон|дзвон|звон)", t):
        reasons.append("телефон для продажу")
    score = len(hits) * 2 + (2 if links >= 4 else 0) + (1 if emoji >= 12 else 0) + (2 if "капслок" in reasons else 0) + (2 if any("телефон" in r for r in reasons) else 0)
    return score, reasons


# ==================================================================== веб-пошук новин
def google_news_url(query, hours=24, lang="uk"):
    when = f" when:{hours}h" if hours <= 72 else f" when:{max(1, hours // 24)}d"
    gl, ceid = ("UA", "UA:uk") if lang == "uk" else ("UA", "UA:ru")
    return f"https://news.google.com/rss/search?q={quote_plus(query + when)}&hl={lang}&gl={gl}&ceid={ceid}"


def source_search_queries(source_url: str, keywords: str = "", hours: int = 24):
    """Build source-scoped Google News queries; do not assume results are exhaustive."""
    host = domain(source_url)
    if not host or host in {"google.com", "news.google.com"}:
        raise ValueError("Потрібен коректний домен джерела")
    if not 1 <= int(hours) <= 24 * 90:
        raise ValueError("Некоректне вікно моніторингу")
    term = " ".join(str(keywords).split())[:250]
    return [f"site:{host} {term}".strip()]


def parse_google_news(data: bytes):
    """Google News RSS -> [{title, url, source, published, snippet}]"""
    import xml.etree.ElementTree as ET
    if b"<!ENTITY" in data[:4000].upper():
        return []
    root = ET.fromstring(data)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        pub = it.findtext("pubDate") or ""
        try:
            ts = parsedate_to_datetime(pub).timestamp()
        except (TypeError, ValueError):
            ts = 0
        desc = re.sub(r"<[^>]+>", " ", _html.unescape(it.findtext("description") or ""))
        out.append({"title": _html.unescape(title), "url": (it.findtext("link") or "").strip(), "source": source,
                    "published": ts, "snippet": re.sub(r"\s+", " ", desc).strip()[:400],
                    "src_url": src_el.attrib.get("url", "") if src_el is not None else ""})
    return out


def parse_websearch(text: str):
    """Текст із websearch.search -> [{title, url, snippet}]"""
    out = []
    for block in re.split(r"\n\n(?=\[\d+\] )", text or ""):
        m = re.match(r"\[\d+\] (.+?)\nURL: (\S+)\n?(.*)", block, re.S)
        if m:
            out.append({"title": m.group(1).strip(), "url": m.group(2).strip(), "source": domain(m.group(2)), "published": 0,
                        "snippet": re.sub(r"\s+", " ", m.group(3)).strip()[:400]})
    return out


def score_prompt(cands, media_topics):
    lines = []
    for n, c in enumerate(cands, 1):
        age = f"{int((now() - c['published']) // 3600)} год тому" if c.get("published") else "час невідомий"
        lines.append(f"{n}. {c['title']} — {c.get('source') or domain(c.get('url', ''))} ({age})\n   {c.get('snippet', '')[:260]}")
    return ("Оціни кандидатів у новини для медіа. Тематика медіа: " + media_topics + ".\n"
            "Для кожного: importance 1–10 (суспільна вага), fit 1–10 (наскільки пасує тематиці), urgency urgent|normal|low, "
            "credibility official|media|blog|anon|unknown (за джерелом), spam true/false (реклама, клікбейт, фейк-маркетинг), "
            "topic — одне-два слова, why — до 12 слів. Не вигадуй фактів.\n"
            "Відповідай ЛИШЕ JSON: {\"items\": [{\"n\": 1, \"importance\": 7, \"fit\": 8, \"urgency\": \"normal\", "
            "\"credibility\": \"media\", \"spam\": false, \"topic\": \"…\", \"why\": \"…\"}]}\n\nКАНДИДАТИ:\n" + "\n".join(lines))


CRED = {"official": 2.0, "media": 1.0, "blog": 0.3, "anon": -1.0, "unknown": 0.0}


def priority(sc, published):
    p = 0.5 * float(sc.get("importance") or 0) + 0.4 * float(sc.get("fit") or 0) + CRED.get(sc.get("credibility"), 0)
    if sc.get("urgency") == "urgent":
        p += 1.5
    if published:
        hours = (now() - published) / 3600
        p += 1.0 if hours < 3 else 0.5 if hours < 12 else 0 if hours < 48 else -0.5
    if sc.get("spam"):
        p -= 10
    return round(p, 2)


def heuristic_priority(c):
    p = 0.0
    if c.get("published"):
        hours = (now() - c["published"]) / 3600
        p += 3 if hours < 3 else 2 if hours < 12 else 1 if hours < 48 else 0
    d = domain(c.get("src_url") or c.get("url", ""))
    if d.endswith("gov.ua") or d in ("rada.gov.ua", "president.gov.ua"):
        p += 2
    return p


def compose_prompt(items, size, template, media_topics, ours_context=""):
    lines = []
    for n, c in enumerate(items, 1):
        when_ = time.strftime("%d.%m %H:%M", time.localtime(c["published"])) if c.get("published") else "час невідомий"
        lines.append(f"[{n}] {c['title']}\nДжерело: {c.get('source') or domain(c.get('url', ''))}{(' · автор ' + c['author']) if c.get('author') else ''} · {when_}\n"
                     f"URL: {c.get('url', '')}\n{(c.get('text') or c.get('snippet') or '')[:2500]}")
    target = {"short": "до 600 символів", "news": "1200–2200 символів", "big": "4000–7000 символів, з підзаголовками в body (рядок-підзаголовок окремим абзацом)"}[size]
    return ("Склади ОДНУ органічну публікацію з кількох матеріалів про одну тему (біг ньюз). Тематика медіа: " + media_topics + ".\n"
            f"Шаблон: {template}\nОбсяг body: {target}.\n"
            "Об'єднуй факти, не повторюй їх; розбіжності між джерелами називай; кожен факт атрибутуй джерелу; "
            "у sources — назви й URL усіх використаних матеріалів; у unconfirmed — те, що підтверджено лише одним неофіційним джерелом; "
            "у notes — оцінка достовірності (час появи, автори, першоджерела) і що перевірити.\n"
            + (f"\nНаші попередні публікації на цю тему (не повторюй їх, пиши, що нового):\n{ours_context[:2500]}\n" if ours_context else "")
            + "\nМАТЕРІАЛИ:\n" + "\n\n".join(lines)[:16000])


def relation_prompt(new_text, ours):
    old = "\n\n".join(f"[{n}] {o['title'] or ''} ({time.strftime('%d.%m.%Y', time.localtime(o['at']))})\n{o['text'][:1200]}" for n, o in enumerate(ours, 1))
    return ("Порівняй НОВИЙ матеріал із нашими попередніми публікаціями. Визнач тип зв'язку:\n"
            "povtor — те саме, нічого нового; dopovnennia — нові деталі до тієї ж події; onovlennia — змінилися ключові факти/цифри/статус; "
            "sprostuvannia — нове прямо заперечує наше; inshe — інша подія.\n"
            "Відповідай ЛИШЕ JSON: {\"relation\": \"povtor|dopovnennia|onovlennia|sprostuvannia|inshe\", \"what\": \"що саме нового/змінилось, до 25 слів\"}\n\n"
            f"НАШІ ПУБЛІКАЦІЇ:\n{old}\n\nНОВИЙ МАТЕРІАЛ:\n{new_text[:3000]}")


REL = {"povtor": "♻️ повтор", "dopovnennia": "➕ доповнення", "onovlennia": "🔁 оновлення", "sprostuvannia": "❌ спростування", "inshe": "інше"}


def angles_prompt(mode, media_topics, facts):
    if mode == "ever":
        task = ("Запропонуй 5 «вічних» тем (evergreen) для медіа — без строку давності, завжди цікаві читачам, практичні. "
                "Для кожної — пошуковий запит українською (3–6 слів), щоб знайти свіжі факти й джерела.")
    else:
        task = ("Запропонуй 5 гострих, але чесних тем-«провокацій»: очевидне й банальне, але болюче, щоб читач думав «а я так і знав» "
                "або «чому я не здогадався». Без образ, без закликів до ворожнечі, без маніпуляції фактами — лише на основі реальних фактів нижче. "
                "Для кожної — пошуковий запит (3–6 слів) для пошуку підтверджень.")
    return (f"{task}\nТематика медіа: {media_topics}.\n" + (f"Свіжі факти для опори:\n{facts[:3000]}\n" if facts else "")
            + "Відповідай ЛИШЕ JSON: {\"angles\": [{\"title\": \"тема/кут\", \"query\": \"запит\"}]}")


# ==================================================================== Word (.docx) без сторонніх бібліотек
def docx_bytes(sections):
    """sections: [(заголовок_документа, [(тип, текст)])], тип: h1 | h2 | p | small | pagebreak"""
    def run(text, bold=False, size=None, color=None):
        props = ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size else "") + (f'<w:color w:val="{color}"/>' if color else "")
        t = _html.escape(text, quote=False)
        return f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{t}</w:t></w:r>'
    body = []
    for n, (_title, blocks) in enumerate(sections):
        if n:
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        for kind, text in blocks:
            if kind == "h1":
                body.append(f'<w:p><w:pPr><w:spacing w:after="200"/></w:pPr>{run(text, True, 36)}</w:p>')
            elif kind == "h2":
                body.append(f'<w:p><w:pPr><w:spacing w:before="200" w:after="80"/></w:pPr>{run(text, True, 28)}</w:p>')
            elif kind == "small":
                body.append(f'<w:p>{run(text, False, 18, "666666")}</w:p>')
            else:
                body.append(f'<w:p><w:pPr><w:spacing w:after="120"/></w:pPr>{run(text, False, 24)}</w:p>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
           "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def article_blocks(d, lang_label, sources_title="Джерела"):
    blocks = [("h1", d["headline"])]
    for para in [p.strip() for p in d["body"].split("\n") if p.strip()]:
        is_sub = len(para) < 90 and not para.endswith((".", "!", "?", "…", ":")) and len(para.split()) <= 12
        blocks.append(("h2" if is_sub else "p", para))
    if d.get("sources"):
        blocks.append(("h2", sources_title))
        blocks += [("p", "• " + s) for s in d["sources"]]
    blocks.append(("small", f"{lang_label} · підготовлено ньюзрумом OrionAI · {time.strftime('%d.%m.%Y %H:%M')} · потребує редакторської перевірки"))
    return blocks


# ==================================================================== сховище пульта
class Desk:
    def __init__(self, store):
        self.store = store
        with closing(store.db()) as con, con:
            con.executescript(SCHEMA)
            cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
            if "story_id" not in cols:
                con.execute("ALTER TABLE items ADD COLUMN story_id INTEGER")
            if "relation" not in cols:
                con.execute("ALTER TABLE items ADD COLUMN relation TEXT DEFAULT ''")
            scol = {r[1] for r in con.execute("PRAGMA table_info(sources)")}
            if "ours" not in scol:
                con.execute("ALTER TABLE sources ADD COLUMN ours INTEGER DEFAULT 0")
            ocol = {r[1] for r in con.execute("PRAGMA table_info(ours_posts)")}
            for col, typ in (("platform", "TEXT DEFAULT 'tg'"), ("views", "INTEGER DEFAULT 0"), ("likes", "INTEGER DEFAULT 0"),
                             ("reposts", "INTEGER DEFAULT 0"), ("comments", "INTEGER DEFAULT 0"), ("metrics_at", "REAL DEFAULT 0")):
                if col not in ocol:
                    con.execute(f"ALTER TABLE ours_posts ADD COLUMN {col} {typ}")
            # Remove legacy generic Astro button, without deleting the new astrology topic.
            con.execute("DELETE FROM topics WHERE key='astro'")
            have = {r[0] for r in con.execute("SELECT key FROM topics") }
            for pos, (k, t, kw) in enumerate(DEFAULT_TOPICS, 1):
                if k not in have and not con.execute("SELECT 1 FROM settings WHERE k=?", ("topic_removed_" + k,)).fetchone():
                    con.execute("INSERT INTO topics(key,title,keywords,position) VALUES(?,?,?,?)", (k, t, kw, pos))

    def db(self):
        return self.store.db()

    def q(self, sql, args=()):
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute(sql, args)]

    def x(self, sql, args=()):
        with closing(self.db()) as con, con:
            return con.execute(sql, args)

    # ----- блокування й спам
    def blocks(self, kind=None):
        return self.q("SELECT * FROM blocks" + (" WHERE kind=?" if kind else "") + " ORDER BY kind, value", (kind,) if kind else ())

    def add_block(self, kind, value):
        value = value.strip().lower()[:120]
        if value:
            self.x("INSERT OR IGNORE INTO blocks(kind,value,at) VALUES(?,?,?)", (kind, value, now()))

    def del_block(self, bid):
        self.x("DELETE FROM blocks WHERE id=?", (int(bid),))

    def blocked(self, text, source="", url=""):
        """-> причина блокування або ''."""
        low = (text or "").lower()
        src = (source or "").lower()
        dom = domain(url)
        for b in self.blocks():
            v = b["value"]
            hit = ((b["kind"] in ("word", "person") and v in low) or (b["kind"] == "source" and v and (v in src or v.lstrip("@") in (url or "").lower()))
                   or (b["kind"] == "domain" and dom and (dom == v or dom.endswith("." + v))))
            if hit:
                self.x("UPDATE blocks SET hits=hits+1 WHERE id=?", (b["id"],))
                return f"заблоковано: {BLOCK_KINDS.get(b['kind'], b['kind'])} «{v}»"
        return ""

    def is_spam(self, text, level):
        if level == "off":
            return ""
        score, reasons = spam_score(text)
        limit = 2 if level == "strict" else 4
        return ("спам: " + "; ".join(reasons)) if score >= limit else ""

    def log_spam(self, reason, origin, source, url, text):
        self.x("INSERT INTO spamlog(at,reason,origin,source,url,text) VALUES(?,?,?,?,?,?)", (now(), reason[:200], origin, source[:120], url[:500], text[:4000]))
        self.x("DELETE FROM spamlog WHERE id NOT IN (SELECT id FROM spamlog ORDER BY id DESC LIMIT 300)")

    # ----- історії (кластери)
    def assign_story(self, title, text, topic="", window_days=14):
        """Знаходить історію для тексту або створює нову. -> (story_id, нова?)"""
        base = (title + "\n" + text)[:3000]
        best, best_id = 0.0, None
        tw = set(normalize(title).split())
        for r in self.q("SELECT story_id, title, text FROM story_refs WHERE at>? ORDER BY id DESC LIMIT 800", (now() - window_days * 86400,)):
            s = similarity(base, (r["title"] + "\n" + r["text"])[:3000])
            ow = set(normalize(r["title"]).split())
            if tw and ow:
                s = max(s, len(tw & ow) / max(1, len(tw | ow)) * 0.9)
            if s > best:
                best, best_id = s, r["story_id"]
        if best >= 0.3 and best_id:
            self.x("UPDATE stories SET updated=?, size=size+1 WHERE id=?", (now(), best_id))
            return best_id, False
        cur = self.x("INSERT INTO stories(title,topic,created,updated,size) VALUES(?,?,?,?,1)", (title[:200], topic[:40], now(), now()))
        return cur.lastrowid, True

    def add_ref(self, story_id, kind, ref_id, title, url="", relation="", note="", text="", at=None):
        self.x("INSERT INTO story_refs(story_id,kind,ref_id,title,url,relation,note,at,text) VALUES(?,?,?,?,?,?,?,?,?)",
               (story_id, kind, ref_id, title[:300], url[:500], relation, note[:400], at or now(), text[:3000]))
        if kind == "ours":
            self.x("UPDATE stories SET ours=1 WHERE id=?", (story_id,))

    def story(self, sid):
        r = self.q("SELECT * FROM stories WHERE id=?", (int(sid),))
        return r[0] if r else None

    def story_refs(self, sid):
        return self.q("SELECT * FROM story_refs WHERE story_id=? ORDER BY at", (int(sid),))

    def stories(self, limit=12):
        return self.q("SELECT * FROM stories ORDER BY updated DESC LIMIT ?", (limit,))

    # ----- наші ресурси
    def add_ours(self, source, url, title, text, at=None, key=None, platform="tg", metrics=None):
        key = key or ("u:" + url if url else "t:" + normalize(text)[:200])
        if self.q("SELECT 1 FROM ours_posts WHERE key=?", (key,)):
            if metrics:
                self.set_metrics(key, metrics)
            return None
        title = title or (text.strip().split("\n")[0][:140])
        sid, _ = self.assign_story(title, text)
        m = metrics or {}
        cur = self.x("INSERT INTO ours_posts(source,url,title,text,at,story_id,key,platform,views,likes,reposts,comments,metrics_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (source[:120], url[:500], title[:300], text[:8000], at or now(), sid, key, platform,
                      int(m.get("views") or 0), int(m.get("likes") or 0), int(m.get("reposts") or 0), int(m.get("comments") or 0),
                      now() if m else 0))
        self.add_ref(sid, "ours", cur.lastrowid, title, url, text=text, at=at)
        return cur.lastrowid

    def set_metrics(self, key_or_url, metrics):
        """Оновлює охоплення нашого допису (перегляди, реакції, поширення, коментарі) — лише вгору, бо лічильники не падають."""
        m = {k: int(metrics.get(k) or 0) for k in ("views", "likes", "reposts", "comments")}
        if not any(m.values()):
            return 0
        key = key_or_url if key_or_url.startswith(("u:", "t:")) else "u:" + key_or_url
        return self.x("UPDATE ours_posts SET views=MAX(views,?), likes=MAX(likes,?), reposts=MAX(reposts,?), comments=MAX(comments,?), "
                      "metrics_at=? WHERE key=?", (m["views"], m["likes"], m["reposts"], m["comments"], now(), key)).rowcount

    def similar_ours(self, text, limit=4, threshold=0.22, days=180):
        out = []
        for o in self.q("SELECT * FROM ours_posts WHERE at>? ORDER BY id DESC LIMIT 1500", (now() - days * 86400,)):
            s = similarity(text, o["text"])
            if s >= threshold:
                out.append((s, o))
        out.sort(key=lambda x: -x[0])
        return [o for _s, o in out[:limit]]

    def ours_count(self):
        return self.q("SELECT COUNT(*) AS n FROM ours_posts")[0]["n"]

    # ----- знахідки
    def add_find(self, origin, ref, c, score=None, prio=None):
        key = "f:" + (c.get("url") or normalize(c["title"])[:200])
        if self.q("SELECT 1 FROM finds WHERE key=?", (key,)):
            return None
        sid, _ = self.assign_story(c["title"], c.get("snippet", ""), (score or {}).get("topic", ""))
        cur = self.x("INSERT INTO finds(origin,ref,title,url,source,author,published,snippet,score,priority,story_id,found,key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (origin, str(ref), c["title"][:300], c.get("url", "")[:600], c.get("source", "")[:120], c.get("author", "")[:120],
                      c.get("published") or 0, c.get("snippet", "")[:600], json.dumps(score or {}, ensure_ascii=False),
                      prio if prio is not None else heuristic_priority(c), sid, now(), key))
        self.add_ref(sid, "find", cur.lastrowid, c["title"], c.get("url", ""), text=c.get("snippet", ""), at=c.get("published") or now())
        return cur.lastrowid

    def find(self, fid):
        r = self.q("SELECT * FROM finds WHERE id=?", (int(fid),))
        if not r:
            return None
        f = r[0]
        f["score"] = json.loads(f["score"] or "{}")
        return f

    def update_find(self, fid, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE finds SET {cols} WHERE id=?", (*kw.values(), int(fid)))

    def finds(self, origin=None, ref=None, folder=None, ids=None, sort="prio", limit=10, offset=0):
        q, a = "SELECT id FROM finds WHERE status!='deleted'", []
        if ids is not None:
            if not ids:
                return []
            q += " AND id IN (%s)" % ",".join("?" * len(ids))
            a += list(ids)
        if origin:
            q += " AND origin=?"
            a.append(origin)
        if ref is not None:
            q += " AND ref=?"
            a.append(str(ref))
        if folder is not None:
            q += " AND folder=?"
            a.append(folder)
        q += {"prio": " ORDER BY priority DESC, published DESC", "time": " ORDER BY published DESC, id DESC",
              "source": " ORDER BY source, published DESC"}.get(sort, " ORDER BY id DESC") + " LIMIT ? OFFSET ?"
        a += [limit, offset]
        return [self.find(r["id"]) for r in self.q(q, a)]

    # ----- теги й теми
    def tags(self):
        return self.q("SELECT * FROM tags ORDER BY id")

    def tag(self, tid):
        r = self.q("SELECT * FROM tags WHERE id=?", (int(tid),))
        return r[0] if r else None

    def topics(self):
        return self.q("SELECT * FROM topics ORDER BY position, id")

    PROFILE_FIELDS = ("tone", "voice", "sources", "direction", "outcome")

    def topic_profile(self, key, inherit=True):
        """Return effective optional editorial settings, with parent fallback."""
        topic = self.topic(key)
        if not topic:
            raise ValueError("Невідома тема")
        key = topic["key"]
        parent = key.split("_", 1)[0] if "_" in key else None
        base = self.topic_profile(parent, inherit=True) if inherit and parent and self.topic(parent) else {k: "" for k in self.PROFILE_FIELDS}
        row = self.q("SELECT * FROM topic_profiles WHERE topic_key=?", (key,))
        for field in self.PROFILE_FIELDS:
            if row and row[0][field]:
                base[field] = row[0][field]
        return base

    def save_topic_profile(self, key, **settings):
        """Update only provided fields; blank value restores inherited/default value."""
        if not self.topic(key):
            raise ValueError("Невідома тема")
        unknown = set(settings) - set(self.PROFILE_FIELDS)
        if unknown:
            raise ValueError("Невідомі поля: " + ", ".join(sorted(unknown)))
        if not settings:
            return self.topic_profile(key)
        current = self.q("SELECT * FROM topic_profiles WHERE topic_key=?", (str(key),))
        data = {field: current[0][field] if current else "" for field in self.PROFILE_FIELDS}
        for field, value in settings.items():
            if not isinstance(value, str) or len(value) > 4000:
                raise ValueError("Значення має бути текстом до 4000 символів")
            data[field] = value.strip()
        with closing(self.db()) as con, con:
            con.execute("INSERT INTO topic_profiles(topic_key,tone,voice,sources,direction,outcome,updated) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(topic_key) DO UPDATE SET "
                        "tone=excluded.tone,voice=excluded.voice,sources=excluded.sources,"
                        "direction=excluded.direction,outcome=excluded.outcome,updated=excluded.updated",
                        (str(key), *(data[k] for k in self.PROFILE_FIELDS), now()))
        return self.topic_profile(key)

    def editorial_instructions(self, key):
        """Optional preferences for prompts, never a substitute for fact-checking."""
        profile = self.topic_profile(key)
        labels = {"tone": "Тон", "voice": "Голос", "sources": "Пріоритетні джерела", "direction": "Напрямок", "outcome": "Бажаний результат"}
        lines = [f"{labels[k]}: {profile[k]}" for k in self.PROFILE_FIELDS if profile[k]]
        if not lines:
            return "Стандартні редакційні правила GreenPost."
        return ("Додаткові побажання редактора (не скасовують перевірку фактів, атрибуцію та погодження):\n"
                + "\n".join(lines))

    def matches_topic(self, topic, text, category=""):
        """Чи стосується текст теми: ключові слова (частини слів) або категорія, яку дала модель."""
        low = (text or "").lower()
        name = topic["title"].split(" ", 1)[-1].lower()
        if category and (category.lower() in name or name.split()[0] in category.lower()):
            return True
        return any(k.strip().lower() and k.strip().lower() in low for k in topic["keywords"].split(","))

    def topic(self, key):
        r = self.q("SELECT * FROM topics WHERE key=? OR id=?", (str(key), int(key) if str(key).isdigit() else -1))
        return r[0] if r else None
