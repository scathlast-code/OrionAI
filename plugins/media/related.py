"""🔗 Пов'язане скачане: автор, тема, географія.

Кожне надіслане медіа записується в історію чату (ід повідомлення, автор, сайт, теми, місця).
Новий файл отримує в підписі гіперпосилання «попереднє з Карпат», «попереднє зі снігопадом»,
а під ним — кнопки «попереднє відео автора» окремо для кожної соцмережі (іконкою).
Посилання ведуть у бот (t.me/<бот>?start=mh_<ід>): бот відповідає на старе повідомлення —
дотик до цитати переносить до нього (в особистому чаті Telegram не дає прямих посилань на повідомлення).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

B = r"(?<![\w'’])"  # початок слова (кирилиця теж \w)

# (ключ, підпис «попереднє …», шаблони, емодзі)
TOPICS = [
    ("snow", "зі снігопадом", ["сніг", "сніж", "хуртовин", "заметіл", "завірюх", "забіл"], "❄️🌨☃️"),
    ("rain", "з дощем", ["дощ", "злив"], "🌧☔"),
    ("storm", "з грозою", ["гроз", "блискав", r"грім\b"], "⛈🌩"),
    ("wind", "з буревієм", ["буревій", "ураган", "шквал", "смерч", "торнадо"], "🌪"),
    ("flood", "з повінню", ["повін", "паводк", "підтоп", "затоп"], ""),
    ("fire", "з пожежею", ["пожеж", "підпал", r"горить\b"], ""),
    ("frost", "з морозом", ["мороз", "ожелед", "крига"], "🥶"),
    ("fog", "з туманом", ["туман"], "🌫"),
    ("heat", "зі спекою", ["спек[аиоу]"], "🥵"),
    ("aurora", "з полярним сяйвом", ["полярн\\w* сяйв", "сяйв"], ""),
    ("pets", "з тваринами", ["собак", "песик", "кішк", "котик", "кошен", "цуцен", "тварин"], "🐶🐱"),
    ("birds", "з птахами", ["птах", "лелек", "лебед", "журавл"], ""),
    ("wild", "з дикими тваринами", ["зубр", "оленя", "олені", "вовк", "лисиц", "ведмед", "кабан", "лось", "лося"], ""),
    ("trash", "зі сміттям", ["смітт", "звалищ"], ""),
    ("forest", "з лісом", [r"ліс(?:у|ом|і|и|ів|ах)?\b", "вируб"], ""),
    ("sea", "з морем", [r"мор(?:е|я|і|ем)\b", "пляж"], ""),
    ("mountains", "з горами", [r"гори\b", r"гір\b", r"горах\b", r"горами\b", "вершин"], ""),
    ("blackout", "з відключеннями", ["відключен", "блекаут", "без світла"], ""),
    ("shelling", "з обстрілом", ["обстріл", "вибух", "прильот", "шахед", "ракет"], ""),
    ("food", "з їжею", ["рецепт", "смачн"], ""),
]

GEOS = [
    ("volyn", "з Волині", ["волин", "луцьк", "ковел", "шацьк", "світяз"]),
    ("karpaty", "з Карпат", ["карпат", "буковел", "говерл", "яремч", "драгобрат", "ворохт", "славськ", "чорногор"]),
    ("zakarpattia", "із Закарпаття", ["закарпат", "ужгород", "мукачев", "синевир", "рахів", "рахов"]),
    ("prykarpattia", "з Прикарпаття", ["франківськ", "прикарпат"]),
    ("bukovyna", "з Буковини", ["чернівц", "буковин"]),
    ("lviv", "зі Львівщини", ["львів", "львов"]),
    ("ternopil", "з Тернопільщини", ["тернопіл", "тернопол"]),
    ("rivne", "з Рівненщини", [r"рівне\b", "рівному", "рівненщ", "рівненськ"]),
    ("khmelnytskyi", "з Хмельниччини", ["хмельниц", "хмельничч", "кам'янець", "камянець"]),
    ("zhytomyr", "з Житомирщини", ["житомир"]),
    ("vinnytsia", "з Вінниччини", ["вінниц", "вінничч"]),
    ("kyiv", "з Києва й Київщини", ["київ", "києв", "кияни", "ірпін", "бровар"]),
    ("chernihiv", "з Чернігівщини", ["чернігів", "чернігов"]),
    ("sumy", "з Сумщини", [r"суми\b", r"сумах\b", "сумщин", "сумськ"]),
    ("poltava", "з Полтавщини", ["полтав"]),
    ("cherkasy", "з Черкащини", ["черкас", "черкащ"]),
    ("kirovohrad", "з Кіровоградщини", ["кропивницьк", "кіровоград"]),
    ("kharkiv", "з Харківщини", ["харків", "харков"]),
    ("dnipro", "з Дніпропетровщини", ["дніпропетр", r"дніпрі\b", "кривий ріг", "кривого рогу", "кривому розі"]),
    ("zaporizhzhia", "із Запоріжжя", ["запоріж", "запорож"]),
    ("donetsk", "з Донеччини", ["донецьк", "донечч", "краматорськ", "слов'янськ", "словянськ", "покровськ", "бахмут"]),
    ("luhansk", "з Луганщини", ["луганськ", "луганщ", "сєвєродонецьк"]),
    ("odesa", "з Одещини", ["одес", "одещ"]),
    ("mykolaiv", "з Миколаївщини", ["миколаїв", "миколаєв"]),
    ("kherson", "з Херсонщини", ["херсон"]),
    ("crimea", "з Криму", [r"крим\b", "криму", "кримськ", "ялт", "севастопол"]),
]

ICONS = (("instagram", "📸"), ("threads", "🧵"), ("tiktok", "🎵"), ("youtube", "▶️"), ("facebook", "📘"),
         ("twitter", "✖️"), ("x", "✖️"), ("telegram", "✈️"), ("reddit", "👽"), ("tumblr", "📝"), ("vimeo", "🎬"),
         ("pinterest", "📌"), ("soundcloud", "🎧"))

_TOP = [(k, lab, re.compile(B + "(?:" + "|".join(p) + ")", re.I), emo) for k, lab, p, emo in TOPICS]
_GEO = [(k, lab, re.compile(B + "(?:" + "|".join(p) + ")", re.I)) for k, lab, p in GEOS]
TOPIC_LABEL = {k: lab for k, lab, _p, _e in TOPICS}
GEO_LABEL = {k: lab for k, lab, _p in GEOS}
TOPIC_NAME = {"snow": "снігопад", "rain": "дощ", "storm": "гроза", "wind": "буревій", "flood": "повінь", "fire": "пожежа",
              "frost": "мороз", "fog": "туман", "heat": "спека", "aurora": "полярне сяйво", "pets": "тварини", "birds": "птахи",
              "wild": "дикі тварини", "trash": "сміття", "forest": "ліс", "sea": "море", "mountains": "гори",
              "blackout": "відключення", "shelling": "обстріли", "food": "їжа"}
GEO_NAME = {"volyn": "Волинь", "karpaty": "Карпати", "zakarpattia": "Закарпаття", "prykarpattia": "Прикарпаття",
            "bukovyna": "Буковина", "lviv": "Львівщина", "ternopil": "Тернопільщина", "rivne": "Рівненщина",
            "khmelnytskyi": "Хмельниччина", "zhytomyr": "Житомирщина", "vinnytsia": "Вінниччина", "kyiv": "Київ і Київщина",
            "chernihiv": "Чернігівщина", "sumy": "Сумщина", "poltava": "Полтавщина", "cherkasy": "Черкащина",
            "kirovohrad": "Кіровоградщина", "kharkiv": "Харківщина", "dnipro": "Дніпропетровщина", "zaporizhzhia": "Запоріжжя",
            "donetsk": "Донеччина", "luhansk": "Луганщина", "odesa": "Одещина", "mykolaiv": "Миколаївщина",
            "kherson": "Херсонщина", "crimea": "Крим"}
KEEP = 3000  # записів на чат


def tags(text):
    """Теми й місця з тексту допису (слова й емодзі). Порядок — як у словниках."""
    s = (text or "").replace("’", "'").replace("ʼ", "'")
    topics = [k for k, _l, rx, emo in _TOP if rx.search(s) or (emo and any(e in s for e in emo))]
    geos = [k for k, _l, rx in _GEO if rx.search(s)]
    return topics, geos


def norm_author(name):
    return re.sub(r"[^\w]", "", str(name or "").lower().lstrip("@"))


def icon(site):
    s = str(site or "").lower()
    for key, ic in ICONS:
        if s == key or s.startswith(key):
            return ic
    return "🌐"


class History:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _load(self):
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)

    def items(self, chat):
        return self._load().get(str(chat), [])

    def get(self, chat, rid):
        return next((e for e in self.items(chat) if e.get("id") == rid), None)

    def add(self, chat, **entry):
        with self.lock:
            data = self._load()
            lst = data.setdefault(str(chat), [])
            entry["id"] = (max((e.get("id", 0) for e in lst), default=0) + 1)
            entry.setdefault("ts", int(time.time()))
            lst.append(entry)
            del lst[:-KEEP]
            self._save(data)
            return entry["id"]

    def related(self, chat, author, topics, geos):
        """Останнє скачане: того ж автора (окремо для кожного сайту), з тим же місцем, на ту ж тему.
        Без теми / без місця — якщо в цьому дописі їх не знайшлось. «Вперше» — теми й місця, яких ще не було.
        Порожніх груп не буває: повертаємо лише те, що справді вже скачувалось."""
        lst = self.items(chat)
        a = norm_author(author)
        by_site, by_geo, by_topic, extra = {}, {}, {}, {}
        seen_t, seen_g = set(), set()
        for e in reversed(lst):  # найновіші спершу
            if a and e.get("author") == a:
                by_site.setdefault(e.get("site") or "", e)
            for g in e.get("geos", []):
                seen_g.add(g)
                if g in geos:
                    by_geo.setdefault(g, e)
            for t in e.get("topics", []):
                seen_t.add(t)
                if t in topics:
                    by_topic.setdefault(t, e)
            if not topics and not e.get("topics"):
                extra.setdefault("notopic", e)
            if not geos and not e.get("geos"):
                extra.setdefault("nogeo", e)
            if e.get("first"):
                extra.setdefault("first", e)
        first = [("t", t) for t in topics if t not in seen_t] + [("g", g) for g in geos if g not in seen_g]
        if not first:
            extra.pop("first", None)  # «попереднє вперше» — лише під новим «вперше»
        return by_site, by_geo, by_topic, extra, first

    def groups(self, chat):
        """Підрахунок для архіву: автори, теми, місця, без теми, без місця, вперше (автори «вперше» не групуються)."""
        out = {"a": {}, "t": {}, "g": {}, "x": {}}
        for e in self.items(chat):
            if e.get("author"):
                out["a"].setdefault(e["author"], []).append(e)
            for t in e.get("topics", []):
                out["t"].setdefault(t, []).append(e)
            for g in e.get("geos", []):
                out["g"].setdefault(g, []).append(e)
            if not e.get("topics"):
                out["x"].setdefault("notopic", []).append(e)
            if not e.get("geos"):
                out["x"].setdefault("nogeo", []).append(e)
            if e.get("first"):
                out["x"].setdefault("first", []).append(e)
        return out


EXTRA_LABEL = {"notopic": "без теми", "nogeo": "без місця", "first": "«вперше»"}
EXTRA_NAME = {"notopic": "🏷 Без теми", "nogeo": "📍 Без місця", "first": "🆕 Вперше"}


def first_text(first):
    parts = [("тема «" + TOPIC_NAME.get(k, k) + "»") if kind == "t" else ("місце «" + GEO_NAME.get(k, k) + "»") for kind, k in first]
    return ", ".join(parts)

def ts_text(ts):
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        return _dt.datetime.fromtimestamp(int(ts), ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
    except Exception:  # noqa: BLE001
        return ""
