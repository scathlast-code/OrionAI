"""📈 Статистика наших каналів за тиждень і найкращий час публікацій.

Дані — наші власні дописи з охопленням, які бот збирає в «🏠 Наші ресурси» (Telegram-канали, сайт, соцмережі).
- Тиждень: скільки дописів, переглядів, реакцій; порівняння з попереднім тижнем; по кожному каналу; найсильніші й найслабші дописи.
- Найкращий час: за 60 днів — у які години й дні тижня дописи збирають більше (медіана індексу допису; година — лише якщо
  в ній щонайменше 3 дописи, щоб випадковість не вирішувала).
Індекс допису — той самий, що в місячному звіті: перегляди/100 + реакції + 2·коментарі + 3·поширення, поділено на медіану платформи.
Звіт за тиждень може приходити сам щопонеділка."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import statistics
import time

from plugins import esc
from .report import raw_score

log = logging.getLogger("orion.newsroom.weekstats")
DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def norm_scores(posts):
    """-> {id: індекс} (1.0 — типовий допис своєї платформи)."""
    by = {}
    for p in posts:
        by.setdefault(p.get("platform") or "tg", []).append(raw_score(p))
    med = {k: (statistics.median([v for v in vs if v > 0]) if any(v > 0 for v in vs) else 0) for k, vs in by.items()}
    return {p["id"]: (raw_score(p) / med[p.get("platform") or "tg"] if med[p.get("platform") or "tg"] else 0) for p in posts}


def best_times(posts, tz=None, min_n=3):
    """-> (години [(год, медіана, n)], дні [(день, медіана, n)]) — найкращі вгорі."""
    sc = norm_scores(posts)
    hours, days = {}, {}
    for p in posts:
        if not any(p.get(k) for k in ("views", "likes", "reposts", "comments")):
            continue
        t = dt.datetime.fromtimestamp(p["at"], tz)
        hours.setdefault(t.hour, []).append(sc[p["id"]])
        days.setdefault(t.weekday(), []).append(sc[p["id"]])
    h = sorted(((k, statistics.median(v), len(v)) for k, v in hours.items() if len(v) >= min_n), key=lambda x: -x[1])
    d = sorted(((k, statistics.median(v), len(v)) for k, v in days.items() if len(v) >= min_n), key=lambda x: -x[1])
    return h, d


def week_summary(cur, prev):
    def tot(ps, k):
        return sum(p.get(k) or 0 for p in ps)
    out = {"n": len(cur), "pn": len(prev)}
    for k in ("views", "likes", "reposts", "comments"):
        out[k], out["p" + k] = tot(cur, k), tot(prev, k)
    chans = {}
    for p in cur:
        c = chans.setdefault(p.get("source") or "?", {"n": 0, "views": 0, "likes": 0, "reposts": 0})
        c["n"] += 1
        for k in ("views", "likes", "reposts"):
            c[k] += p.get(k) or 0
    out["chans"] = sorted(chans.items(), key=lambda kv: -kv[1]["views"])
    sc = norm_scores(cur)
    ranked = sorted([p for p in cur if sc.get(p["id"])], key=lambda p: -sc[p["id"]])
    out["top"] = [(p, sc[p["id"]]) for p in ranked[:5]]
    out["flop"] = [(p, sc[p["id"]]) for p in ranked[-3:][::-1]] if len(ranked) >= 8 else []
    out["no_metrics"] = sum(1 for p in cur if not any(p.get(k) for k in ("views", "likes", "reposts", "comments")))
    return out


def delta(a, b):
    if not b:
        return ""
    d = round((a - b) * 100 / b)
    return f" ({'+' if d >= 0 else ''}{d}% до мин. тижня)"


def num(n):
    return f"{n:,}".replace(",", " ")


class WeekStatsUI:
    """Мікс до «Новинарні»."""

    def ws_posts(self, a, b):
        return [dict(r) for r in self.desk.q("SELECT * FROM ours_posts WHERE at>=? AND at<? ORDER BY at", (a, b))]

    def ws_text(self):
        now = time.time()
        cur, prev = self.ws_posts(now - 7 * 86400, now), self.ws_posts(now - 14 * 86400, now - 7 * 86400)
        s = week_summary(cur, prev)
        L = ["📈 <b>Наші канали за 7 днів</b>", ""]
        if not cur:
            L.append("За тиждень наших дописів у базі немає. Перевірте «🏠 Наші ресурси»: чи додані ваші канали й сайт.")
            return "\n".join(L)
        L += [f"📝 Дописів: <b>{s['n']}</b>{delta(s['n'], s['pn'])}",
              f"👁 Переглядів: <b>{num(s['views'])}</b>{delta(s['views'], s['pviews'])}",
              f"❤️ Реакцій: {num(s['likes'])}{delta(s['likes'], s['plikes'])} · 🔁 поширень: {num(s['reposts'])} · 💬 коментарів: {num(s['comments'])}"]
        if s["no_metrics"]:
            L.append(f"ℹ️ Без даних охоплення: {s['no_metrics']} (сайт чи мережі, де бот не бачить переглядів)")
        if len(s["chans"]) > 1:
            L += ["", "<b>По каналах</b>"] + [f"• {esc(k[:40])}: {v['n']} доп. · 👁 {num(v['views'])} · ❤️ {num(v['likes'])}" for k, v in s["chans"][:8]]
        if s["top"]:
            L += ["", "🏆 <b>Найсильніші</b> (індекс: 1.0 — звичайний допис)"]
            for p, x in s["top"]:
                t = esc((p["title"] or p["text"].split("\n")[0])[:90])
                L.append(f"{x:.1f} · " + (f"<a href=\"{esc(p['url'])}\">{t}</a>" if p["url"] else t) + f" · 👁 {num(p.get('views') or 0)}")
        if s["flop"]:
            L += ["", "🔻 <b>Найслабші</b>"]
            for p, x in s["flop"]:
                t = esc((p["title"] or p["text"].split("\n")[0])[:90])
                L.append(f"{x:.1f} · " + (f"<a href=\"{esc(p['url'])}\">{t}</a>" if p["url"] else t))
        h, d = best_times(self.ws_posts(now - 60 * 86400, now), self.host.tz)
        if h:
            L += ["", "🕘 <b>Найкращий час</b> (за 60 днів): " + ", ".join(f"{k:02d}:00" for k, _m, _n in h[:3])
                  + (" · дні: " + ", ".join(DAYS[k] for k, _m, _n in d[:3]) if d else "")]
        return "\n".join(L)[:4000]

    def ws_screen(self):
        auto = self.store.get("ws_auto") is not False
        return (self.ws_text() + f"\n\n📬 Щопонеділковий звіт: {'увімкнено' if auto else 'вимкнено'}",
                [[("🕘 Найкращий час докладно", self.cb("wst"))],
                 [("📬 Щопонеділка: " + ("так" if auto else "ні"), self.cb("wsa")), ("📊 Звіт за місяць", self.cb("rp"))],
                 [("« Новинарня", self.cb())]])

    def ws_times_screen(self):
        now = time.time()
        posts = self.ws_posts(now - 60 * 86400, now)
        h, d = best_times(posts, self.host.tz)
        L = ["🕘 <b>Коли публікувати</b> · за 60 днів", "",
             "Медіана індексу дописів за годиною публікації (1.0 — звичайний допис). Година — лише якщо в ній щонайменше 3 дописи.", ""]
        if not h:
            L.append("Замало дописів з охопленням, щоб робити висновки. Потрібно хоча б кілька десятків.")
        else:
            mx = max(m for _k, m, _n in h) or 1
            for k, m, n in sorted(h):
                L.append(f"{k:02d}:00 {'🟩' * max(1, round(m / mx * 8))} {m:.1f} · {n} доп.")
            if d:
                L += ["", "<b>Дні тижня</b>: " + " · ".join(f"{DAYS[k]} {m:.1f}" for k, m, _n in sorted(d))]
        rows = []
        if h:
            rows.append([("🗓 Взяти найкращі години для черги", self.cb("wsq"))])
        rows.append([("« Статистика", self.cb("ws"))])
        return "\n".join(L)[:4000], rows

    async def ws_route(self, chat, a, x, y):
        if a == "wst":
            return None, self.ws_times_screen()
        if a == "wsa":
            self.store.set("ws_auto", self.store.get("ws_auto") is False)
            return None, self.ws_screen()
        if a == "wsq":
            now = time.time()
            h, _d = best_times(self.ws_posts(now - 60 * 86400, now), self.host.tz)
            slots = sorted(f"{k:02d}:00" for k, _m, _n in [x for x in h if 7 <= x[0] <= 22][:4])  # ніч не пропонуємо
            if slots:
                self.store.set("pq_slots", slots)
            return "🗓 Типові години черги: " + ", ".join(slots), self.ws_times_screen()
        return None, self.ws_screen()

    async def _ws_loop(self):
        await asyncio.sleep(150)
        while True:
            try:
                now = dt.datetime.now(self.host.tz) if self.host.tz else dt.datetime.now()
                wk = now.strftime("%G-W%V")
                if (now.weekday() == 0 and now.hour >= 9 and self.store.get("ws_sent") != wk and self.store.get("ws_auto") is not False
                        and self.on()):
                    self.store.set("ws_sent", wk)
                    await self.host.notify_admins(self.ws_text(), [[("📈 Докладніше", self.cb("ws"))]])
            except Exception:  # noqa: BLE001
                log.exception("week stats loop")
            await asyncio.sleep(1800)
