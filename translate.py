"""🌐 Переклад: кнопка під статтями й дописами та переклад тексту, який людина надіслала з проханням «переклади».

Першими — українська, російська, англійська; далі інші поширені мови або будь-яка, яку людина напише сама.
"""
from __future__ import annotations

import re

LANGS = [  # код → (кнопка, назва в промті)
    ("uk", "🇺🇦 Українська", "українську"), ("ru", "Російська", "російську"), ("en", "🇬🇧 Англійська", "англійську"),
    ("pl", "🇵🇱 Польська", "польську"), ("de", "🇩🇪 Німецька", "німецьку"), ("fr", "🇫🇷 Французька", "французьку"),
    ("es", "🇪🇸 Іспанська", "іспанську"), ("it", "🇮🇹 Італійська", "італійську"), ("cs", "🇨🇿 Чеська", "чеську"),
    ("ro", "🇷🇴 Румунська", "румунську"), ("tr", "🇹🇷 Турецька", "турецьку"), ("pt", "🇵🇹 Португальська", "португальську"),
    ("zh", "🇨🇳 Китайська", "китайську (спрощену)"), ("ja", "🇯🇵 Японська", "японську"), ("ar", "🇸🇦 Арабська", "арабську"),
    ("ka", "🇬🇪 Грузинська", "грузинську"), ("he", "🇮🇱 Іврит", "іврит"), ("myv", "Ерзянська", "ерзянську"),
]
NAME = {c: n for c, _b, n in LANGS}

TARGET_HINTS = [
    (r"(?i)англ|english|\ben\b", "en"), (r"(?i)україн|ukrain|\bua\b|\buk\b", "uk"), (r"(?i)рос|русск|russian|\bru\b", "ru"),
    (r"(?i)польськ|polish", "pl"), (r"(?i)німецьк|german|deutsch", "de"), (r"(?i)французьк|french", "fr"),
    (r"(?i)іспанськ|spanish", "es"), (r"(?i)італійськ|italian", "it"), (r"(?i)чеськ|czech", "cs"), (r"(?i)румунськ|romanian", "ro"),
    (r"(?i)турецьк|turkish", "tr"), (r"(?i)китайськ|chinese", "zh"), (r"(?i)японськ|japanese", "ja"), (r"(?i)арабськ|arabic", "ar"),
    (r"(?i)ерзян", "myv"),
]


def lang_rows(cb, per=3):
    """Кнопки мов: перші три — окремим рядом, далі по per в ряд."""
    btns = [(b, cb(c)) for c, b, _n in LANGS]
    return [btns[:3]] + [btns[i:i + per] for i in range(3, len(btns), per)]


def is_cyr_uk(text):
    t = text.lower()
    return bool(re.search(r"[іїєґ]", t))


START_RE = re.compile(r"(?i)^\s*(?:будь ласка,?\s*|please,?\s*)?(переклади|перекладіть|перекласти|переведи|translate)\b")


def parse_request(text, quoted=None):
    """«переклади англійською: …» / «translate to Polish:\n…» / відповідь «переклади» на повідомлення → (текст, мова) або None.
    Мова за замовчуванням: українське → англійська, інше → українська. Питання на кшталт «як перекласти…» сюди не потрапляють."""
    m = START_RE.match(text or "")
    if not m:
        return None
    rest = text[m.end():]
    mm = re.match(r"^([^:\n]{0,60})[:\n]", rest)
    if mm:
        instr, body = mm.group(1), rest[mm.end():]
    else:
        first = re.match(r"^\s*((?:на|to|into)\s+)?(\S+)", rest)
        hint = first and any(re.search(rx, first.group(2)) for rx, _c in TARGET_HINTS)
        instr, body = (first.group(0), rest[first.end():]) if hint else ("", rest)
    target = next((code for rx, code in TARGET_HINTS if re.search(rx, instr)), None)
    src = body.strip() or (quoted or "").strip()
    if len(src) < 2:
        return None
    if not target:
        target = "en" if is_cyr_uk(src) else "uk"
    return src, target


def prompt(target, custom=None):
    lang = custom or NAME.get(target, target)
    return (f"Переклади текст на {lang} мову. Лише переклад — без пояснень, коментарів і лапок. Збережи зміст, тон, абзаци, "
            "емодзі, посилання, імена й цифри. Якщо текст уже цією мовою — виправ лише помилки й поверни його.")
