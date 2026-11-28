"""🔗 Пов'язане скачане: автор, тема, локація, мова.

Кожне надіслане медіа записується в історію чату (ід повідомлення, автор, сайт, теми, локації, мова).
Тему, локацію й мову визначає ШІ (тем і місць без обмежень, увесь світ) з тексту допису і геотегу
(в Instagram і Facebook геотег над дописом, у Threads і X — під ним; бот бере його з даних сторінки, де б він не стояв);
якщо ШІ недоступний — запасний словник нижче. Нові назви запам'ятовуються, щоб групи не дробились.
Новий файл отримує в підписі гіперпосилання «попереднє з Карпат», «попереднє зі снігопадом», «попереднє без локації»,
«попереднє українською», а під ним — кнопки «попереднє відео автора» окремо для кожної соцмережі (іконкою).
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

# Запасний словник (назва, «попереднє …», шаблони, емодзі)
TOPICS = [
    ("снігопад", "зі снігопадом", ["сніг", "сніж", "хуртовин", "заметіл", "завірюх", "забіл", "засипа", "замело", "заміт",
                                   r"біл\w* ковдр", "білим-біло", r"мете\b", "сипле", "перший пухнаст"], "❄️🌨☃️⛄"),
    ("дощ", "з дощем", ["дощ", "злив", r"ллє\b", "лило", "мряк", "калюж", "парасоль"], "🌧☔☂️"),
    ("гроза", "з грозою", ["гроз", "блискав", r"грім\b", "гримить", "загриміл"], "⛈🌩⚡"),
    ("буревій", "з буревієм", ["буревій", "ураган", "шквал", "смерч", "торнадо"], "🌪"),
    ("повінь", "з повінню", ["повін", "паводк", "підтоп", "затоп"], ""),
    ("пожежа", "з пожежею", ["пожеж", "підпал", r"горить\b"], ""),
    ("мороз", "з морозом", ["мороз", "ожелед", "крига", "мінус", "задубі", "холоднеч", "іній", "паморозь"], "🥶🧊"),
    ("туман", "з туманом", ["туман"], "🌫"),
    ("спека", "зі спекою", ["спек[аиоу]", "пекло на вулиці", "плюс сорок", "задуха", "розпечен"], "🥵🌡"),
    ("полярне сяйво", "з полярним сяйвом", [r"полярн\w* сяйв", "сяйв"], ""),
    ("веселка", "з веселкою", ["веселк", "райдуг"], "🌈"),
    ("свійські тварини", "зі свійськими тваринами", ["собак", "песик", "кішк", "котик", "кошен", "цуцен"], "🐶🐱"),
    ("птахи", "з птахами", ["птах", "лелек", "лебед", "журавл"], ""),
    ("дикі тварини", "з дикими тваринами", ["зубр", "оленя", "олені", "вовк", "лисиц", "ведмед", "кабан", "лось", "лося"], ""),
    ("сміття", "зі сміттям", ["смітт", "звалищ"], ""),
    ("ліс", "з лісом", [r"ліс(?:у|ом|і|и|ів|ах)?\b", "вируб"], ""),
    ("море", "з морем", [r"мор(?:е|я|і|ем)\b", "пляж"], ""),
    ("гори", "з горами", [r"гори\b", r"гір\b", r"горах\b", r"горами\b", "вершин"], ""),
    ("відключення світла", "з відключеннями світла", ["відключен", "блекаут", "без світла"], ""),
    ("обстріли", "з обстрілом", ["обстріл", "вибух", "прильот", "шахед", "ракет"], ""),
    ("їжа", "з їжею", ["рецепт", "смачн"], ""),
    ("дорога", "з дорогою", ["дтп", "аварі", r"трас(?:а|і|у)\b", "затор"], ""),
    ("свято", "зі святом", ["свят", "фестивал", "ярмарк"], "🎄🎉"),
    ("спорт", "зі спортом", ["матч", "футбол", "забіг", "марафон", "турнір"], "⚽"),
    ("музика", "з музикою", ["концерт", "пісн", "співа"], "🎶"),
    ("діти", "з дітьми", ["дитин", r"діти\b", "дітей", "малюк"], ""),
    ("риболовля", "з риболовлею", ["рибал", "риболов", "щук", "короп"], "🎣"),
]

GEOS = [
    ("Волинь", "з Волині", ["волин", "луцьк", "ковел", "шацьк", "світяз"]),
    ("Карпати", "з Карпат", ["карпат", "буковел", "говерл", "яремч", "драгобрат", "ворохт", "славськ", "чорногор"]),
    ("Закарпаття", "із Закарпаття", ["закарпат", "ужгород", "мукачев", "синевир", "рахів", "рахов"]),
    ("Прикарпаття", "з Прикарпаття", ["франківськ", "прикарпат"]),
    ("Буковина", "з Буковини", ["чернівц", "буковин"]),
    ("Львівщина", "зі Львівщини", ["львів", "львов"]),
    ("Тернопільщина", "з Тернопільщини", ["тернопіл", "тернопол"]),
    ("Рівненщина", "з Рівненщини", [r"рівне\b", "рівному", "рівненщ", "рівненськ"]),
    ("Хмельниччина", "з Хмельниччини", ["хмельниц", "хмельничч", "кам'янець", "камянець"]),
    ("Житомирщина", "з Житомирщини", ["житомир"]),
    ("Вінниччина", "з Вінниччини", ["вінниц", "вінничч"]),
    ("Київ і Київщина", "з Києва й Київщини", ["київ", "києв", "кияни", "ірпін", "бровар"]),
    ("Чернігівщина", "з Чернігівщини", ["чернігів", "чернігов"]),
    ("Сумщина", "з Сумщини", [r"суми\b", r"сумах\b", "сумщин", "сумськ"]),
    ("Полтавщина", "з Полтавщини", ["полтав"]),
    ("Черкащина", "з Черкащини", ["черкас", "черкащ"]),
    ("Кіровоградщина", "з Кіровоградщини", ["кропивницьк", "кіровоград"]),
    ("Харківщина", "з Харківщини", ["харків", "харков"]),
    ("Дніпропетровщина", "з Дніпропетровщини", ["дніпропетр", r"дніпрі\b", "кривий ріг", "кривого рогу", "кривому розі"]),
    ("Запоріжжя", "із Запоріжжя", ["запоріж", "запорож"]),
    ("Донеччина", "з Донеччини", ["донецьк", "донечч", "краматорськ", "слов'янськ", "словянськ", "покровськ", "бахмут"]),
    ("Луганщина", "з Луганщини", ["луганськ", "луганщ", "сєвєродонецьк"]),
    ("Одещина", "з Одещини", ["одес", "одещ"]),
    ("Миколаївщина", "з Миколаївщини", ["миколаїв", "миколаєв"]),
    ("Херсонщина", "з Херсонщини", ["херсон"]),
    ("Крим", "з Криму", [r"крим\b", "криму", "кримськ", "ялт", "севастопол"]),
    # світ
    ("Польща", "з Польщі", ["польщ", "польськ", "варшав", "краків", "закопан"]),
    ("Словаччина", "зі Словаччини", ["словаччин", "словацьк", "братислав", "татр"]),
    ("Чехія", "з Чехії", ["чехі", "чеськ", r"праг(?:а|и|і|у)\b"]),
    ("Угорщина", "з Угорщини", ["угорщин", "угорськ", "будапешт"]),
    ("Румунія", "з Румунії", ["румуні", "румунськ", "бухарест"]),
    ("Молдова", "з Молдови", ["молдов", "кишинів", "кишинев"]),
    ("Німеччина", "з Німеччини", ["німеччин", "німецьк", "берлін", "мюнхен"]),
    ("Австрія", "з Австрії", ["австрі", "австрійськ"]),
    ("Швейцарія", "зі Швейцарії", ["швейцар", "цюрих", "женев"]),
    ("Альпи", "з Альп", [r"альп(?:и|ах|ами)?\b", "альпійськ"]),
    ("Італія", "з Італії", ["італі", "італійськ", "венеці", "мілан"]),
    ("Франція", "з Франції", ["франці", "французьк", "париж"]),
    ("Іспанія", "з Іспанії", ["іспані", "іспанськ", "мадрид", "барселон"]),
    ("Португалія", "з Португалії", ["португалі", "лісабон"]),
    ("Велика Британія", "з Великої Британії", ["британі", "британськ", "лондон", "шотланді"]),
    ("Нідерланди", "з Нідерландів", ["нідерланд", "голланді", "амстердам"]),
    ("Скандинавія", "зі Скандинавії", ["норвегі", "швеці", "фінлянді", "ісланді", "скандинав"]),
    ("Балтія", "з Балтії", ["литв", "латві", "естоні", "вільнюс", "таллін"]),
    ("Грузія", "з Грузії", ["грузі", "тбіліс", "батум"]),
    ("Туреччина", "з Туреччини", ["туреччин", "турецьк", "стамбул", "анталі"]),
    ("Греція", "з Греції", ["греці", "грецьк", "афін"]),
    ("Хорватія", "з Хорватії", ["хорвачі", "хорватськ"]),
    ("Єгипет", "з Єгипту", ["єгипт"]),
    ("Ізраїль", "з Ізраїлю", ["ізраїл"]),
    ("ОАЕ", "з ОАЕ", [r"оае\b", "дубай", "дубаї", "емірат"]),
    ("США", "зі США", [r"сша\b", "америк", "нью-йорк", "каліфорні", "флорид", "техас"]),
    ("Канада", "з Канади", ["канад", "торонто", "ванкувер"]),
    ("Японія", "з Японії", ["японі", "японськ", "токіо"]),
    ("Китай", "з Китаю", [r"китай\b", "китаю", "китайськ", "пекін"]),
    ("Таїланд", "з Таїланду", ["таїланд", "тайланд", "бангкок", "пхукет"]),
    ("Індія", "з Індії", [r"індії\b", r"індія\b", "індійськ"]),
    ("Австралія", "з Австралії", ["австралі"]),
    ("Антарктида", "з Антарктиди", ["антаркт", "вернадськ"]),
]

# Мови: (назва, «попереднє …»)
LANGS = {"uk": ("українська", "українською"), "en": ("англійська", "англійською"), "pl": ("польська", "польською"),
         "de": ("німецька", "німецькою"), "fr": ("французька", "французькою"), "es": ("іспанська", "іспанською"),
         "it": ("італійська", "італійською"), "pt": ("португальська", "португальською"), "cs": ("чеська", "чеською"),
         "sk": ("словацька", "словацькою"), "ro": ("румунська", "румунською"), "hu": ("угорська", "угорською"),
         "tr": ("турецька", "турецькою"), "ka": ("грузинська", "грузинською"), "ru": ("російська", "російською"),
         "be": ("білоруська", "білоруською"), "ja": ("японська", "японською"), "zh": ("китайська", "китайською"),
         "ar": ("арабська", "арабською"), "he": ("іврит", "івритом"), "lt": ("литовська", "литовською"),
         "lv": ("латиська", "латиською"), "et": ("естонська", "естонською"), "nl": ("нідерландська", "нідерландською")}

ICONS = (("instagram", "📸"), ("threads", "🧵"), ("tiktok", "🎵"), ("youtube", "▶️"), ("facebook", "📘"),
         ("twitter", "✖️"), ("x", "✖️"), ("telegram", "✈️"), ("reddit", "👽"), ("tumblr", "📝"), ("vimeo", "🎬"),
         ("pinterest", "📌"), ("soundcloud", "🎧"))

EXTRA_LABEL = {"notopic": "без теми", "nogeo": "без локації", "first": "«вперше»"}
EXTRA_NAME = {"notopic": "🏷 Без теми", "nogeo": "📍 Без локації", "first": "🆕 Вперше"}
KINDS = {"t": "topics", "g": "geos", "l": "langs"}
KEEP = 3000  # записів на чат


def key_of(name):
    return re.sub(r"\s+", " ", str(name or "").replace("’", "'").replace("ʼ", "'").strip().lower())[:60]


def _emojis(emo):
    """«❄️🌨☃️» -> ["❄", "🌨", "☃"]: без невидимих знаків-модифікаторів (вони є в багатьох емодзі й давали хибні збіги)."""
    return [c for c in emo if c not in "\ufe0f\u200d"]


_TOP = [(key_of(n), lab, re.compile(B + "(?:" + "|".join(p) + ")", re.I), _emojis(emo)) for n, lab, p, emo in TOPICS]
_GEO = [(key_of(n), lab, re.compile(B + "(?:" + "|".join(p) + ")", re.I)) for n, lab, p in GEOS]
SEED = {"t": {key_of(n): {"name": n, "phrase": lab} for n, lab, _p, _e in TOPICS},
        "g": {key_of(n): {"name": n, "phrase": lab} for n, lab, _p in GEOS},
        "l": {key_of(n): {"name": n, "phrase": lab} for n, lab in LANGS.values()}}
OLD = {"snow": "снігопад", "rain": "дощ", "storm": "гроза", "wind": "буревій", "flood": "повінь", "fire": "пожежа",
       "frost": "мороз", "fog": "туман", "heat": "спека", "aurora": "полярне сяйво", "pets": "свійські тварини",
       "birds": "птахи", "wild": "дикі тварини", "trash": "сміття", "forest": "ліс", "sea": "море", "mountains": "гори",
       "blackout": "відключення світла", "shelling": "обстріли", "food": "їжа", "volyn": "волинь", "karpaty": "карпати",
       "zakarpattia": "закарпаття", "lviv": "львівщина", "kyiv": "київ і київщина"}  # записи першої версії


def tags(text):
    """Теми й локації зі словника -> ([(назва, «попереднє …»)], [(назва, «попереднє …»)])."""
    s = (text or "").replace("’", "'").replace("ʼ", "'")
    topics = [(SEED["t"][k]["name"], lab) for k, lab, rx, emo in _TOP if rx.search(s) or (emo and any(e in s for e in emo))]
    geos = [(SEED["g"][k]["name"], lab) for k, lab, rx in _GEO if rx.search(s)]
    return topics, geos


def guess_lang(text):
    """Мова допису за літерами (запасний спосіб) -> код або ''."""
    s = re.sub(r"https?://\S+|[@#]\w+", " ", (text or "").lower())
    letters = re.findall(r"[^\W\d_]", s)
    if len(letters) < 8:
        return ""
    cyr = sum(1 for c in letters if "а" <= c <= "я" or c in "іїєґёыэъў")
    if cyr > len(letters) / 2:
        if re.search(r"[іїєґ]", s):
            return "uk"
        if "ў" in s:
            return "be"
        if re.search(r"[ыэъё]", s):
            return "ru"
        return "uk"
    if re.search(r"[ąęłńśźż]", s):
        return "pl"
    if re.search(r"[ěřůť]", s):
        return "cs"
    if re.search(r"[äöüß]", s):
        return "de"
    if re.search(r"[ğış]", s):
        return "tr"
    if re.search(r"[ñ¿¡]", s):
        return "es"
    if re.search(r"[ăâîșț]", s):
        return "ro"
    if re.search(r"[ა-ჰ]", s):
        return "ka"
    if re.search(r"[぀-ヿ]", s):
        return "ja"
    if re.search(r"[一-鿿]", s):
        return "zh"
    if re.search(r"[؀-ۿ]", s):
        return "ar"
    if re.search(r"[֐-׿]", s):
        return "he"
    if re.search(r"[a-z]", s) and not re.search(r"[àâçèéêëîïôùûœ]", s):
        return "en"
    return ""


AI_USER = (" Користувач сам попросив поставити теги — обов'язково додай кожен у topics або places (визнач, тема це чи місце), "
           "виправивши відмінок: name — називний, phrase — для «попереднє …» (полісся → Полісся, «з Полісся»; озером → озеро, «з озером»).")

AI_SYSTEM = ("Ти визначаєш тему, локацію й мову допису з соцмережі для групування скачаних відео. Відповідай лише JSON без пояснень: "
             '{"topics":[{"name":"снігопад","phrase":"зі снігопадом"}],"places":[{"name":"Карпати","phrase":"з Карпат"}],"lang":"uk"}. '
             "name — українською, називний відмінок, коротко (1–3 слова), тема з малої літери, місце з великої. "
             "phrase — продовження слів «попереднє …» з правильним прийменником і відмінком (з Волині, зі снігопадом, про вибори, із Закарпаття). "
             "Теми: 1–3 головні, що видно в дописі (погода, явище, подія, тварини, люди, справа, настрій тощо). "
             "Враховуй емодзі (❄️🌨 — снігопад, 🌧 — дощ, 🐶 — тварини…) і непрямі звороти (\"Карпати забіліли\" = снігопад, "
             "\"засипало\", \"ллє як з відра\" = дощ, \"пекло на вулиці\" = спека): тема — що відбувається, навіть якщо слово не назване прямо. "
             "Локації: що конкретно вказано в тексті чи геотегу — регіон/область, місто, гори, країна; увесь світ. "
             "Якщо місце за кордоном — додай і країну окремим пунктом. Для України — область чи відомий регіон (Карпати, Волинь), без слова «Україна». "
             "lang — код мови тексту допису (uk, en, pl, de…), без тексту — порожньо. "
             "Якщо значення збігається з назвою зі списку «вже є» — повтори її дослівно. Нічого не вигадуй: немає локації — places: [], "
             "незрозуміла тема — topics: [].")


def ai_prompt(text, geotag, known_t, known_g, user_tags=(), seen=False):
    ask = f"\n\nКористувач просить поставити теги: {', '.join(user_tags)}" if user_tags else ""
    look = ("\n\nДо запиту додано кадр із фото чи відео допису: визнач теми й за тим, що на ньому видно "
            "(тварини, природа, погода, подія, місце), а не лише за текстом. Місце за картинкою не вгадуй — лише з тексту й геотегу."
            if seen else "")
    return (f"Текст допису:\n{(text or '')[:1500]}\n\nГеотег допису: {geotag or 'немає'}{ask}{look}\n\n"
            f"Теми, що вже є: {', '.join(known_t[:120])}\nЛокації, що вже є: {', '.join(known_g[:120])}")


def parse_ai(answer):
    """Відповідь ШІ (JSON) -> ([(назва, фраза)], [(назва, фраза)], код мови)."""
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return [], [], ""
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return [], [], ""
    out = []
    for key in ("topics", "places"):
        items = []
        for it in (data.get(key) or [])[:4]:
            if isinstance(it, dict) and str(it.get("name", "")).strip():
                name = str(it["name"]).strip()[:40]
                phrase = str(it.get("phrase") or "").strip()[:50] or ("про " + name if key == "topics" else "· " + name)
                items.append((name, phrase))
        out.append(items)
    lang = str(data.get("lang") or "").strip().lower()[:3]
    return out[0], out[1], lang if re.fullmatch(r"[a-z]{2,3}", lang) else ""


def lang_pair(code):
    if not code:
        return []
    name, phrase = LANGS.get(code, (code, "мовою " + code))
    return [(name, phrase)]


def merge(*lists):
    seen, out = set(), []
    for lst in lists:
        for name, phrase in lst:
            if key_of(name) and key_of(name) not in seen:
                seen.add(key_of(name))
                out.append((name, phrase))
    return out


# ✍️ Вказівки користувача разом із посиланням: «сортуй по тегу полісся», «групуй всі за озером»,
# «не став тег снігопад», «без тегів»
_VERB = r"(?:сортуй|сортувати|групуй|групувати|додай|постав|став|познач|тег|теги|тегом|тегу|категорі\w*)"
_NO_ALL = re.compile(r"(?:без|не\s+(?:став|ставь|додавай|треба|потрібно|сортуй|групуй|познач)\w*)\s+(?:жодних\s+|ніяких\s+|взагалі\s+)?"
                     r"(?:тег(?:и|ів)|сортуван\w*|групуван\w*|категорі[йї]|позначок)\b|не\s+(?:сортуй|групуй)\s*(?:взагалі|ніяк)?\s*$", re.I)
_NO_ONE = re.compile(r"(?:без|не\s+(?:став|ставь|додавай|сортуй|групуй|познач|треба)\w*)\s+(?:тег\w*|категорі\w*|по|за|як)?\s*[«\"']?([^,.;!?\n«»\"']{2,40})", re.I)
_ADD = re.compile(r"(?:сортуй|сортувати|групуй|групувати|додай|постав|познач)\w*\s+(?:(?:всі|все|усі|усе|його|це|їх)\s+)?"
                  r"(?:(?:по|за|під|в|у|як|з)\s+)?(?:тег\w*|категорі\w*|темі|темою|тему|локаці\w*)?\s*(?:(?:по|за|під|з)\s+)?[«\"'#]?([^,.;!?\n«»\"']{2,40})"
                  r"|(?:^|\s)(?:тег|теги|категорія)\s*[:\-]?\s*[«\"']?([^,.;!?\n«»\"']{2,40})|#([\w'’-]{2,40})", re.I)
_TRIM = re.compile(r"^(?:тег\w*|категорі\w*|по|за|під|з|із|зі|як|темі|темою|тему|всі|все|усі|відео)\s+|\s+(?:будь ласка|плз|pls|і|й|та|а|але|ще)$", re.I)


def clean_tag(raw):
    t = (raw or "").strip(" «»\"'#-—:")
    for _ in range(4):
        t2 = _TRIM.sub("", t).strip()
        if t2 == t:
            break
        t = t2
    return t[:40]


def parse_instr(text):
    """Текст поруч із посиланням -> {"add": [...], "drop": [...], "none": bool} або None, якщо це не вказівки про теги."""
    s = re.sub(r"https?://\S+", " ", text or "").strip()
    if not s or not re.search(_VERB + r"|#\w", s, re.I):
        return None
    out = {"add": [], "drop": [], "none": False}
    if _NO_ALL.search(s):
        out["none"] = True
        return out
    rest = s
    for m in _NO_ONE.finditer(s):
        t = clean_tag(m.group(1))
        if t:
            out["drop"].append(t)
        rest = rest.replace(m.group(0), " ")
    for m in _ADD.finditer(rest):
        for part in re.split(r"\s+(?:і|й|та|а також)\s+|\s*,\s*", next(g for g in m.groups() if g is not None)):
            t = clean_tag(part)
            if t and t.lower() not in ("тег", "теги") and t not in out["add"]:
                out["add"].append(t)
    return out if (out["add"] or out["drop"]) else None


def stem_rx(name):
    """Шаблон для пошуку назви в іншому тексті: основа кожного слова (Полісся → поліс…, озеро → озер…)."""
    words = [w for w in re.findall(r"[\w'’]+", key_of(name)) if len(w) > 2]
    if not words:
        return None
    parts = [re.escape(w[:max(4, len(w) - 2)] if len(w) > 4 else w) for w in words]
    return re.compile(B + r"\w*\W+".join(parts), re.I)


def norm_url(url):
    """Одне й те саме посилання в різних виглядах -> однаковий ключ (без www/m., ?igsh=…, слеша в кінці; threads.net = threads.com)."""
    from urllib.parse import parse_qs, urlsplit
    try:
        u = urlsplit(str(url).strip())
    except ValueError:
        return str(url)
    host = (u.hostname or "").lower()
    for pre in ("www.", "m.", "mobile.", "web."):
        if host.startswith(pre):
            host = host[len(pre):]
    host = {"threads.net": "threads.com", "twitter.com": "x.com", "youtu.be": "youtube.com"}.get(host, host)
    path = u.path.rstrip("/")
    keep = ""
    if host == "youtube.com":
        v = parse_qs(u.query).get("v", [""])[0] or (path.strip("/") if u.hostname and "youtu.be" in u.hostname else "")
        if v:
            path, keep = "/watch", "v=" + v
    return f"{host}{path}" + (f"?{keep}" if keep else "")


def norm_author(name):
    return re.sub(r"[^\w]", "", str(name or "").lower().lstrip("@"))


def icon(site):
    s = str(site or "").lower()
    for key, ic in ICONS:
        if s == key or s.startswith(key):
            return ic
    return "🌐"


def ts_text(ts):
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        return _dt.datetime.fromtimestamp(int(ts), ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
    except Exception:  # noqa: BLE001
        return ""


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

    @staticmethod
    def _fix(e):
        e["topics"] = [OLD.get(t, t) for t in e.get("topics", [])]
        e["geos"] = [OLD.get(g, g) for g in e.get("geos", [])]
        e["first"] = [f.split(":", 1)[0] + ":" + OLD.get(f.split(":", 1)[-1], f.split(":", 1)[-1]) for f in e.get("first", [])]
        e.setdefault("langs", [])
        return e

    def items(self, chat):
        return [self._fix(e) for e in self._load().get(str(chat), [])]

    def items_all(self):
        return [self._fix(e) for c, lst in self._load().items() if c != "_vocab" for e in lst]

    def update(self, chat, rid, **fields):
        """✏️ Ручна зміна категорій уже скачаного (теми, локації, мови)."""
        with self.lock:
            data = self._load()
            for e in data.get(str(chat), []):
                if e.get("id") == rid:
                    e.update(fields)
                    e.pop("notag", None)
                    self._save(data)
                    return self._fix(dict(e))
        return None

    def by_url(self, chat, *urls):
        """Чи це вже скачували (за будь-яким із посилань: як надіслали чи справжнє після переадресації)."""
        keys = {norm_url(u) for u in urls if u}
        for e in reversed(self.items(chat)):
            if keys & {norm_url(e.get("url", "")), norm_url(e.get("src_url", ""))}:
                return e
        return None

    def by_msg(self, chat, msg_id):
        return next((e for e in self.items(chat) if e.get("msg") == msg_id), None)

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

    # --- словник тем, локацій і мов (сідовий + вивчений від ШІ)
    def vocab(self):
        v = self._load().get("_vocab", {})
        return {k: {**SEED[k], **v.get(k, {})} for k in KINDS}

    def learn(self, kind, pairs):
        """Нові назви запам'ятовуються з фразою «попереднє …». -> ключі."""
        keys = []
        with self.lock:
            data = self._load()
            voc = data.setdefault("_vocab", {}).setdefault(kind, {})
            changed = False
            for name, phrase in pairs:
                k = key_of(name)
                keys.append(k)
                if k not in SEED[kind] and k not in voc:
                    voc[k] = {"name": name, "phrase": phrase}
                    changed = True
            if changed:
                self._save(data)
        return keys

    def learn_user(self, kind, name, phrase):
        """✍️ Тег, який поставив сам користувач: зберігається й надалі шукається в нових дописах."""
        k = key_of(name)
        with self.lock:
            data = self._load()
            voc = data.setdefault("_vocab", {}).setdefault(kind, {})
            voc[k] = {"name": name, "phrase": phrase, "user": True}
            self._save(data)
        return k

    def user_tags(self):
        v = self._load().get("_vocab", {})
        return [(kind, k, d) for kind in ("t", "g") for k, d in v.get(kind, {}).items() if d.get("user")]

    def drop_user(self, kind, k):
        with self.lock:
            data = self._load()
            d = data.get("_vocab", {}).get(kind, {}).get(k)
            if d:
                d.pop("user", None)
                self._save(data)

    def match_user(self, text):
        """Збережені теги користувача, згадані в тексті допису -> {"t": [ключі], "g": [ключі]}."""
        out = {"t": [], "g": []}
        for kind, k, d in self.user_tags():
            rx = stem_rx(d.get("name") or k)
            if rx and rx.search(text or ""):
                out[kind].append(k)
        return out

    def name(self, kind, k):
        return self.vocab()[kind].get(k, {}).get("name") or k

    def label(self, kind, k):
        return self.vocab()[kind].get(k, {}).get("phrase") or ("про " + k if kind == "t" else "· " + k)

    def known(self, kind, limit=150):
        """Найуживаніші назви першими — ШІ повторює їх дослівно, щоб групи не дробились."""
        cnt = {}
        for e in self.items_all():
            for k in e.get(KINDS[kind], []):
                cnt[k] = cnt.get(k, 0) + 1
        voc = self.vocab()[kind]
        return [voc[k]["name"] for k in sorted(voc, key=lambda k: (not voc[k].get("user"), -cnt.get(k, 0)))[:limit]]

    def first_text(self, first):
        return ", ".join(("тема «" if kind == "t" else "локація «") + self.name(kind, k) + "»" for kind, k in first)

    # --- пошук пов'язаного
    def related(self, chat, author, topics, geos, langs=()):
        """Останнє скачане: того ж автора (окремо для кожного сайту), з тією ж локацією, на ту ж тему, тією ж мовою.
        Без теми / без локації — якщо в цьому дописі їх не знайшлось. «Вперше» — лише теми й локації (не автори).
        Порожніх груп не буває: повертаємо лише те, що справді вже скачувалось."""
        lst = self.items(chat)
        a = norm_author(author)
        by_site, by_geo, by_topic, by_lang, extra = {}, {}, {}, {}, {}
        seen_t, seen_g = set(), set()
        for e in reversed(lst):  # найновіші спершу
            if e.get("notag"):  # «без тегів» — лише в своїй групі
                continue
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
            for lg in e.get("langs", []):
                if lg in langs:
                    by_lang.setdefault(lg, e)
            if not topics and not e.get("topics"):
                extra.setdefault("notopic", e)
            if not geos and not e.get("geos"):
                extra.setdefault("nogeo", e)
            if e.get("first"):
                extra.setdefault("first", e)
        first = [("t", t) for t in topics if t not in seen_t] + [("g", g) for g in geos if g not in seen_g]
        if not first:
            extra.pop("first", None)  # «попереднє вперше» — лише під новим «вперше»
        return {"site": by_site, "g": by_geo, "t": by_topic, "l": by_lang, "x": extra, "first": first}

    def groups(self, chat):
        """Для архіву: автори, теми, локації, мови, без теми, без локації, вперше."""
        out = {"a": {}, "t": {}, "g": {}, "l": {}, "x": {}}
        for e in self.items(chat):
            if e.get("notag"):
                continue
            if e.get("author"):
                out["a"].setdefault(e["author"], []).append(e)
            for kind in ("t", "g", "l"):
                for k in e.get(KINDS[kind], []):
                    out[kind].setdefault(k, []).append(e)
            if not e.get("topics"):
                out["x"].setdefault("notopic", []).append(e)
            if not e.get("geos"):
                out["x"].setdefault("nogeo", []).append(e)
            if e.get("first"):
                out["x"].setdefault("first", []).append(e)
        return out

    def search(self, chat, query):
        """🔎 «снігопад з Карпат від oleno4ka» -> (що зрозуміли, знайдені записи). Тема, локація, автор, мова — усі разом (І);
        решта слів — пошук у назві/тексті."""
        q = (query or "").strip()
        voc = self.vocab()
        want = {"t": set(), "g": set(), "l": set()}
        rest = q
        for kind in ("t", "g", "l"):
            for k, d in voc[kind].items():
                rx = stem_rx(d.get("name") or k)
                m = rx.search(rest) if rx else None
                if m:
                    want[kind].add(k)
                    rest = rest[:m.start()] + " " + rest[m.end():]
        for name, _p in tags(q)[0]:
            want["t"].add(key_of(name))
        for name, _p in tags(q)[1]:
            want["g"].add(key_of(name))
        items = [e for e in self.items(chat) if not e.get("notag") or e.get("group")]
        authors = {e.get("author"): e.get("who") or e.get("author") for e in items if e.get("author")}
        who = set()
        for a, w in authors.items():
            if len(a) >= 3 and (a in norm_author(q) or norm_author(w) in norm_author(q)):
                who.add(a)
                rest = re.sub(re.escape(str(w)), " ", rest, flags=re.I)
        words = [w for w in re.findall(r"[\w'’]{3,}", rest.lower())
                 if w not in ("знайди", "пошукай", "покажи", "скачане", "скачані", "завантажене", "завантажені", "відео", "дописи",
                              "допис", "фото", "про", "від", "автора", "автор", "мені", "всі", "усі", "тему", "темою", "яке", "які", "with")]
        res = []
        for e in items:
            if want["t"] and not want["t"] & set(e.get("topics", [])):
                continue
            if want["g"] and not want["g"] & set(e.get("geos", [])):
                continue
            if want["l"] and not want["l"] & set(e.get("langs", [])):
                continue
            if who and e.get("author") not in who:
                continue
            if words and not (want["t"] or want["g"] or want["l"] or who):
                hay = f"{e.get('title', '')} {e.get('who', '')} {e.get('geotag', '')}".lower()
                if not any(w[:max(4, len(w) - 2)] in hay for w in words):
                    continue
            res.append(e)
        understood = ([("🏷 " + self.name("t", k)) for k in want["t"]] + [("📍 " + self.name("g", k)) for k in want["g"]]
                      + [("🗣 " + self.name("l", k)) for k in want["l"]] + [("👤 " + authors[a]) for a in who])
        if not understood and words:
            understood = ["🔤 " + " ".join(words)]
        return understood, list(reversed(res))

