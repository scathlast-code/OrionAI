"""🧹 Чистий текст репосту: прибираємо технічне сміття чужих каналів і сайтів — «підпишись», «написати адміну»,
адреси каналів (@назва, t.me/…), «надіслати новину», рекламні приписки, рядки з самих посилань чи хештегів.
Самий зміст новини (і посилання всередині речень) лишається."""
from __future__ import annotations

import re

_CTA = re.compile(
    r"(?i)(підпис(?:атись|атися|уйтесь|уйся|ывайтесь|ывайся|ись|іться|ться)|subscribe|follow us|"
    r"напис(?:ати|ать|ати нам) (?:адміну|админу|адміністратору|нам)|адмін(?:у|)\s*[:—-]|админ\s*[:—-]|зв'?язок з адміном|"
    r"надісла(?:ти|ть) новину|прислать новость|запропонувати новину|предложить новость|повідомити новину|"
    r"наш (?:канал|чат|сайт|бот)|наш\w* (?:instagram|інстаграм|facebook|фейсбук|tiktok|тікток|youtube|ютуб)|"
    r"(?:будь )?в курсі подій|будьте в курсі|стежте за нами|слідкуйте за нами|приєднуйтесь|присоединяйтесь|"
    r"підтримати канал|поддержать канал|донат\w*|monobank\.ua/jar|send\.monobank|реклама|#реклама|співпраця\s*[:—-]|сотрудничество\s*[:—-]|"
    r"чат[- ]бот для новин|бот для новин|прислати фото|прислать фото)")
_HANDLE = r"@[A-Za-z0-9_]{4,}"
_LINK = r"(?:https?://)?(?:t\.me|telegram\.me|instagram\.com|facebook\.com|fb\.com|tiktok\.com|youtube\.com|youtu\.be|x\.com|twitter\.com|threads\.net)/\S*"
_ONLY_JUNK = re.compile(rf"(?i)^[\s\W\d_]*(?:(?:{_HANDLE}|{_LINK}|https?://\S+|#\w+)[\s|·•,;:—–\-]*)+[\s\W]*$")
_EMOJI_ARROWS = re.compile(r"^[\s👉👇➡️⬇️🔗📲📱✅🔥⚡️💙💛🇺🇦|•·—–\-:]+$")


def clean(text):
    """Повертає текст без технічних приписок. Якщо після чистки нічого не лишилось — повертає оригінал."""
    if not text:
        return text
    out = []
    for line in str(text).splitlines():
        s = line.strip()
        if not s:
            out.append("")
            continue
        if _ONLY_JUNK.match(s) or _EMOJI_ARROWS.match(s):
            continue  # рядок — лише адреси каналів, посилання чи хештеги
        if _CTA.search(s) and len(re.sub(rf"{_HANDLE}|{_LINK}|https?://\S+", "", s)) < 120:
            continue  # коротка приписка «підпишись / напиши адміну / наш канал»
        s2 = re.sub(rf"\s*(?:[|·•]\s*)?(?:{_HANDLE}|{_LINK})\s*$", "", s)  # адреса каналу в кінці рядка
        out.append(line.replace(s, s2) if s2 else line)
    res = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return res or str(text).strip()
