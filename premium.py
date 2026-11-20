"""⭐ Telegram Premium: кожен сам позначає, чи є він у нього (і до якої дати), а бот під це підлаштовує пропозиції.

Бот не може дізнатися про Premium людини сам (Bot API цього не показує), тому:
- у налаштуваннях людина обирає «є / немає / є до дати»;
- бот за розкладом, який людина ставить сама (день, час, як часто), питає, чи щось змінилось.

Premium нічого не вимикає: усі функції лишаються, змінюються лише підказки й порядок пропозицій
(без Premium немає сенсу першими пропонувати довгі підписи чи кастомні емодзі в тексті, але зробити це можна —
наприклад, щоб опублікувати з іншого акаунта, де Premium є).
"""
from __future__ import annotations

import datetime as dt

# Обмеження звичайного акаунта й акаунта з Premium (за даними Telegram; Telegram інколи їх змінює).
LIMITS = [
    ("✍️ Підпис під фото/відео", "1024 символи", "4096 символів"),
    ("💬 Звичайне повідомлення", "4096 символів", "4096 символів"),
    ("😀 Кастомні емодзі в тексті", "лише переглядати", "вставляти будь-де"),
    ("📎 Файл за раз", "до 2 ГБ", "до 4 ГБ"),
    ("📁 Теки чатів", "10 (по 100 чатів)", "20 (по 200 чатів)"),
    ("📌 Закріплені чати", "5", "10"),
    ("📣 Канали й групи", "до 500", "до 1000"),
    ("🔗 Публічні посилання", "10", "20"),
    ("👤 Біо профілю", "70 символів", "140 символів"),
    ("⭐ Обрані стікери · GIF", "5 · 200", "10 · 400"),
    ("🎙 Розшифровка голосових у Telegram", "обмежено", "без обмежень"),
]
CAPTION_FREE, CAPTION_PREMIUM = 1024, 4096

EVERY = {"off": "не питати", "w": "щотижня", "2w": "раз на 2 тижні", "m": "щомісяця"}
DOW = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
DEFAULT_ASK = {"every": "m", "dow": 0, "dom": 1, "time": "10:00"}

_get = lambda chat: {}  # noqa: E731
_set = lambda chat, value: None  # noqa: E731


def setup(getter, setter):
    global _get, _set
    _get, _set = getter, setter


def state(chat) -> dict:
    s = dict(_get(chat) or {})
    s["ask"] = {**DEFAULT_ASK, **(s.get("ask") or {})}
    return s


def save(chat, s):
    _set(chat, s)


def today(tz=None):
    return (dt.datetime.now(tz) if tz else dt.datetime.now()).date()


def has(chat, tz=None):
    """True / False / None (людина ще не казала). «Є до дати» після цієї дати — уже False."""
    s = state(chat)
    if s.get("has") is None:
        return None
    if s.get("has") and s.get("until"):
        try:
            return dt.date.fromisoformat(s["until"]) >= today(tz)
        except ValueError:
            return True
    return bool(s.get("has"))


def caption_limit(chat, tz=None):
    return CAPTION_PREMIUM if has(chat, tz) else CAPTION_FREE


def set_has(chat, value, until=None):
    s = state(chat)
    s["has"], s["until"] = value, (until if value else None)
    s["answered"] = today().isoformat()
    save(chat, s)


def set_ask(chat, **kw):
    s = state(chat)
    s["ask"].update(kw)
    save(chat, s)


def status_line(chat, tz=None):
    h = has(chat, tz)
    s = state(chat)
    if h is None:
        return "не вказано"
    if h and s.get("until"):
        d = dt.date.fromisoformat(s["until"])
        return f"є, до {d.strftime('%d.%m.%Y')}"
    if not h and s.get("until"):
        return "немає (термін минув)"
    return "є" if h else "немає"


def ask_line(chat):
    a = state(chat)["ask"]
    if a["every"] == "off":
        return "не питати"
    day = f"щопонеділка" if a["every"] == "w" and a["dow"] == 0 else (
        f"у {DOW[a['dow']]}" if a["every"] in ("w", "2w") else f"{a['dom']}-го числа")
    return f"{EVERY[a['every']]}, {day}, о {a['time']}"


