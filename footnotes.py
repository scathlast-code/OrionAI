"""📎 Зноски-гіперлінки для текстів бота: кожне джерело — номер [n] біля своєї частини, список джерел — у кінці."""
from __future__ import annotations

import re
from html import escape


def esc(s):
    return escape(str(s or ""), quote=True)


class Notes:
    def __init__(self):
        self.urls, self.labels = [], []

    def mark(self, url, label=""):
        url = str(url or "")
        if not url.startswith("http"):
            return ""
        if url not in self.urls:
            self.urls.append(url)
            self.labels.append(label or re.sub(r"^https?://(www\.)?", "", url).split("/")[0])
        n = self.urls.index(url) + 1
        return f' <a href="{esc(url)}">[{n}]</a>'

    def legend(self, title="📎 <b>Джерела:</b> "):
        if not self.urls:
            return ""
        return "\n" + title + " · ".join(f'<a href="{esc(u)}">[{i}] {esc(l)}</a>' for i, (u, l) in enumerate(zip(self.urls, self.labels), 1))
