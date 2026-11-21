"""🔤 Охорона мови: у тексті українською не має бути випадкових слів іншими мовами («traditionally», «不幸»).

Моделі (особливо безкоштовні) інколи вставляють англійське чи китайське слово посеред українського речення.
foreign() знаходить такі вставки; fix() просить модель замінити їх українськими відповідниками,
а якщо не вийшло — прибирає ієрогліфи, щоб текст лишався читабельним.
Дозволено: назви й бренди з великої літери (Gemini, Open-Meteo), абревіатури, посилання, код у `…`."""
from __future__ import annotations

import re

ALIEN = re.compile(r"[؀-ۿ֐-׿฀-๿぀-ヿ㐀-䶿一-鿿가-힯]+")
LATIN = re.compile(r"(?<![\w/@.#`-])[a-z]{4,}(?![\w/@.`-])")
URLS = re.compile(r"https?://\S+|`[^`]*`|<[^>]+>")
CYR = re.compile(r"[а-яіїєґ]", re.I)


def foreign(text, source=""):
    """Іншомовні вставки в українському тексті (порожньо — усе гаразд). source — запит людини: якщо там є ці ж слова, вони доречні."""
    t = URLS.sub(" ", text or "")
    if len(CYR.findall(t)) < 20:  # текст не український (або дуже короткий) — не чіпаємо
        return []
    src = (source or "").lower() if isinstance(source, str) else ""
    bits = ALIEN.findall(t)
    bits += [w for w in LATIN.findall(t) if w not in src]
    return [b for b in dict.fromkeys(bits) if b not in src]


def strip_alien(text):
    return ALIEN.sub("", text or "")


FIX_PROMPT = ("Ось текст українською, у якому випадково трапились слова іншими мовами: {bits}. "
              "Перепиши текст повністю українською: заміни ці слова точними українськими відповідниками за змістом, "
              "більше нічого не змінюй (оформлення, емодзі, розмітку, назви й посилання лиши як є). Відповідай лише виправленим текстом.\n\n{text}")


async def fix(text, ask, source=""):
    """ask(prompt) -> str. Повертає виправлений текст (або той самий, якщо вставок немає)."""
    bits = foreign(text, source)
    if not bits:
        return text
    try:
        out = (await ask(FIX_PROMPT.format(bits=", ".join(bits[:12]), text=text)) or "").strip()
    except Exception:  # noqa: BLE001
        out = ""
    if out and len(out) > len(text) * 0.6 and len(foreign(out, source)) < len(bits):
        text = out
    return strip_alien(text) if ALIEN.search(text or "") and not ALIEN.search(source or "") else text


# ================================================================ 🔎 обов'язкова перевірка кожної публікації
REVIEW_PROMPT = (
    "Ти — редактор-коректор і фактчекер українського тексту, який бот збирається опублікувати ({kind}). Перевір КОЖНЕ речення:\n"
    "1) мова: неіснуючі вислови й кальки (напр. «на відпрацюванні посту»), безглузді словосполучення, граматика, іншомовні слова "
    "посеред тексту — виправ на правильну природну українську;\n"
    "2) факти: лиши лише те, що відоме й перевірне; якщо твердження сумнівне, вигадане, суперечливе чи переплутане "
    "(не той день, не те свято, не той звичай) — виправ, якщо точно знаєш правильне, інакше прибери це речення;\n"
    "3) нічого нового від себе не додавай, оформлення, емодзі, посилання й порядок рядків збережи.\n"
    "{sources}"
    "Відповідай лише остаточним виправленим текстом, без пояснень. Якщо все гаразд — поверни текст без змін.\n\nТЕКСТ:\n{text}")


async def review(text, ask, kind="публікація", sources=""):
    """Перевіряє текст перед показом/публікацією: мова + факти (ask — бажано ІНША модель, ніж та, що писала).
    Повертає перевірений текст; якщо перевірка не вдалася — текст з виправленими іншомовними вставками й позначкою."""
    if not (text or "").strip():
        return text
    src = f"Джерела для звірки (довіряй їм більше, ніж собі):\n{sources[:3000]}\n" if sources else ""
    try:
        out = (await ask(REVIEW_PROMPT.format(kind=kind, sources=src, text=text)) or "").strip()
    except Exception:  # noqa: BLE001
        out = ""
    if out and 0.3 * len(text) < len(out) < 1.6 * len(text) + 200:
        return strip_alien(out) if not foreign(text) or len(foreign(out)) <= len(foreign(text)) else strip_alien(text)
    return strip_alien(text) + "\n(⚠️ автоматична перевірка не вдалася — звіряйте)"
