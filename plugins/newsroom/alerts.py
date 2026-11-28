"""⚡ Термінові сповіщення: щойно бот бачить термінову новину (ШІ позначив «терміново» й важливість висока, джерело не анонімне)
або новину з вашими «словами-тривогами» — одразу пише вам, без чекання зведення.
Без повторів: та сама подія з різних джерел — одне сповіщення (схожість за темою за останні 6 годин).
Тихі години: вночі — лише найважливіше (9–10), решта приходить зранку одним списком. Денна межа, щоб не засипати."""
from __future__ import annotations

import datetime as dt
import re
import time

from plugins import esc
from .ourswrote import stems, topic_score

DEFAULTS = {"on": True, "min": 7, "words": "", "quiet": "23:00-07:00", "cap": 15, "mute": ""}
MINS = [6, 7, 8, 9]


def in_quiet(span, now):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", span or "")
    if not m:
        return False
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    t = now.hour * 60 + now.minute
    return a <= t < b if a < b else (t >= a or t < b)


def word_hits(text, words):
    low = (text or "").lower()
    out = []
    for w in (words or "").replace(",", "\n").splitlines():
        w = w.strip().lower()
        if w and re.search(r"(?<![\w])" + re.escape(w), low):
            out.append(w)
    return out


def decide(cfg, sc, text):
    """-> причина сповіщення або None. sc — оцінка ШІ (importance, urgency, credibility, spam)."""
    if not cfg.get("on", True) or (sc or {}).get("spam"):
        return None
    if word_hits(text, cfg.get("mute")):
        return None
    hits = word_hits(text, cfg.get("words"))
    if hits:
        return "слова-тривоги: " + ", ".join(hits[:3])
    imp = float((sc or {}).get("importance") or 0)
    if (sc or {}).get("urgency") == "urgent" and imp >= cfg.get("min", 7) and (sc or {}).get("credibility") != "anon":
        return f"терміново · важливість {int(imp)}/10"
    return None