def due(chat, now: dt.datetime) -> bool:
    """Чи пора зараз спитати людину (до 15 хв запізнення; не частіше, ніж обрано)."""
    s = state(chat)
    a = s["ask"]
    if a["every"] == "off":
        return False
    try:
        hh, mm = (int(x) for x in a["time"].split(":"))
    except ValueError:
        hh, mm = 10, 0
    late = (now.hour * 60 + now.minute) - (hh * 60 + mm)
    if not 0 <= late <= 15:
        return False
    d = now.date()
    last = s.get("asked")
    if last == d.isoformat():
        return False
    if a["every"] == "m":
        return d.day == min(int(a["dom"]), 28)
    if d.weekday() != int(a["dow"]):
        return False
    if a["every"] == "2w" and last:
        try:
            return (d - dt.date.fromisoformat(last)).days >= 13
        except ValueError:
            return True
    return True


def mark_asked(chat, now: dt.datetime):
    s = state(chat)
    s["asked"] = now.date().isoformat()
    save(chat, s)


def parse_date(text, tz=None):
    """«31.12», «31.12.2026», «2026-12-31», «через 30 днів», «на місяць» → date або None."""
    t = (text or "").strip().lower()
    base = today(tz)
    import re
    m = re.search(r"(\d+)\s*(дн|день|дні|тиж|міс)", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return base + dt.timedelta(days=n * (7 if unit.startswith("тиж") else 30 if unit.startswith("міс") else 1))
    if "місяц" in t:
        return base + dt.timedelta(days=30)
    if "рік" in t or "року" in t:
        return base + dt.timedelta(days=365)
    year_given = True
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$", t)
        if not m:
            return None
        d, mo = int(m.group(1)), int(m.group(2))
        year_given = bool(m.group(3))
        y = int(m.group(3)) if year_given else base.year
        if y < 100:
            y += 2000
    try:
        r = dt.date(y, mo, d)
        if not year_given and r < base:
            r = dt.date(y + 1, mo, d)
    except ValueError:
        return None
    return r


def screen(chat, tz=None):
    h = has(chat, tz)
    s = state(chat)
    a = s["ask"]
    lines = ["⭐ <b>Telegram Premium</b>",
             f"У вас: <b>{status_line(chat, tz)}</b>",
             f"🔁 Перепитувати: {ask_line(chat)}",
             "",
             "<blockquote expandable>ℹ️ Навіщо це\n"
             "Бот не бачить, чи є у вас Premium, — тому позначте самі. Від цього залежать лише підказки: "
             "без Premium бот не пропонуватиме першими довгі підписи під фото й кастомні емодзі в тексті "
             "(вони все одно доступні — наприклад, щоб опублікувати з іншого акаунта, де Premium є). "
             "Набори емодзі можна переглядати, збирати й пересилати й без Premium.\n"
             "Premium то є, то немає? Оберіть «⏳ Є до дати» — після неї бот сам вважатиме, що Premium немає, "
             "а за розкладом нижче — перепитає.</blockquote>",
             "",
             "<b>Різниця в обмеженнях</b> (без Premium → з Premium):"]
    lines += [f"{n}: {f} → <b>{p}</b>" for n, f, p in LIMITS]
    rows = [[(("● " if h is True and not s.get("until") else "") + "⭐ Є", "m:prem:y"),
             (("● " if h is False else "") + "▫️ Немає", "m:prem:n"),
             (("● " if h and s.get("until") else "") + "⏳ Є до дати", "m:prem:u")],
            ]
    freq = [(("● " if a["every"] == k else "") + ("🔁 " if k != "off" else "✖️ ") + v.capitalize(), f"m:prem:e:{k}") for k, v in EVERY.items()]
    rows += [freq[:2], freq[2:]]
    if a["every"] in ("w", "2w"):
        rows.append([(("● " if int(a["dow"]) == i else "") + d, f"m:prem:d:{i}") for i, d in enumerate(DOW)])
    elif a["every"] == "m":
        dom = [(("● " if int(a["dom"]) == n else "") + f"{n} числа", f"m:prem:md:{n}") for n in (1, 5, 10, 15, 20, 25)]
        rows += [dom[:3], dom[3:]]
    if a["every"] != "off":
        rows.append([("🕘 Час: " + a["time"], "m:prem:t")])
    return "\n".join(lines), rows


def question(chat, tz=None):
    return (f"⭐ <b>Чи є зараз у вас Telegram Premium?</b>\nЗараз у налаштуваннях: {status_line(chat, tz)}.\n"
            "Від відповіді залежать лише підказки бота (довгі підписи, кастомні емодзі в тексті).",
            [[("⭐ Є", "m:prem:y:q"), ("▫️ Немає", "m:prem:n:q"), ("⏳ Є до дати", "m:prem:u")],
             [("⚙️ Як часто питати", "m:prem")]])
