"""📎 Зноски-джерела: під кожною частиною тексту — її джерело гіперпосиланням (а не окремий список унизу)."""
from __future__ import annotations

from html import escape
import re


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


def visible_len(text):
    import html as _h
    return len(_h.unescape(re.sub(r"<[^>]+>", "", text or "")))


def _raw_pos(text, room):
    """Позиція в HTML, де видимого тексту набирається room символів."""
    import html as _h
    vis, i, n = 0, 0, len(text)
    while i < n:
        if text[i] == "<":
            j = text.find(">", i)
            i = n if j < 0 else j + 1
            continue
        if text[i] == "&":
            m = re.match(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);", text[i:])
            step = len(m.group(0)) if m else 1
            w = len(_h.unescape(m.group(0))) if m else 1
        else:
            step, w = 1, 1
        if vis + w > room:
            break
        vis += w
        i += step
    return i


def html_cut(text, limit=4000, tail=""):
    """Обрізає HTML для Telegram, не розриваючи теги: ріже по рядку, закриває відкриті <b>, <i>, <a>, <blockquote>."""
    text = text or ""
    room = limit - visible_len(tail)
    if visible_len(text) <= room:  # ліміт Telegram рахує видимий текст — теги й адреси посилань не враховуються
        return text + tail
    cut = text[:_raw_pos(text, room)]
    room = len(cut)
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


def html_split(text, limit=3900):
    """Ділить довгий HTML на частини для Telegram, не розриваючи тегів: відкриті <b>, <blockquote>… закриваються
    в кінці частини й відкриваються знову на початку наступної. Ріже по порожніх рядках, за потреби — по рядках."""
    text = text or ""
    if visible_len(text) <= limit:
        return [text]
    parts, cur, opened = [], "", []  # opened — повні відкривні теги, що діють на межі

    def stack_after(chunk, start):
        st = list(start)
        for m in _TAG.finditer(chunk):
            name = m.group(2).lower()
            if m.group(1):
                for i in range(len(st) - 1, -1, -1):
                    if re.match(rf"<{name}\b", st[i], re.I):
                        del st[i:]
                        break
            else:
                st.append(m.group(0))
        return st

    def close(st):
        return "".join(f"</{re.match(r'<(\w[\w-]*)', t).group(1)}>" for t in reversed(st))

    pieces = re.split(r"(\n\n)", text)
    lines = []
    for p in pieces:
        if visible_len(p) > limit:  # завеликий абзац — по рядках
            lines += re.split(r"(\n)", p)
        else:
            lines.append(p)
    start = []
    for seg in lines:
        if cur and visible_len(cur + seg) > limit:
            st = stack_after(cur, start)
            parts.append("".join(start) + cur + close(st))
            start, cur = st, ""
            seg = seg.lstrip("\n")
        cur += seg
    if cur.strip():
        parts.append("".join(start) + cur)
    return [html_cut(p, 4000) for p in parts if p.strip()]
