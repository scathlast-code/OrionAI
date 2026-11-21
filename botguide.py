"""🧭 Самознання для ШІ: що вміє бот і мініапп, де що лежить, які налаштування можна змінити за проханням,
у якому стані моделі й чому вони не відповідають. Використовується інструментами bot_guide / bot_settings / bot_status.

Усе береться з самого коду (розділи, довідка «❔ Що це», підказки кнопок), тож нові функції потрапляють сюди самі.
"""
from __future__ import annotations

import re
import time

PROMPT = (
    "\nТи — ШІ всередині Telegram-бота OrionAI, у якого є й мініапп (той самий функціонал у вікні Telegram). "
    "Коли людина питає, що вміє бот, де щось знайти, як налаштувати, чому щось не спрацювало чи модель не відповіла — "
    "спершу виклич bot_guide (за темою) або bot_status, і відповідай за цими даними: назви розділ і шлях кнопками "
    "(напр. «🏠 Додому → 📰 Новинарня → 🔗 UTM-мітки»), коротко поясни й порадь краще налаштування. "
    "Коли людина просить змінити налаштування бота — виклич bot_settings (спершу action=list, потім set), "
    "а у відповіді скажи, що саме змінено і як повернути. Нічого не вигадуй про функції, яких немає в bot_guide."
)

# key -> (назва, тип, допустимі значення або межі, пояснення)
SETTABLE = {
    "length": ("Довжина відповідей ШІ", "choice", ("short", "normal", "long"), "short — 1–3 речення, normal — звичайно, long — розгорнуто"),
    "memory": ("Скільки повідомлень пам'ятає розмова", "int", (2, 200), "більше — краще пам'ятає контекст, але повільніше й дорожче для лімітів"),
    "temp": ("Креативність відповідей", "float", (0.0, 1.5), "0 — точно й сухо, 0.7 — звичайно, 1+ — вигадливо"),
    "footer": ("Підпис моделі під відповіддю", "bool", None, "показувати, яка модель відповіла"),
    "alerts": ("Сповіщення про проблеми моделей", "bool", None, "окремі повідомлення, коли модель недоступна чи вичерпала ліміт"),
    "task_start": ("Заплановані завдання", "choice", ("auto", "manual"), "auto — бот запускає сам, manual — нагадує й чекає натискання"),
    "shape": ("Формат картинок", "choice", ("square", "portrait", "landscape", "story"), "квадрат, вертикаль 4:5, горизонталь 16:9, сторіс 9:16"),
    "slide": ("Секунд на слайд у слайдшоу", "int", (2, 15), ""),
    "autocat": ("Автовизначення того, що надіслано", "bool", None, "бот лише пропонує дії для посилань, фото, текстів; сам нічого не робить"),
    "collect": ("Режим збору текстів", "bool", None, "пересилання й довгі тексти мовчки складаються для зведення; короткі репліки — розмова з ШІ"),
    "fwd_in_app": ("Репости видно в застосунку", "bool", None, "переслане показується в розмові мініапп (але не в пам'яті ШІ)"),
    "model": ("Яка модель відповідає", "model", None, "auto — бот сам обирає доступну; або конкретна модель"),
    "media_quality": ("Якість завантаження відео за замовчуванням", "choice",
                      ("ask", "360", "480", "720", "1080", "1440", "2160", "0", "a"),
                      "ask — питати щоразу; число — не вище цієї якості; 0 — найкраща, що є; a — лише звук"),
}


def _plain(html_text):
    return re.sub(r"<[^>]+>", "", str(html_text or "")).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def _explain(key):
    import explain
    e = explain.E.get(key)
    return e if isinstance(e, dict) else {}


def overview(B):
    """Усі розділи бота одним списком: назва, що в ньому, частини."""
    out = ["Розділи бота (кнопки в 🏠 Додому; у мініапп — ті самі вкладки й плитки):"]
    for key, (title, intro, parts) in getattr(B, "HUBS", {}).items():
        names = ", ".join(_plain(h) for h, _d, _n, _m in parts)
        out.append(f"• {title} — {intro} Частини: {names}.")
    return "\n".join(out)


def guide(B, topic=""):
    """Довідка за темою: розділи, частини, «Що це / Як / Навіщо», підказки кнопок. Без теми — огляд."""
    import button_hints
    t = (topic or "").strip().lower()
    if not t:
        return overview(B) + "\n\nУточни тему (напр. «UTM-мітки», «завантаження відео», «погода», «нотатки»), щоб отримати подробиці."
    words = [w for w in re.findall(r"[\w'’-]{3,}", t)]

    def hit(text):
        s = (text or "").lower()
        return sum(1 for w in words if w[:5] in s)
    found = []
    for key, (title, intro, parts) in getattr(B, "HUBS", {}).items():
        for head, data, _n, _m in parts:
            k = data.removeprefix("m:pl:").removeprefix("m:").split(":")[0]
            e = _explain(k)
            score = hit(" ".join([head, *(str(v) for v in e.values())])) * 2 + hit(title) * 0.5
            if score >= 1:
                info = [f"Розділ: {title} → {_plain(head)} (кнопка «{_plain(head)}»)"]
                for f, lab in (("what", "Що це"), ("how", "Як"), ("why", "Навіщо"), ("undo", "Як скасувати")):
                    if e.get(f):
                        info.append(f"{lab}: {_plain(e[f])}")
                found.append((score, "\n".join(info)))
    import explain
    for k, e in explain.E.items():  # решта довідки (налаштування, служби…)
        if isinstance(e, dict) and hit(" ".join(str(v) for v in e.values())) and not any(k in f for _s, f in found):
            found.append((hit(" ".join(str(v) for v in e.values())) - 0.5,
                          "\n".join(f"{lab}: {_plain(e[f])}" for f, lab in (("what", "Що це"), ("how", "Як"), ("why", "Навіщо")) if e.get(f))))
    hints = [f"Кнопка «…{d}»: {h}" for d, h in button_hints.H.items() if hit(h + " " + d)][:8]
    sets = [f"Налаштування «{v[0]}» (bot_settings key={k}): {v[3]}" for k, v in SETTABLE.items() if hit(v[0] + " " + v[3])]
    found.sort(key=lambda x: -x[0])
    parts = [f for _s, f in found[:6]] + (["Підказки кнопок:\n" + "\n".join(hints)] if hints else []) + sets
    if not parts:
        return "За цією темою нічого не знайдено. " + overview(B)
    return "\n\n".join(parts)[:6000]


