"""🧩 Власні набори: створити з нуля, зібрати зі стікерів інших пакетів (масово чи по одному), об'єднати, почистити, прибрати зайве.

Набір створює бот від імені користувача (Bot API createNewStickerSet) — він з'являється в Telegram у власника як його набір,
а назва закінчується на _by_<бот>. Файли не перезавантажуються: беремо file_id наявних стікерів."""
from __future__ import annotations

import re
import secrets

from telegram import InputSticker

LIMIT = {"regular": 120, "custom_emoji": 200}
FIRST_BATCH = 50  # createNewStickerSet приймає від 1 до 50 стікерів, решту додаємо по одному
# Пакети, яких не беремо: російська/білоруська пропаганда й мова, 18+, лайка.
BLOCK_RE = re.compile(r"[ыэъё]|\bzov\b|\bzv?\b|росси|русск|путин|ватн|porn|порн|xxx|18\+|nsfw|хуй|бля|пизд|ёб|сука", re.I)


def fmt(st):
    return "video" if st.is_video else "animated" if st.is_animated else "static"


def blocked(title, name=""):
    return bool(BLOCK_RE.search(f"{title} {name}"))


def slug(title, bot_username):
    tr = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z", "и": "y",
                        "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
                        "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia"})
    base = re.sub(r"[^a-z0-9]+", "_", (title or "").lower().translate(tr)).strip("_")[:24] or "set"
    if not base[0].isalpha():
        base = "s" + base
    suffix = f"_{secrets.token_hex(2)}_by_{bot_username}"
    return (base[: 64 - len(suffix)] + suffix).replace("__", "_")


def dedup(stickers, have=()):
    seen, out = set(have), []
    for s in stickers:
        if s.file_unique_id in seen:
            continue
        seen.add(s.file_unique_id)
        out.append(s)
    return out


def to_input(st):
    return InputSticker(sticker=st.file_id, emoji_list=[st.emoji or "🙂"], format=fmt(st))


async def pack_stickers(bot, names):
    """Стікери з пакетів; заблоковані пакети — окремо, із поясненням."""
    got, skipped = [], []
    for name in names:
        try:
            s = await bot.get_sticker_set(name)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{name}: недоступний ({type(exc).__name__})")
            continue
        if blocked(s.title, s.name):
            skipped.append(f"{s.title}: не беремо (мова чи зміст)")
            continue
        got += list(s.stickers)
    return got, skipped


async def create(bot, user_id, title, stickers, bot_username):
    """→ (назва набору, тип, додано, пропущено). Тип — за першим стікером; інший тип пропускаємо."""
    stickers = dedup(stickers)
    if not stickers:
        raise ValueError("немає стікерів")
    kind = stickers[0].type
    same = [s for s in stickers if s.type == kind][: LIMIT.get(kind, 120)]
    skipped = len(stickers) - len(same)
    name = slug(title, bot_username)
    await bot.create_new_sticker_set(user_id, name, (title or "Мій набір")[:64], [to_input(s) for s in same[:FIRST_BATCH]], sticker_type=kind)
    added = min(len(same), FIRST_BATCH)
    for s in same[FIRST_BATCH:]:
        try:
            await bot.add_sticker_to_set(user_id, name, to_input(s))
            added += 1
        except Exception:  # noqa: BLE001
            skipped += 1
    return name, kind, added, skipped


async def add(bot, user_id, name, stickers):
    """Додати в наш набір (без повторів, лише того ж типу, до ліміту) → (додано, пропущено)."""
    cur = await bot.get_sticker_set(name)
    room = LIMIT.get(cur.sticker_type, 120) - len(cur.stickers)
    todo = [s for s in dedup(stickers, {x.file_unique_id for x in cur.stickers}) if s.type == cur.sticker_type]
    skipped, added = len(stickers) - len(todo), 0
    for s in todo[:max(0, room)]:
        try:
            await bot.add_sticker_to_set(user_id, name, to_input(s))
            added += 1
        except Exception:  # noqa: BLE001
            skipped += 1
    return added, skipped + max(0, len(todo) - max(0, room))


async def clean(bot, name):
    """Прибрати повтори (однаковий файл) → скільки прибрано."""
    cur = await bot.get_sticker_set(name)
    seen, n = set(), 0
    for s in cur.stickers:
        if s.file_unique_id in seen:
            await bot.delete_sticker_from_set(s.file_id)
            n += 1
        seen.add(s.file_unique_id)
    return n