class AlertsUI:
    """Мікс до «Новинарні»."""

    def al_cfg(self):
        return {**DEFAULTS, **(self.store.get("al_cfg") or {})}

    def al_save(self, **kw):
        self.store.set("al_cfg", {**(self.store.get("al_cfg") or {}), **kw})

    def _al_now(self):
        return dt.datetime.now(self.host.tz) if self.host.tz else dt.datetime.now().astimezone()

    async def al_maybe(self, title, text, sc, source="", url="", ref=None):
        """Викликається для кожної нової знахідки/матеріалу. ref — ("find", id) або ("draft", id)."""
        cfg = self.al_cfg()
        why = decide(cfg, sc, title + "\n" + (text or ""))
        if not why:
            return False
        recent = [r for r in (self.store.get("al_recent") or []) if r["at"] > time.time() - 6 * 3600]
        mine = stems(title + " " + (text or "")[:600])
        for r in recent:
            n, frac = topic_score(mine, set(r["st"]))
            if n >= 3 and frac >= 0.4:  # та сама подія — вже повідомляли
                return False
        now = self._al_now()
        today = now.date().isoformat()
        sent = self.store.get("al_count") or {}
        if sent.get("d") != today:
            sent = {"d": today, "n": 0}
        imp = float((sc or {}).get("importance") or 0)
        item = {"at": time.time(), "st": sorted(mine)[:60], "title": title[:200], "url": url, "src": source[:60], "why": why,
                "ref": list(ref) if ref else None}
        recent.append(item)
        self.store.set("al_recent", recent[-80:])
        if (in_quiet(cfg["quiet"], now) and imp < 9) or sent["n"] >= cfg["cap"]:
            held = self.store.get("al_held") or []
            held.append(item)
            self.store.set("al_held", held[-30:])
            return True
        sent["n"] += 1
        self.store.set("al_count", sent)
        await self.host.notify_admins(self.al_text(item), self.al_rows(item))
        return True

    def al_text(self, it):
        return (f"⚡ <b>Терміново</b> · {esc(it['why'])}\n\n<b>{esc(it['title'])}</b>\n"
                + (f"🔗 {esc(it['src'] or 'джерело')}: {esc(it['url'])}" if it.get("url") else (f"Джерело: {esc(it['src'])}" if it.get("src") else "")))

    def al_rows(self, it):
        ref = it.get("ref") or []
        rows = []
        if ref and ref[0] == "find":
            rows.append([("📰 Відкрити й написати", self.cb("Fi", ref[1]))])
        elif ref and ref[0] == "draft":
            rows.append([("📰 Відкрити чернетку", self.cb("d", ref[1]))])
        rows.append([("🔕 Менше такого", self.cb("alm")), ("⚙️ Сповіщення", self.cb("al"))])
        return rows

    async def al_morning(self):
        """Те, що притримали тихими годинами, — зранку одним повідомленням."""
        held = self.store.get("al_held") or []
        if not held:
            return
        self.store.set("al_held", [])
        lines = [f"⚡ <b>Поки ви відпочивали · {len(held)}</b>", ""]
        for it in held[-15:]:
            lines.append(f"• <b>{esc(it['title'][:140])}</b>" + (f" — <a href=\"{esc(it['url'])}\">{esc(it['src'] or 'джерело')}</a>" if it.get("url") else ""))
        await self.host.notify_admins("\n".join(lines), [[("⚙️ Сповіщення", self.cb("al"))]])

    def al_screen(self):
        c = self.al_cfg()
        held = len(self.store.get("al_held") or [])
        sent = self.store.get("al_count") or {}
        text = ("⚡ <b>Термінові сповіщення</b>\n\nБот пише одразу, коли бачить термінову важливу новину (оцінка ШІ) "
                "або новину з вашими словами-тривогами. Та сама подія з різних джерел — одне сповіщення.\n\n"
                f"Стан: {'увімкнено ✅' if c['on'] else 'вимкнено'}\n"
                f"📊 Поріг важливості: від {c['min']}/10\n"
                f"🚨 Слова-тривоги: {esc(c['words'].replace(chr(10), ', ')) or 'немає'}\n"
                f"🔕 Не сповіщати про: {esc(c['mute'].replace(chr(10), ', ')) or 'немає'}\n"
                f"🌙 Тихі години: {esc(c['quiet']) or 'немає'} (вночі — лише 9–10/10, решта зранку списком)\n"
                f"🧮 Не більше {c['cap']} на день · сьогодні: {sent.get('n', 0) if sent.get('d') == self._al_now().date().isoformat() else 0}"
                + (f"\n📥 Притримано до ранку: {held}" if held else ""))
        rows = [[("✅ Увімкнено" if c["on"] else "▫️ Вимкнено", self.cb("alc", "on")), (f"📊 Поріг {c['min']}", self.cb("alc", "min"))],
                [("🚨 Слова-тривоги", self.cb("ali", "words")), ("🔕 Не сповіщати про", self.cb("ali", "mute"))],
                [("🌙 Тихі години", self.cb("ali", "quiet")), (f"🧮 Межа {c['cap']}/день", self.cb("alc", "cap"))]]
        if held:
            rows.append([("📥 Показати притримане", self.cb("alh"))])
        rows.append([("« Новинарня", self.cb())])
        return text, rows

    async def al_route(self, chat, a, x, y):
        if a == "alc":
            c = self.al_cfg()
            if x == "on":
                self.al_save(on=not c["on"])
            elif x == "min":
                self.al_save(min=MINS[(MINS.index(c["min"]) + 1) % len(MINS)] if c["min"] in MINS else 7)
            elif x == "cap":
                self.al_save(cap={5: 10, 10: 15, 15: 30, 30: 5}.get(c["cap"], 15))
            return None, self.al_screen()
        if a == "ali":
            hint = {"words": "по рядку або через кому, напр. <code>вибух, ДСНС, Карпати, лісова пожежа</code>",
                    "mute": "теми, про які НЕ сповіщати, напр. <code>футбол, курс валют</code>",
                    "quiet": "проміжок, напр. <code>23:00-07:00</code>; «-» — без тихих годин"}.get(x, "")
            self.ask(chat, "al" + x)
            return None, (f"✍️ Напишіть {hint}. «-» — очистити.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "alh":
            await self.al_morning()
            return "📥 Надіслано", self.al_screen()
        if a == "alm":
            return None, ("🔕 Щоб такого було менше: підніміть поріг важливості або додайте тему в «Не сповіщати про».", self.al_screen()[1])
        return None, self.al_screen()

    async def al_text_in(self, chat, key, data, text):
        field = key[2:]
        val = "" if text.strip() in ("-", "—") else text.strip()[:1000]
        if field == "quiet" and val and not re.fullmatch(r"\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*", val):
            return "Формат: 23:00-07:00", self.al_screen()
        self.al_save(**{field: val})
        return "Збережено", self.al_screen()
