"""🧪 Пісочниця OrionAI: навчання й перевірка функцій на знімку справжніх екранів бота.

- Один знімок (simulations/sandbox_screens.json, збирає sandbox_build.py) для чату й Mini App.
- У чаті: кнопки з даними «sbx:…» показують екрани зі знімка; дії лише описуються. Жодних змін у базах бота.
- Статистика — лише цифри (відкриття, натискання, розділи, сценарії) без ID, імен і текстів.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "simulations" / "sandbox_screens.json"
PAGE = ROOT / "simulations" / "sandbox.html"
_lock = threading.Lock()

ACTIONS = {
    "ok": "✅ Схвалено. Якщо ввімкнено «Одразу» — бот одразу опублікує в канал.", "no": "❌ Чернетку відхилено.",
    "re": "↩️ Повернуто в чернетки.", "rw": "🔁 Бот перепише новину й надішле нову версію; стара — у «🕘 Версії».",
    "chk": "🔎 Бот шукає в інтернеті й додає звіт: ✅ підтверджено / ⚠️ не знайдено / ❌ суперечить.",
    "pub": "📤 Опубліковано в канал (лише схвалене).", "txt": "📋 Бот надсилає чистий текст окремим повідомленням.",
    "lenset": "📏 Бот підганяє текст до точної кількості символів і перевіряє сам.", "dgo": "🧾 Бот складає зведення за добу.",
    "intake": "Режим прийому звичайного тексту змінено.", "Lnow": "🔄 Бот перевіряє джерела зараз.", "Enow": "🔄 Перевіряю це джерело.",
    "Lexp": "📥 Бот надсилає CSV для Excel і TXT — їх можна відредагувати й завантажити назад.",
    "Lsync": "🔄 Підписки акаунта синхронізовано.", "Lmtre": "🔌 Підключаюсь до службового акаунта…",
    "Etog": "🟢/⚪️ Джерело увімкнено / вимкнено.", "Ltog": "Список увімкнено / вимкнено.", "Emv": "↕️ Джерело переміщено.",
    "Lsortv": "🔃 Список відсортовано.", "Lall": "Усі джерела увімкнено / вимкнено.", "Lclean": "🧹 Прибрано.",
    "Ldel": "🗑 Список видалено.", "Edel": "🗑 Джерело видалено.", "Lmerget": "🔗 Списки об'єднано.", "Elstt": "📁 Джерело перенесено.",
    "Liv": "⏱ Інтервал змінено.", "Lmove": "↕️ Список переміщено.", "Emeth": "🔁 Спосіб читання: авто → вебпревʼю → акаунт → бот.",
    "Lnewon": "Нові підписки: увімкнені / вимкнені.", "tog": "Плагін увімкнено / вимкнено.", "sox": "Джерело увімкнено.",
    "go": "📦 Завантажую пакет: прогрес в одному повідомленні, потім ZIP і звіт.", "inf": "🔎 Назва, тип і кількість стікерів.",
    "trk1": "👁 Стежу за пакетом.", "mode": "📦 Один архів ↔ 🗂 окремий на кожен пакет.", "png": "🖼 PNG-копії: так / ні.",
    "again": "🔁 Повторюю завантаження.", "one": "📦 Завантажую пакет.", "untrk": "Більше не стежу.",
    "Dgo": "🔎 Бот шукає свіже, оцінює, групує схоже, звіряє з нашими й одразу пише. Список знайденого й чернетка прийдуть окремо.",
    "Dfind": "🔎 Бот лише шукає й надсилає список знайденого.", "Fw": "✍️ Бот пише з цієї знахідки й схожих матеріалів її історії.",
    "Fbig": "📚 Бот пише одну велику публікацію з верхніх знахідок.", "Fchk": "✅ Бот перевіряє факти пошуком і додає звіт.",
    "Fref": "❌ Бот готує спростування — лише якщо знайде підтверджені докази.", "Fmvt": "📁 Знахідку переміщено.",
    "Fdel": "🗑 Знахідку видалено.", "Fblv": "🚫 Заблоковано.", "Tgt": "Тег увімкнено / вимкнено.", "Tgiv": "⏱ Інтервал тегу змінено.",
    "Tgs": "Сортування дайджесту змінено.", "Tgrun": "🔄 Шукаю за тегом зараз.", "Tgdel": "🗑 Тег видалено.", "Tpdel": "🗑 Тему прибрано.",
    "Bkd": "Розблоковано.", "Spl": "🧹 Рівень антиспаму змінено.", "Spr": "↩️ Повернуто в роботу — це не спам.",
    "Ourt": "🏠 Позначку «наш ресурс» змінено.", "Su": "✍️ Бот пише на основі всієї історії.", "Wtr": "Повернуто базовий шаблон.",
    "Dwb": "📄 Word UA+RU: так / ні.", "Hfwd": "↪️ Переслане → ньюзрум: так / ні.", "toai": "💬 Відповідаю як ШІ.",
    "tg": "Перемикач змінено.", "setlenv": "Збережено.", "autorss": "Автоперевірка RSS: так / ні.", "poll": "🔄 Перевіряю RSS…",
    "retry": "🔁 Повторюю обробку.", "force": "📝 Беру в роботу.", "vback": "↩️ Повернуто стару версію.", "pubchx": "Канал відключено.",
    "rulesx": "Прибрано.", "sodel": "Видалено.", "Ouc": "🏠 Створено список наших каналів.",
    "obw": "✍️ Бот шукає свіже на тему дня й пише чернетку.", "obz": "➕ Тему додано до зведення.", "obx": "🙈 Тему сховано.",
    "obu": "🔄 Бот завантажує теми з Вікіпедії на весь рік.", "obm": "⏰ Час ранкового сповіщення.", "oba": "➕ Своя тема.",
    "cgo": "🟢 Збір для зведення увімкнено / вимкнено.", "cmk": "🎭 Вибір сценарію перед зведенням.", "csg": "🧾 Бот пише зведення.",
    "cday": "➕ Вибір теми для новин за добу.", "cdt": "➕ Додано новини на тему.", "cclr": "🗑 Зібране очищено.", "cpop": "Прибрано останнє.",
    "act": "Виконується запрограмована кнопка.", "tobot": "🤖 Надіслано дочірньому боту.", "tofld": "📁 Покладено в теку.", "delok": "🗑 Видалено.",
    "rpin": "📌 Закріплено зверху бота.", "rgo": "📄 Бот надсилає повний звіт файлом Word.", "rpun": "🧹 Відкріплено.",
    "rpa": "🔔 Щомісячний звіт 1-го числа увімкнено / вимкнено.", "rpt": "✅ Позначку змінено.", "rpau": "🔁 Автооновлення закріпленого змінено.",
}


# ==================================================================== статистика (лише цифри)
def _stats_path():
    base = Path(os.path.dirname(os.getenv("DB_PATH", "data/bot.db")) or ".")
    base.mkdir(parents=True, exist_ok=True)
    return base / "sandbox_stats.json"


def stats():
    try:
        return json.loads(_stats_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {"opens": 0, "clicks": 0, "where": {}, "sections": {}, "scenes": {}, "days": {}}


def count(event: str, where: str = "chat"):
    """event: open | click:<розділ> | scene:<назва>. Інше ігнорується (захист від сміття)."""
    event = str(event)[:40]
    kind, _, name = event.partition(":")
    if kind not in ("open", "click", "scene") or (name and not name.replace("_", "").replace("-", "").isalnum()):
        return
    with _lock:
        s = stats()
        day = time.strftime("%Y-%m-%d")
        s["days"][day] = s["days"].get(day, 0) + 1
        s["days"] = dict(sorted(s["days"].items())[-60:])
        s["where"][where] = s["where"].get(where, 0) + 1
        if kind == "open":
            s["opens"] += 1
        elif kind == "click":
            s["clicks"] += 1
            s["sections"][name or "menu"] = s["sections"].get(name or "menu", 0) + 1
        else:
            s["scenes"][name] = s["scenes"].get(name, 0) + 1
        tmp = _stats_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False), "utf-8")
        os.replace(tmp, _stats_path())


def stats_text():
    s = stats()
    top = sorted(s["sections"].items(), key=lambda x: -x[1])[:5]
    names = {"news": "📰 Новинарня", "stk": "🏷 Наліпки", "menu": "меню", "info": "плагіни"}
    week = sum(v for k, v in s["days"].items() if k >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400)))
    return (f"Відкрито: {s['opens']} · натискань: {s['clicks']} · за 7 днів подій: {week}\n"
            f"Де: " + (", ".join(f"{'чат' if k == 'chat' else 'застосунок'} {v}" for k, v in s["where"].items()) or "—") + "\n"
            "Найчастіше: " + (", ".join(f"{names.get(k, k)} {v}" for k, v in top) or "—"))


# ==================================================================== знімок
_cache = {"mtime": 0, "data": None, "keys": []}


def snapshot():
    try:
        m = SNAPSHOT.stat().st_mtime
    except OSError:
        return None
    if _cache["mtime"] != m:
        data = json.loads(SNAPSHOT.read_text("utf-8"))
        _cache.update(mtime=m, data=data, keys=list(data["screens"]))
    return _cache["data"]


def key_id(data_key):
    snap = snapshot()
    return _cache["keys"].index(data_key) if snap and data_key in snap["screens"] else None


def action_of(d):
    p = d.split(":")
    if len(p) > 2 and p[1] == "pl":
        return p[3] if len(p) > 3 else p[2]
    return ""


def screen(data_key):
    """-> (текст, рядки з даними sbx:…) або None."""
    snap = snapshot()
    if not snap or data_key not in snap["screens"]:
        return None
    s = snap["screens"][data_key]
    rows = []
    for row in s["rows"]:
        r = []
        for t, d in row:
            if not d or d.startswith("m:nav") or d == "imp:s":
                continue
            if d.startswith("url:"):
                r.append((t, d))
                continue
            i = key_id(d)
            r.append((t, f"sbx:s:{i}" if i is not None else f"sbx:a:{action_of(d)[:20] or 'x'}:{(d.split(':')[2] if len(d.split(':')) > 2 else 'menu')[:10]}"))
        if r:
            rows.append(r)
    rows.append([("🏠 Додому", f"sbx:s:{key_id('m:home')}"), ("🚪 Вийти з пісочниці", "sbx:x"), ("💡", "imp:s")])
    return "🧪 <i>Пісочниця · демо-дані</i>\n\n" + s["text"], rows


def press(data):
    """sbx:… -> ('screen', (text, rows)) | ('toast', текст) | ('exit', None)"""
    parts = data.split(":")
    if parts[1] == "x":
        return "exit", None
    if parts[1] == "s":
        key = _cache["keys"][int(parts[2])] if snapshot() and parts[2].isdigit() and int(parts[2]) < len(_cache["keys"]) else "m:home"
        count("click:" + (key.split(":")[2] if len(key.split(":")) > 2 else "menu"))
        return "screen", screen(key)
    act = parts[2] if len(parts) > 2 else ""
    count("click:" + (parts[3] if len(parts) > 3 else "menu"))
    if act in ACTIONS:
        return "toast", "🧪 " + ACTIONS[act]
    if parts[3:4] and parts[3] not in ("news", "stk", "menu", "info"):
        return "toast", "🧪 Цей розділ у пісочниці не показано."
    return "toast", "🧪 У справжньому боті тут вводиться текст або виконується дія. У пісочниці — лише показ."


def page_html():
    try:
        return PAGE.read_text("utf-8")
    except OSError:
        return None
