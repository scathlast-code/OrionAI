"""🧭 Тема тексту без ШІ: війна, погода, енергетика, екологія… — щоб розкладати репости по окремих зведеннях
і підказувати, що можна зробити з тим, що людина просто кинула в бот (посилання, текст, фото).

Нічого не виконує сам — лише визначає.
"""
from __future__ import annotations

import re

TOPICS = {  # ключ: (назва, корені слів)
    "war": ("⚔️ Війна", r"обстріл\w*|вибух\w*|прил[іе]т\w*|ракет\w*|дрон\w*|шахед\w*|бпла|атак\w*|поранен\w*|загибл\w*|зсу|фронт\w*|"
                       r"війн\w*|окупант\w*|ппо|артилер\w*|каб\w{0,3}|евакуац\w*|повітрян\w* тривог\w*|тривог\w*|руйнуван\w*|уламк\w*"),
    "weather": ("🌦 Погода", r"погод\w*|синоптик\w*|опад\w*|дощ\w*|сніг\w*|снігопад\w*|мороз\w*|спек\w*|вітр\w*|вітер|шторм\w*|"
                            r"ожеледиц\w*|температур\w*|градус\w*|туман\w*|гроз\w*|хуртовин\w*|злив\w*|паводк\w*|підтоплен\w*"),
    "energy": ("⚡️ Енергетика", r"світл[оау]|без світла|електроенерг\w*|електропостачан\w*|знеструм\w*|відключен\w*|графік\w* відключ\w*|"
                               r"укренерго|енергосистем\w*|енергетик\w*|дтек|блекаут\w*|тес|тец|гес|аес|газ(?:у|ом|опостачан\w*|ов\w*)?|"
                               r"опален\w*|генератор\w*|енергооб'?єкт\w*"),
    "eco": ("🌿 Екологія", r"еколог\w*|довкілл\w*|забрудн\w*|сміт\w*|сміття|звалищ\w*|вируб\w*|ліс(?:у|ів|и|ом)?|викид\w*|клімат\w*|"
                          r"держекоінспекц\w*|заповідн\w*|браконьєр\w*|нафтопродукт\w*"),
    "agro": ("🌾 Агро", r"аграр\w*|врожа\w*|урожа\w*|посівн\w*|жнива|фермер\w*|зерн\w*|пшениц\w*|кукурудз\w*|соняшник\w*|тваринн\w*|апк"),
    "health": ("🩺 Здоровʼя", r"моз|лікар\w*|медицин\w*|медик\w*|хвороб\w*|захворюван\w*|епідем\w*|вакцин\w*|ковід\w*|covid|грип\w*|"
                             r"пацієнт\w*|госпітал\w*|отруєн\w*|інфекц\w*"),
    "animals": ("🐾 Тварини", r"тварин\w*|зоозахис\w*|безпритул\w*|притулк\w*|притулок|собак\w*|котів|кішк\w*|ветеринар\w*"),
    "space": ("🔭 Космос", r"космос\w*|космічн\w*|астроном\w*|nasa|esa|телескоп\w*|астероїд\w*|комет\w*|затемнен\w*|магнітн\w* бур\w*"),
}
_RE = {k: re.compile(r"(?<![\w'ʼ])(?:" + v[1] + r")(?![\w])", re.I) for k, v in TOPICS.items()}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
TASK_RE = re.compile(r"(?i)\b(треба|потрібно|зроби(?:ти)?|написати|підготувати|нагадай|не забути|до (?:понеділка|вівторка|середи|четверга|"
                     r"п'?ятниці|пʼятниці|суботи|неділі|завтра|кінця)|дедлайн|завдання)\b")


def label(key):
    return TOPICS.get(key, ("📄 Інше",))[0]


def scores(text):
    t = (text or "").replace("’", "'").replace("ʼ", "'")
    return {k: len(r.findall(t)) for k, r in _RE.items() if r.search(t)}


def classify(text):
    """Теми за спаданням: [ключ]. Порожньо — тема не визначена."""
    s = scores(text)
    return sorted(s, key=lambda k: -s[k])


def main_and_mixed(text):
    """(головна тема або 'other', [інші теми, що теж суттєві]) — суттєва, якщо згадана хоча б удвічі рідше за головну."""
    s = scores(text)
    if not s:
        return "other", []
    order = sorted(s, key=lambda k: -s[k])
    top = order[0]
    return top, [k for k in order[1:] if s[k] * 2 >= s[top]]


def group(items):
    """items: [{id, text}] → ({тема: [items]}, {(a, b): [items]}) — змішані (напр. «нема світла через обстріли») окремо."""
    pure, mixed = {}, {}
    for it in items:
        top, extra = main_and_mixed(it["text"])
        if extra:
            mixed.setdefault(tuple(sorted([top, extra[0]])), []).append(it)
        else:
            pure.setdefault(top, []).append(it)
    return pure, mixed


def apply_choices(pure, mixed, choices):
    """choices: {(a, b): 'both'|'a'|'b'|'join'} → {назва-групи: [items]}; ключ групи — тема або 'a+b' (спільне зведення)."""
    out = {k: list(v) for k, v in pure.items()}
    for pair, its in mixed.items():
        c = choices.get(pair, "both")
        a, b = pair
        if c == "join":
            out.setdefault(f"{a}+{b}", []).extend(its)
            continue
        for k in ([a, b] if c == "both" else [a] if c == "a" else [b]):
            out.setdefault(k, []).extend(its)
    for v in out.values():
        v.sort(key=lambda i: i["id"])
    return out


def group_title(key):
    return " + ".join(label(k) for k in key.split("+"))


def find_topic(text):
    """«готуй зведення про погоду» → 'weather' (або None)."""
    m = re.search(r"(?i)\bпро\s+(.+)$", text or "")
    if not m:
        return None
    ks = classify(m.group(1))
    if ks:
        return ks[0]
    words = m.group(1).lower()
    for k, (name, _) in TOPICS.items():
        if name.split(" ", 1)[1].lower()[:5] in words:
            return k
    return None


def kind_of(text, photo=False):
    """Що це: link / task / photo / text."""
    if URL_RE.search(text or ""):
        return "link"
    if photo:
        return "photo"
    if TASK_RE.search(text or ""):
        return "task"
    return "text"


ASKS_RE = re.compile(r"(?i)\?|\b(?:що|про що|перекаж\w*|поясн\w*|розкаж\w*|підсумуй|коротко|переклад\w*|переклади|чи правда|як|чому|навіщо|скільки|хто|де|коли)\b")


def is_talk(text):
    """💬 Звичайна репліка в розмові (коротка або питання) — відповідає ШІ, а не автовизначення чи збір."""
    t = (text or "").strip()
    if URL_RE.search(t):
        return False
    return len(t) < 200 or (t.endswith("?") and len(t) < 600)
