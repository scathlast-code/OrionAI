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


def short_t(p):
    return (p["title"] or p["text"].split("\n")[0])[:24]

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


EG_SYSTEM = ("Ти редактор новинного медіа про екологію, здоров'я й суспільство. З наших старих дописів, що добре зайшли, обери до 3 "
             "«вічнозелених» — тих, що актуальні й зараз (поради, пояснення, сезонні теми, історії без прив'язки до дати), а не новини, "
             "що вже застаріли. Відповідай лише JSON: "
             '{"picks":[{"id":12,"why":"чому актуально зараз","update":"що оновити перед повтором"}]}. Нічого не вигадуй; немає — {"picks":[]}.')


def eg_parse(answer, ids):
    import json
    import re
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return []
    try:
        picks = json.loads(m.group(0)).get("picks") or []
    except ValueError:
        return []
    out = []
    for p in picks[:3]:
        try:
            i = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if i in ids:
            out.append((i, str(p.get("why") or "")[:160], str(p.get("update") or "")[:160]))
    return out


class WeekStatsUI:
    """Мікс до «Новинарні»."""

    def eg_candidates(self, now=None):
        """♻️ Наші дописи старші за 2 місяці (до 2 років), що зайшли краще за звичайні (індекс ≥ 1.5), без нещодавніх повторів."""
        now = now or time.time()
        old = self.ws_posts(now - 730 * 86400, now - 60 * 86400)
        sc = norm_scores(old)
        done = set(self.store.get("eg_done") or [])
        good = [p for p in old if sc.get(p["id"], 0) >= 1.5 and p["id"] not in done]
        return sorted(good, key=lambda p: -sc[p["id"]])[:15], sc

    async def eg_pick(self):
        """ШІ обирає з кандидатів «вічнозелені» (кеш на добу)."""
        cache = self.store.get("eg_cache") or {}
        if cache.get("day") == dt.date.today().isoformat():
            return cache.get("picks") or []
        cands, sc = self.eg_candidates()
        if not cands:
            return []
        listing = "\n".join(f"{p['id']} · {dt.datetime.fromtimestamp(p['at']).strftime('%d.%m.%Y')} · індекс {sc[p['id']]:.1f} · "
                            f"{(p['title'] or p['text'].split(chr(10))[0])[:140]}" for p in cands)
        try:
            ans, _m = await asyncio.wait_for(self.host.ask_ai(listing, system=EG_SYSTEM, label="♻️ Вічнозелені"), 60)
            picks = eg_parse(ans, {p["id"] for p in cands})
        except Exception:  # noqa: BLE001
            log.info("evergreen: ШІ не відповів", exc_info=True)
            picks = []
        self.store.set("eg_cache", {"day": dt.date.today().isoformat(), "picks": picks})
        return picks

    async def eg_screen(self):
        picks = await self.eg_pick()
        L = ["♻️ <b>Варто повторити</b>", "",
             "Наші старі дописи (від 2 місяців), що зайшли краще за звичайні й досі актуальні — ШІ обрав «вічнозелені», а не застарілі новини.", ""]
        rows = []
        for i, why, upd in picks:
            r = self.desk.q("SELECT * FROM ours_posts WHERE id=?", (i,))
            if not r:
                continue
            p = dict(r[0])
            t = esc((p["title"] or p["text"].split("\n")[0])[:100])
            L.append((f"• <a href=\"{esc(p['url'])}\">{t}</a>" if p["url"] else f"• {t}")
                     + f" · {dt.datetime.fromtimestamp(p['at']).strftime('%d.%m.%Y')}")
            if why:
                L.append(f"   ✅ {esc(why)}")
            if upd:
                L.append(f"   ✏️ {esc(upd)}")
            rows.append([(f"✍️ Оновити: {short_t(p)}", self.cb("egw", i)), ("✖️", self.cb("egx", i))])
        if not picks:
            L.append("Поки нічого: потрібні наші дописи з охопленням старші за 2 місяці (бот збирає їх у «🏠 Наші ресурси»).")
        rows.append([("« Статистика", self.cb("ws"))])
        return "\n".join(L)[:4000], rows

    async def eg_route(self, chat, a, x):
        i = int(x) if str(x).isdigit() else 0
        r = self.desk.q("SELECT * FROM ours_posts WHERE id=?", (i,)) if i else []
        if a == "egx" and i:
            self.store.set("eg_done", (self.store.get("eg_done") or [])[-300:] + [i])
            self.store.set("eg_cache", {})
            return "Прибрано зі списку", await self.eg_screen()
        if a == "egw" and r:
            p = dict(r[0])
            topic = (f"Оновити й повторити наш давній допис, що добре зайшов ({dt.datetime.fromtimestamp(p['at']).strftime('%d.%m.%Y')}). "
                     f"Перевір факти й цифри на сьогодні, додай свіже, прибери застаріле.\n\n{p['text'][:3000]}")
            iid, _st, _o = self.store.add_item("evergreen", topic, source_name="♻️ повтор нашого допису", source_url=p["url"] or "",
                                               requested_by=chat)
            self._enqueue(iid)
            self.store.set("eg_done", (self.store.get("eg_done") or [])[-300:] + [i])
            self.store.set("eg_cache", {})
            return "✍️ Пишу оновлену версію — чернетка прийде окремим повідомленням", await self.eg_screen()
        return None, await self.eg_screen()

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
                [[("🕘 Найкращий час докладно", self.cb("wst")), ("♻️ Варто повторити", self.cb("eg"))],
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
                    n_eg = len(await self.eg_pick())
                    await self.host.notify_admins(self.ws_text() + (f"\n\n♻️ Варто повторити: {n_eg} давніх дописів" if n_eg else ""),
                                                  [[("📈 Докладніше", self.cb("ws"))] + ([("♻️ Варто повторити", self.cb("eg"))] if n_eg else [])])
            except Exception:  # noqa: BLE001
                log.exception("week stats loop")
            await asyncio.sleep(1800)
