"""🩺 Сторож здоров'я: бот сам попереджає власника, коли щось тихо перестало працювати.

• Сервіси (розшифровка мови, Shazam, ШІ із зором, контент-план, сайти-джерела, пісні): після 3 збоїв поспіль —
  повідомлення «що зламалось, що від цього залежить і що зробити»; не частіше разу на 6 год для того самого сервісу;
  коли сервіс знову запрацював — коротке «✅ знову працює».
• Ліміти: відповідь «quota / 429 / exceeded» — одразу окреме попередження (без чекання трьох збоїв).
• Диск: раз на годину — місце на томі з базою (/data) і розмір бази; понад 85 % — попередження.
Стан — у пам'яті процесу (після перезапуску лічильники з нуля), час останніх попереджень — у сховищі бота.
"""

import asyncio
import logging
import os
import re
import shutil
import time

log = logging.getLogger("orion.health")

THRESHOLD = 3
QUIET_H = 6
DISK_PCT = 85

# що зламалось → що від цього залежить і що зробити (людською мовою)
SERVICES = {
    "stt": ("🗣 Розшифровка мови",
            "голосові, «що говорять у відео», 🔤 субтитри, ✂️ короткі ролики, розшифровка інтерв'ю",
            "Додайте безкоштовний GROQ_API_KEY у Railway Variables (console.groq.com → API Keys) — бот переключиться сам."),
    "vision": ("👁 ШІ із зором", "кадри й написи в аналізі допису, картка Threads, водяні знаки, перевірка фото",
               "Перевірте ключі моделей у «🤖 ШІ-моделі → 🩺 Стан моделей»."),
    "shazam": ("🎧 Shazam", "музика у звуці відео, попередження про авторські права на музику",
               "Зазвичай минає саме; якщо довго — повідомте розробнику."),
    "cplan": ("📋 Контент-план", "теми дня, план і завдання редакції",
              "Перевірте, що таблиця відкрита для перегляду за посиланням."),
    "song": ("🎶 Пошук пісень", "«🎵 Музика окремо», «🎶 Повна пісня»",
             "YouTube із сервера просить вхід — потрібен MEDIA_PROXY; SoundCloud і Audius працюють без нього."),
    "media": ("⬇️ Скачування", "скачування з сайту, на якому збої", "Спробуйте «Медіа → Інструменти → 🩺 Перевірити сайти»."),
}
QUOTA_RE = re.compile(r"(?i)quota|exceeded|429|rate.?limit|insufficient|billing")

_fails = {}      # kind -> кількість збоїв поспіль
_last = {}       # kind -> останній текст помилки
_alerted = set()  # kind, про які вже попередили (чекаємо «знову працює»)
_notify = None   # async (text) -> None
_kv = (None, None)


def setup(notify, kv_get=None, kv_set=None):
    global _notify, _kv
    _notify, _kv = notify, (kv_get, kv_set)


def _recent(key):
    get, _set = _kv
    try:
        t = float((get(f"health:{key}") if get else 0) or 0)
    except (TypeError, ValueError):
        t = 0
    return time.time() - t < QUIET_H * 3600


def _mark(key):
    _get, put = _kv
    if put:
        try:
            put(f"health:{key}", str(time.time()))
        except Exception:  # noqa: BLE001
            pass


def _send(text):
    if not _notify:
        return
    try:
        asyncio.get_running_loop().create_task(_notify(text))
    except RuntimeError:  # немає циклу (тести) — лише журнал
        log.warning("health: %s", re.sub(r"<[^>]+>", "", text)[:200])


def fail(kind, detail=""):
    """Збій сервісу. 3 поспіль (або вичерпаний ліміт) — попередження власнику."""
    _fails[kind] = _fails.get(kind, 0) + 1
    _last[kind] = str(detail)[:300]
    name, needs, fix = SERVICES.get(kind, (kind, "", ""))
    quota = bool(QUOTA_RE.search(str(detail)))
    key = f"{kind}:{'quota' if quota else 'fail'}"
    if (quota or _fails[kind] >= THRESHOLD) and not _recent(key):
        _mark(key)
        _alerted.add(kind)
        why = "вичерпано ліміт сервісу" if quota else f"{_fails[kind]} збої поспіль"
        _send(f"🩺 <b>{name}: не працює</b> ({why})\n\n"
              + (f"Від цього залежить: {needs}.\n" if needs else "")
              + (f"Що зробити: {fix}\n" if fix else "")
              + (f"\n<i>Остання помилка: {_esc(_last[kind][:200])}</i>" if _last[kind] else ""))


def ok(kind):
    """Сервіс спрацював. Якщо перед тим попереджали — «знову працює»."""
    if _fails.get(kind):
        _fails[kind] = 0
    if kind in _alerted:
        _alerted.discard(kind)
        name = SERVICES.get(kind, (kind,))[0]
        _send(f"✅ {name} знову працює.")


def status():
    """Для екранів діагностики: [(назва, збоїв поспіль, остання помилка)]."""
    return [(SERVICES.get(k, (k,))[0], n, _last.get(k, "")) for k, n in _fails.items() if n]


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def disk_report(db_path):
    folder = os.path.dirname(os.path.abspath(db_path)) or "/"
    try:
        du = shutil.disk_usage(folder)
    except OSError:
        return None
    pct = du.used * 100 / du.total if du.total else 0
    db = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    return pct, du.used, du.total, db, folder


async def disk_loop(db_path):
    await asyncio.sleep(120)
    while True:
        try:
            rep = disk_report(db_path)
            if rep and rep[0] >= DISK_PCT and not _recent("disk"):
                _mark("disk")
                pct, used, total, db, folder = rep
                _send(f"🩺 <b>Диск майже повний: {pct:.0f} %</b> ({used / 1048576:.0f} з {total / 1048576:.0f} МБ, тека {_esc(folder)}; "
                      f"база {db / 1048576:.1f} МБ).\nЩо зробити: очистити кошик і старі медіа в боті або збільшити Volume у Railway.")
        except Exception:  # noqa: BLE001
            log.exception("health: disk")
        await asyncio.sleep(3600)
