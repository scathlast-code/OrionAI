"""📦 Групове завантаження: кілька посилань одним повідомленням → один вибір → по черзі, кожне окремим дописом.

Текст біля кожного посилання (у тому ж рядку чи до наступного посилання) — вказівки про теги саме для нього:
    https://threads.com/…  сортуй по тегу полісся
    https://instagram.com/…
    без тегів
Спільні вказівки для всіх — текст перед першим посиланням.
"""
from __future__ import annotations

import re

from . import related as R

URL = re.compile(r"https?://[^\s<>«»\"']+")
MODES = (("auto", "✨ Як є"), ("video", "🎬 Відео"), ("audio", "🎵 Звук"), ("gif", "🎞 GIF"), ("mute", "🔇 Без звуку"))
MODE_ICON = {"auto": "✨", "video": "🎬", "audio": "🎵", "gif": "🎞", "mute": "🔇"}


def split(text):
    """-> (спільні вказівки | None, [(посилання, вказівки | None, сирий текст біля нього)]) або None, якщо посилань менше двох."""
    text = text or ""
    found = list(URL.finditer(text))
    if len(found) < 2:
        return None
    common = R.parse_instr(text[:found[0].start()])
    items, seen = [], set()
    for i, m in enumerate(found):
        url = m.group(0).rstrip(").,;]»")
        if url in seen:
            continue
        seen.add(url)
        tail = text[m.end():found[i + 1].start() if i + 1 < len(found) else len(text)].strip(" \n,;—-")
        items.append([url, R.parse_instr(tail), tail[:200]])
    return (common, items) if len(items) >= 2 else None


def own_tags(text):
    """Те, що людина ввела кнопкою «✍️ Теги до №…»: вказівки («без тегів», «не став тег …») або просто теги через кому."""
    instr = R.parse_instr(text)
    if instr:
        return instr
    tags = [R.clean_tag(t) for t in re.split(r"[,;\n]|\s+і\s+|\s+та\s+", text or "")]
    tags = [t for t in tags if t]
    return {"add": tags, "drop": [], "none": False} if tags else None


def merge_instr(common, own):
    """Спільні вказівки + свої для допису (свої важливіші; «без тегів» у будь-якій — без тегів)."""
    if not common and not own:
        return None
    common, own = common or {}, own or {}
    if own.get("none") or (common.get("none") and not own.get("add")):
        return {"add": [], "drop": [], "none": True}
    return {"add": list(dict.fromkeys(common.get("add", []) + own.get("add", []))),
            "drop": list(dict.fromkeys(common.get("drop", []) + own.get("drop", []))), "none": False}


def instr_short(instr):
    if not instr:
        return "теги — сам визначу"
    if instr.get("none"):
        return "без тегів"
    parts = []
    if instr.get("add"):
        parts.append("✍️ " + ", ".join(instr["add"]))
    if instr.get("drop"):
        parts.append("не ставити: " + ", ".join(instr["drop"]))
    return " · ".join(parts) + " + сам доповню"