def settings_list(B, chat):
    p = B.prefs(chat)
    out = []
    for k, (label, typ, vals, about) in SETTABLE.items():
        if k == "model":
            cur = B.get_choice(chat)
            allowed = "auto, " + ", ".join(getattr(B, "PROVIDERS", {}))
        elif k == "media_quality":
            med = B.PLUGINS.get("med")
            cur = med.pref(chat, "q", "ask") if med else "—"
            allowed = ", ".join(vals)
        else:
            cur = p.get(k)
            allowed = ", ".join(vals) if typ == "choice" else (f"{vals[0]}…{vals[1]}" if vals else "true/false")
        out.append(f"{k} = {cur} · {label} · можна: {allowed}" + (f" · {about}" if about else ""))
    return "Поточні налаштування цієї людини:\n" + "\n".join(out)


def settings_set(B, chat, key, value):
    if key not in SETTABLE:
        return f"Невідоме налаштування «{key}». Доступні: {', '.join(SETTABLE)}."
    label, typ, vals, _about = SETTABLE[key]
    v = str(value).strip().lower()
    try:
        if typ == "bool":
            new = v in ("1", "true", "так", "yes", "on", "увімкнути", "увімк")
        elif typ == "int":
            new = max(vals[0], min(vals[1], int(float(v))))
        elif typ == "float":
            new = max(vals[0], min(vals[1], float(v.replace(",", "."))))
        elif typ == "model":
            if v != "auto" and v not in getattr(B, "PROVIDERS", {}):
                return f"Такої моделі немає. Можна: auto, {', '.join(B.PROVIDERS)}."
            old = B.get_choice(chat)
            B.set_choice(chat, v)
            return f"Змінено «{label}»: {old} → {v}."
        else:
            if v not in vals:
                return f"Недопустиме значення для «{label}». Можна: {', '.join(vals)}."
            new = v
    except (TypeError, ValueError):
        return f"Не вдалося розібрати значення «{value}» для «{label}»."
    if key == "media_quality":
        med = B.PLUGINS.get("med")
        if not med:
            return "Модуль завантаження вимкнений."
        old = med.pref(chat, "q", "ask")
        med.set_pref(chat, "q", new)
        return f"Змінено «{label}»: {old} → {new}."
    old = B.prefs(chat).get(key)
    B.set_pref(chat, key, new)
    return f"Змінено «{label}»: {old} → {new}. Повернути можна так само або в меню налаштувань."


def status(B):
    """Стан кожної моделі: готова, на паузі (до коли й чому), останній збій і що з ним робити."""
    now, out = time.time(), []
    for pid, prov in getattr(B, "PROVIDERS", {}).items():
        st = B.get_state(pid) or {}
        until = B.paused_until(pid)
        line = f"{prov.get('title', pid)}: "
        if until:
            kind = st.get("cooldown_kind") or st.get("err_kind") or "paused"
            line += f"на паузі ще {int((until - now) // 60)} хв — {B.ERR.get(kind, ('', kind))[1].lower()}"
        elif st.get("err_ts") and (not st.get("last_ok") or st["err_ts"] > st["last_ok"]):
            kind = st.get("err_kind") or ""
            line += f"останній запит невдалий ({B.ERR.get(kind, ('', kind))[1].lower()}, {int((now - st['err_ts']) // 60)} хв тому)"
        else:
            line += "готова" + (f", востаннє відповіла {int((now - st['last_ok']) // 60)} хв тому" if st.get("last_ok") else "")
        fix = getattr(B, "FIX_SHORT", {}).get(st.get("cooldown_kind") or st.get("err_kind") or "")
        if fix and (until or st.get("err_ts")):
            line += f". Що робити: {fix}"
        out.append(line)
    tail = ("Причини збоїв загалом: " + "; ".join(f"{t} ({k})" for k, (_i, t) in getattr(B, "ERR", {}).items()) +
            ". Коли відповідає кілька моделей у «Авто», бот сам переходить до наступної; відкладене завдання виконає, щойно модель звільниться.")
    return ("Моделі ШІ:\n" + "\n".join(out) if out else "Жодної моделі не підключено (немає ключів у змінних сервера).") + "\n\n" + tail
