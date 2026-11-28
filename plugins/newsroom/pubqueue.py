"""🗓 Черга публікацій: схвалена новина → на вибраний час.

- Telegram-канал (підключений у 📤 Публікація) — бот публікує сам у призначений час.
- Instagram, Threads, X, Facebook — бот не має доступу публікувати від вашого імені, тому в призначений час надсилає вам
  готовий текст цієї мережі (з «📲 Під мережі») з позначкою «✅ Опубліковано» — щоб нічого не забулося.
Пауза, скасування й «зараз» — для кожного запису окремо; запис не губиться після перезапуску."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time

from plugins import esc
from .crosspost import NETWORKS, ORDER

log = logging.getLogger("orion.newsroom.queue")
SLOTS_DEFAULT = ["08:00", "12:00", "18:00", "21:00"]
ST = {"wait": "⏳", "pause": "⏸", "done": "✅", "fail": "⚠️", "remind": "🔔"}


def parse_when(text, now):
    """«18:30», «29.11 09:00», «+45» (хвилин), «+2г» → datetime у тому ж поясі, що й now; None, якщо не розібрати."""
    t = (text or "").strip().lower()
    m = re.fullmatch(r"\+\s*(\d{1,4})\s*(хв|m|г|год|h)?", t)
    if m:
        n = int(m[1])
        return now + (dt.timedelta(hours=n) if (m[2] or "") in ("г", "год", "h") else dt.timedelta(minutes=n))
    m = re.fullmatch(r"(?:(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s+)?(\d{1,2})[:.](\d{2})", t)
    if not m:
        return None
    hh, mm = int(m[4]), int(m[5])
    if hh > 23 or mm > 59:
        return None
    if m[1]:
        year = int(m[3]) + (2000 if m[3] and len(m[3]) == 2 else 0) if m[3] else now.year
        try:
            when = now.replace(year=year, month=int(m[2]), day=int(m[1]), hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            return None
        if when < now and not m[3]:
            when = when.replace(year=year + 1)
        return when
    when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return when if when > now else when + dt.timedelta(days=1)


def when_text(ts, now):
    w = dt.datetime.fromtimestamp(ts, now.tzinfo)
    day = "сьогодні" if w.date() == now.date() else "завтра" if w.date() == now.date() + dt.timedelta(days=1) else f"{w:%d.%m}"
    return f"{day} {w:%H:%M}"


class QueueUI:
    """Мікс до «Новинарні»: черга публікацій."""

    def _now(self):
        return dt.datetime.now(self.host.tz) if self.host.tz else dt.datetime.now().astimezone()

    def pq(self):
        return self.store.get("pq") or []

    def pq_save(self, q):
        keep = [r for r in q if r["st"] in ("wait", "pause")] + [r for r in q if r["st"] not in ("wait", "pause")][-100:]
        self.store.set("pq", sorted(keep, key=lambda r: r["at"]))

    def pq_slots(self):
        return self.store.get("pq_slots") or SLOTS_DEFAULT

    def pq_add(self, did, nets, at, by):
        q = self.pq()
        nid = max([r["id"] for r in q] + [0]) + 1
        for k in nets:
            q.append({"id": nid, "did": did, "net": k, "at": at, "st": "wait", "by": by, "err": ""})
            nid += 1
        self.pq_save(q)

    # ------------------------------------------------------------ екрани
    def pq_plan(self, did):
        """Куди й коли: вибір мереж і часу для новини."""
        d = self.store.draft(did)
        if not d:
            return "Новину не знайдено.", [[("« Новинарня", self.cb())]]
        if d["status"] not in ("approved",):
            return ("🗓 <b>Запланувати</b>\n\nЗапланувати можна лише схвалену новину: спершу «✅ Схвалити» (бот перевірить факти).",
                    [[("« Новина", self.cb("d", did))]])
        sel = self.store.get(f"pqsel:{did}") or (["tg"] if self.store.get("publish_chat") else [])
        have = self.xp_get(did)
        lines = [f"🗓 <b>Запланувати #{did}</b>", f"<b>{esc(d['headline'])}</b>", "", "1️⃣ Куди (натисніть, щоб вибрати):"]
        rows, nets = [], []
        for k in ORDER:
            icon, name = NETWORKS[k][:2]
            if k == "tg":
                ok = bool(self.store.get("publish_chat"))
                note = f"бот опублікує сам у «{esc(self.store.get('publish_title') or '')}»" if ok else "канал не підключено (📤 Публікація)"
            else:
                ok = True
                note = "нагадаю з готовим текстом" + ("" if k in have else " (версію підготую заздалегідь)")
            lines.append(f"{'✅' if k in sel else '▫️'} {icon} {name} — {note}")
            if ok:
                nets.append(((("☑️ " if k in sel else "⬜ ") + icon), self.cb("pqs", did, k)))
        rows.append(nets)
        lines += ["", "2️⃣ Коли:"]
        now = self._now()
        quick = [("+30 хв", "+30"), ("+1 год", "+60"), ("+3 год", "+180")]
        rows.append([(f"⏱ {t}", self.cb("pqt", did, v.replace("+", "p"))) for t, v in quick])
        slots = []
        for hm in self.pq_slots():
            w = parse_when(hm, now)
            if w:
                slots.append((f"🕘 {when_text(w.timestamp(), now)}", self.cb("pqt", did, hm.replace(":", ""))))
        rows += [slots[i:i + 2] for i in range(0, len(slots), 2)]
        rows += [[("⌨️ Свій час", self.cb("pqi", did)), ("🚀 Зараз", self.cb("pqt", did, "now"))],
                 [("🗓 Черга", self.cb("pql")), ("« Новина", self.cb("d", did))]]
        return "\n".join(lines), rows

    def pq_list(self, show_done=False):
        now, q = self._now(), self.pq()
        act = [r for r in q if r["st"] in ("wait", "pause")]
        lines = ["🗓 <b>Черга публікацій</b>", ""]
        rows = []
        if not act:
            lines.append("Порожньо. Запланувати можна зі схваленої новини: «🗓 Запланувати».")
        for r in act[:20]:
            d = self.store.draft(r["did"]) or {"headline": "(видалено)"}
            icon = NETWORKS.get(r["net"], ("•",))[0]
            lines.append(f"{ST[r['st']]} {when_text(r['at'], now)} · {icon} · #{r['did']} {esc(d['headline'][:60])}")
            rows.append([(f"{icon} {when_text(r['at'], now)} #{r['did']}", self.cb("pqr", r["id"])),
                         ("▶️" if r["st"] == "pause" else "⏸", self.cb("pqp", r["id"])), ("🚀", self.cb("pqn", r["id"])),
                         ("✖️", self.cb("pqx", r["id"]))])
        done = [r for r in q if r["st"] not in ("wait", "pause")]
        if done:
            lines += ["", f"Виконано й нагадано: {len(done)}" + ("" if show_done else " — «🕘 Що вже вийшло»")]
            if show_done:
                for r in done[-15:][::-1]:
                    d = self.store.draft(r["did"]) or {"headline": "(видалено)"}
                    lines.append(f"{ST.get(r['st'], '•')} {when_text(r['at'], now)} · {NETWORKS.get(r['net'], ('•',))[0]} · "
                                 f"#{r['did']} {esc(d['headline'][:50])}" + (f" — {esc(r['err'][:80])}" if r.get("err") else ""))
        lines += ["", "⏸ пауза · 🚀 зараз · ✖️ скасувати — для кожного запису окремо, інші не зачіпає."]
        rows.append([("🕘 Що вже вийшло" if not show_done else "🗓 Лише заплановане", self.cb("pql", "" if show_done else "h")),
                     ("🕘 Типові години", self.cb("pqh"))])
        rows.append([("« Новинарня", self.cb())])
        return "\n".join(lines)[:4000], rows

    def pq_hours(self):
        s = self.pq_slots()
        return ("🕘 <b>Типові години публікацій</b>\n\nЦі години пропонуються кнопками при плануванні.\nЗараз: " + ", ".join(s) +
                "\n\nЩоб змінити — «✍️ Задати» і напишіть через кому, напр. <code>07:30, 13:00, 19:30</code>.",
                [[("✍️ Задати", self.cb("pqhi")), ("↩️ Типові", self.cb("pqhr"))], [("« Черга", self.cb("pql"))]])

    # ------------------------------------------------------------ маршрути
    async def pq_route(self, chat, a, x, y):
        if a == "pq":
            return None, self.pq_plan(int(x))
        if a == "pql":
            return None, self.pq_list(x == "h")
        if a == "pqs":
            did = int(x)
            sel = self.store.get(f"pqsel:{did}") or (["tg"] if self.store.get("publish_chat") else [])
            sel = [k for k in sel if k != y] if y in sel else sel + [y]
            self.store.set(f"pqsel:{did}", sel)
            return None, self.pq_plan(did)
        if a == "pqt":
            did, now = int(x), self._now()
            sel = self.store.get(f"pqsel:{did}") or (["tg"] if self.store.get("publish_chat") else [])
            if not sel:
                return "Спершу виберіть, куди", self.pq_plan(did)
            if y == "now":
                when = now
            elif y.startswith("p"):
                when = now + dt.timedelta(minutes=int(y[1:]))
            else:
                when = parse_when(f"{y[:2]}:{y[2:]}", now)
            return self._pq_commit(chat, did, sel, when)
        if a == "pqi":
            self.ask(chat, "pqi", int(x))
            return None, ("⌨️ <b>Коли опублікувати?</b>\n\nНапишіть, наприклад: <code>18:30</code> · <code>29.11 09:00</code> · "
                          "<code>+45</code> (через 45 хв) · <code>+2г</code>.", [[("✖️ Скасувати", "m:cancel")]])
        if a in ("pqp", "pqx", "pqn", "pqr", "pqdone"):
            q = self.pq()
            r = next((r for r in q if r["id"] == int(x)), None)
            if not r:
                return "Запису вже немає", self.pq_list()
            if a == "pqp":
                r["st"] = "wait" if r["st"] == "pause" else "pause"
            elif a == "pqx":
                q.remove(r)
            elif a == "pqn":
                r["at"], r["st"] = time.time() - 1, "wait"
            elif a == "pqdone":
                r["st"], r["err"] = "done", "позначено вручну"
                self.pq_save(q)
                return "✅ Позначено як опубліковане", None
            elif a == "pqr":
                return None, self.xp_screen(r["did"], r["net"]) if r["net"] != "tg" else self.card(r["did"])
            self.pq_save(q)
            if a == "pqn":
                self._bg(self.pq_tick())
            return {"pqp": "Готово", "pqx": "✖️ Скасовано", "pqn": "🚀 Публікую"}[a], self.pq_list()
        if a == "pqh":
            return None, self.pq_hours()
        if a == "pqhi":
            self.ask(chat, "pqh")
            return None, ("🕘 Напишіть години через кому, напр. <code>07:30, 13:00, 19:30</code>.", [[("✖️ Скасувати", "m:cancel")]])
        if a == "pqhr":
            self.store.set("pq_slots", None)
            return None, self.pq_hours()
        return None, self.pq_list()

    def _pq_commit(self, chat, did, sel, when):
        if not when:
            return "Не зрозумів час", self.pq_plan(did)
        self.pq_add(did, sel, when.timestamp(), chat)
        need = [k for k in sel if k != "tg" and k not in self.xp_get(did)]
        if need:  # версії для мереж — заздалегідь, щоб у призначений час усе було готове
            self._bg(self.xp_make(chat, did))
        self.store.set(f"pqsel:{did}", None)
        if when.timestamp() <= time.time() + 30:
            self._bg(self.pq_tick())
        names = ", ".join(NETWORKS[k][1] for k in sel)
        return f"🗓 {when_text(when.timestamp(), self._now())}", (
            f"🗓 <b>Заплановано #{did}</b> на {when_text(when.timestamp(), self._now())}\nКуди: {esc(names)}" +
            ("\n\n✨ Готую версії для мереж — прийдуть окремим повідомленням." if need else ""),
            [[("🗓 Черга", self.cb("pql")), ("« Новина", self.cb("d", did))]])

    async def pq_text(self, chat, key, data, text):
        if key == "pqh":
            hs = []
            for p in re.split(r"[,;\s]+", text):
                m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", p.strip())
                if m and int(m[1]) < 24 and int(m[2]) < 60:
                    hs.append(f"{int(m[1]):02d}:{m[2]}")
            if not hs:
                return "Не знайшов жодної години", self.pq_hours()
            self.store.set("pq_slots", sorted(set(hs))[:8])
            return "Збережено", self.pq_hours()
        did = int(data)
        sel = self.store.get(f"pqsel:{did}") or (["tg"] if self.store.get("publish_chat") else [])
        if not sel:
            return "Спершу виберіть, куди", self.pq_plan(did)
        return self._pq_commit(chat, did, sel, parse_when(text, self._now()))

    # ------------------------------------------------------------ фон
    async def pq_tick(self):
        q, changed = self.pq(), False
        for r in q:
            if r["st"] != "wait" or r["at"] > time.time():
                continue
            changed = True
            d = self.store.draft(r["did"])
            if not d:
                r["st"], r["err"] = "fail", "новину видалено"
                continue
            if r["net"] == "tg":
                if d["status"] == "published":
                    r["st"], r["err"] = "done", "уже було опубліковано"
                    continue
                ok, msg = await self.publish(r["did"])
                r["st"], r["err"] = ("done", "") if ok else ("fail", msg)
                await self.host.notify(r["by"], (f"📤 Опубліковано за розкладом: #{r['did']} {esc(d['headline'][:80])}" if ok
                                                 else f"⚠️ Не вдалося опублікувати за розкладом #{r['did']}: {esc(msg)}"),
                                       [[("📰 Новина", self.cb("d", r["did"])), ("🗓 Черга", self.cb("pql"))]])
            else:
                v = self.xp_get(r["did"]).get(r["net"])
                icon, name = NETWORKS[r["net"]][:2]
                if not v:
                    await self.xp_make(r["by"], r["did"], r["net"])
                    v = self.xp_get(r["did"]).get(r["net"])
                body = (f"🔔 <b>Час публікувати в {icon} {name}</b> · #{r['did']}\n\n<pre>{esc(v['text'])}</pre>" if v else
                        f"🔔 <b>Час публікувати в {icon} {name}</b> · #{r['did']}\n\n⚠️ Версію підготувати не вдалося — відкрийте новину.")
                if v and v.get("slides"):
                    body += "\n\n🖼 Слайди:\n" + "\n".join(f"{i}. {esc(s)}" for i, s in enumerate(v["slides"], 1))
                r["st"] = "remind"
                await self.host.notify(r["by"], body[:4000] + "\n\n👆 Натисніть на текст у рамці — скопіюється.",
                                       [[("✅ Опубліковано", self.cb("pqdone", r["id"])), ("📲 Версії", self.cb("xp", r["did"], r["net"]))]])
        if changed:
            fresh = {r["id"]: r for r in q}
            self.pq_save([fresh.get(r["id"], r) for r in self.pq() if r["id"] in fresh] +
                         [r for r in self.pq() if r["id"] not in fresh])

    async def _pq_loop(self):
        await asyncio.sleep(35)
        while True:
            try:
                if self.on():
                    await self.pq_tick()
                    if self.store.get("al_held") and hasattr(self, "al_morning"):  # ⚡ притримане вночі — після тихих годин
                        from .alerts import in_quiet
                        if not in_quiet(self.al_cfg()["quiet"], self._now()):
                            await self.al_morning()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("publish queue")
            await asyncio.sleep(30)
