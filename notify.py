"""🔔 Режими сповіщень: кожна людина сама вирішує, як приходить кожен тип повідомлень, які бот надсилає сам.

🔔 гучно — звичайне сповіщення (звук і вібрація за налаштуваннями телефона);
🔕 тихо — без звуку й без вібрації (Telegram `disable_notification`), але повідомлення з'являється в чаті;
✖️ вимкнено — для тих типів, без яких бот працює далі (ранкові теми, резервні копії…).

Відповіді на власні дії людини не чіпаємо — вона їх чекає. Telegram не має режиму «лише вібрація»
і не дає боту позначати повідомлення прочитаними, тому тихі повідомлення все одно додають лічильник непрочитаних.
"""
from __future__ import annotations

import contextlib
import contextvars

KIND = contextvars.ContextVar("notify_kind", default="other")

# ключ → (назва, можна вимкнути, типовий режим)
KINDS = {
    "tasks": ("🧠 Відповіді, що готувались у фоні", False, "loud"),
    "media": ("📥 Завантаження", False, "loud"),
    "morning": ("🌅 Ранкові теми дня й погода", True, "loud"),
    "warn": ("📅 Попередження про важливі дні", True, "loud"),
    "news": ("📰 Ньюзрум: новини, чернетки, звіти", False, "loud"),
    "backup": ("🛟 Резервні копії", True, "silent"),
    "system": ("⚙️ Службові (збої, заявки, ключі)", False, "loud"),
    "other": ("🔔 Усе інше", False, "loud"),
}
MODES = {"loud": "🔔", "silent": "🔕", "off": "✖️"}
MODE_NAMES = {"loud": "гучно", "silent": "тихо", "off": "вимкнено"}

_get = None   # (chat_id) -> dict режимів
_set = None   # (chat_id, dict) -> None


def setup(getter, setter):
    global _get, _set
    _get, _set = getter, setter


def modes(chat_id):
    saved = {}
    if _get:
        try:
            saved = _get(chat_id) or {}
        except Exception:  # noqa: BLE001
            saved = {}
    out = {}
    for k, (_n, can_off, default) in KINDS.items():
        m = saved.get(k, default)
        if m not in MODES or (m == "off" and not can_off):
            m = default
        out[k] = m
    return out


def mode(chat_id, kind=None):
    kind = kind or KIND.get()
    return modes(chat_id).get(kind if kind in KINDS else "other", "loud")


def wanted(chat_id, kind):
    """Чи взагалі надсилати цій людині повідомлення цього типу."""
    return mode(chat_id, kind) != "off"


def cycle(chat_id, kind):
    """🔔 → 🔕 → ✖️ (якщо можна) → 🔔."""
    if kind not in KINDS:
        return
    order = ["loud", "silent"] + (["off"] if KINDS[kind][1] else [])
    cur = mode(chat_id, kind)
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
    saved = dict((_get(chat_id) if _get else None) or {})
    saved[kind] = nxt
    if _set:
        _set(chat_id, saved)
    return nxt


def set_all(chat_id, value):
    saved = {}
    for k, (_n, can_off, default) in KINDS.items():
        saved[k] = value if (value != "off" or can_off) else "silent"
    if _set:
        _set(chat_id, saved)


def apply(chat_id, kwargs, proactive):
    """Додає disable_notification до самостійного повідомлення, якщо людина обрала тихий режим."""
    if not proactive or kwargs.get("disable_notification"):
        return kwargs
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return kwargs
    if cid > 0 and mode(cid) in ("silent", "off"):
        kwargs["disable_notification"] = True
    return kwargs


@contextlib.contextmanager
def kind(name):
    tok = KIND.set(name)
    try:
        yield
    finally:
        KIND.reset(tok)


def screen(chat_id):
    m = modes(chat_id)
    lines = ["🔔 <b>Сповіщення</b>", "",
             "Як приходять повідомлення, які бот надсилає сам. Торкніться рядка, щоб змінити: "
             "🔔 гучно → 🔕 тихо (без звуку й вібрації) → ✖️ вимкнено (де можна).", ""]
    rows = []
    for k, (name, _c, _d) in KINDS.items():
        lines.append(f"{MODES[m[k]]} {name} — {MODE_NAMES[m[k]]}")
        rows.append([(f"{MODES[m[k]]} {name}", f"m:ntf:c:{k}")])
    lines += ["", "ℹ️ Режиму «лише вібрація» Telegram не має, а тихі повідомлення все одно позначаються непрочитаними — "
              "це обмеження Telegram, бот на нього не впливає."]
    rows.append([("🔔 Усе гучно", "m:ntf:a:loud"), ("🔕 Усе тихо", "m:ntf:a:silent")])
    return "\n".join(lines), rows
