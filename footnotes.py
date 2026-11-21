"""📎 Зноски-джерела: під кожною частиною тексту — її джерело гіперпосиланням (а не окремий список унизу)."""
from __future__ import annotations

from html import escape


def esc(s):
    return escape(str(s or ""), quote=True)


class Notes:
    """mark(url, назва) — запам'ятати джерело поточної частини; flush() — рядок-зноска під цією частиною."""

    def __init__(self):
        self.cur = []

    def mark(self, url, label=""):
        url = str(url or "")
        if url.startswith("http") and url not in [u for u, _ in self.cur]:
            self.cur.append((url, label or url.split("//", 1)[-1].split("/")[0]))
        return ""

    def flush(self):
        if not self.cur:
            return ""
        groups = {}
        for u, l in self.cur:  # «Open-Meteo · Захід», «Open-Meteo · Північ» → «Open-Meteo: Захід · Північ»
            head, _, tail = l.partition(" · ")
            groups.setdefault(head if tail else "", []).append((u, tail or l))
        parts = []
        for head, items in groups.items():
            links = " · ".join(f'<a href="{esc(u)}">{esc(l)}</a>' for u, l in items)
            parts.append(f"{esc(head)}: {links}" if head else links)
        many = len(self.cur) > 1
        self.cur = []
        return f"↳ <i>{'Джерела' if many else 'Джерело'}:</i> " + "; ".join(parts)

    def legend(self, *a, **k):  # сумісність: окремого списку джерел більше немає
        return ""
