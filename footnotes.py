"""📎 Зноски-джерела: під кожною частиною тексту — її джерело гіперпосиланням (а не окремий список унизу)."""
from __future__ import annotations

from html import escape


def esc(s):
    return escape(str(s or ""), quote=True)


class Notes:
    """Джерело — гіперпосиланням одразу після свого речення/рядка: mark(url, назва) повертає « · <a>назва</a>».
    flush() лишено для сумісності — окремих рядків-зносок більше немає."""

    def __init__(self):
        self.cur = []

    @staticmethod
    def short(label, url):
        lab = (label or "").split(" (")[0].split(" · ")[0].strip()
        return lab or url.split("//", 1)[-1].split("/")[0].removeprefix("www.")

    def mark(self, url, label=""):
        url = str(url or "")
        if not url.startswith("http"):
            return ""
        return f' · <a href="{esc(url)}">{esc(self.short(label, url))}</a>'

    def flush(self):
        return ""

    def legend(self, *a, **k):
        return ""


import re as _re

_TAG = _re.compile(r"<(/?)([a-zA-Z]+)[^>]*>")


def html_cut(text, limit=4000, tail=""):
    """Обрізає HTML для Telegram, не розриваючи теги: ріже по рядку, закриває відкриті <b>, <i>, <a>, <blockquote>."""
    text = text or ""
    room = limit - len(tail)
    if len(text) <= room:
        return text + tail
    cut = text[:room]
    nl = cut.rfind("\n")
    if nl > room * 0.5:
        cut = cut[:nl]
    lt = cut.rfind("<")
    if lt > cut.rfind(">"):  # обірваний тег наприкінці
        cut = cut[:lt]
    stack = []
    for m in _TAG.finditer(cut):
        name = m.group(2).lower()
        if m.group(1):
            if name in stack:
                while stack and stack.pop() != name:
                    pass
        else:
            stack.append(name)
    closing = "".join(f"</{t}>" for t in reversed(stack))
    return cut + "…" + closing + tail
