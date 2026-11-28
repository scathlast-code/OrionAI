"""🧩 Програмовані кнопки під написаною новиною + ⏰ написання за розкладом."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

from plugins import esc

log = logging.getLogger("orion.plugins.newsroom.actions")

TYPES = {
    "publish": ("📤", "Опублікувати в канал"),
    "bot": ("🤖", "Надіслати дочірньому боту"),
    "chat": ("💬", "Надіслати в чат або канал"),
    "folder": ("📁", "Покласти в теку"),
    "archive": ("🗄", "В архів"),
    "copy": ("📋", "Копіювати текст"),
    "delete": ("🗑", "Видалити"),
}
DEFAULT_ACTIONS = [
    {"id": "a1", "label": "📤 Опублікувати", "type": "publish", "target": ""},
    {"id": "a2", "label": "🤖 На перевірку", "type": "bot", "target": ""},
    {"id": "a3", "label": "🗄 В архів", "type": "archive", "target": ""},
    {"id": "a4", "label": "📁 У теку", "type": "folder", "target": ""},
    {"id": "a5", "label": "📋 Копіювати", "type": "copy", "target": ""},
    {"id": "a6", "label": "🗑 Видалити", "type": "delete", "target": ""},
]
DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


class ActionsUI:
    # ================================================================ дані
    def actions(self):
        a = self.store.get("actions")
        return a if isinstance(a, list) and a else [dict(x) for x in DEFAULT_ACTIONS]

    def save_actions(self, acts):
        self.store.set("actions", acts)

    def folders(self):
        f = self.store.get("folders")
        return f if isinstance(f, list) and f else ["⭐ Важливе", "⏳ На потім"]

    def hub(self):
        p = (self.host.extra.get("plugin") or (lambda pid: None))("hub")
        return p.M if p and p.M else None

    def child_bots(self):
        M = self.hub()
        return [b for b in (M.bots_list() if M else [])]

    def action_rows(self, did):
        acts = self.actions()
        btns = [(a["label"][:30], self.cb("act", did, a["id"])) for a in acts]
        return [btns[i:i + 2] for i in range(0, len(btns), 2)]

    def draft_text(self, d):
        src = ("\n\nДжерела: " + "; ".join(d["sources"][:5])) if d.get("sources") else ""
        return f"{d['headline']}\n\n{d['body']}{src}"

    # ================================================================ виконання
    async def run_action(self, chat, did, aid):
        a = next((x for x in self.actions() if x["id"] == aid), None)
        d = self.store.draft(did)
        if not a or not d:
            return "Кнопка або новина вже не існує", None
        t = a["type"]
        if t == "publish":
            if d["status"] in ("draft",):
                self.store.update_draft(did, keep_version=True, status="approved", decided_by=chat)
            elif d["status"] == "length_mismatch":
                return "Спочатку підженіть довжину (📏 у «⋯ Ще»)", self.card(did)
            ok, msg = await self.publish(did)
            return msg, self.card(did)
        if t == "bot":
            if not a.get("target"):
                return None, self.pick_bot_screen(did, aid)
            return await self.send_to_bot(did, int(a["target"])), self.card(did)
        if t == "chat":
            if not a.get("target"):
                return "У кнопці не вказано чат — ⚙️ → 🧩 Кнопки під новиною", self.card(did)
            bot = self.host.bot()
            try:
                await bot.send_message(int(a["target"]) if str(a["target"]).lstrip("-").isdigit() else a["target"], self.post_html(d)[:4096],
                                       parse_mode="HTML", disable_web_page_preview=True)
                self.note(did, f"💬 надіслано в {a['target']}")
                return "💬 Надіслано", self.card(did)
            except Exception as exc:  # noqa: BLE001
                return f"Не вдалося: {str(exc)[:120]}", self.card(did)
        if t == "folder":
            if not a.get("target"):
                return None, self.pick_folder_screen(did)
            self.store.update_draft(did, keep_version=True, folder=a["target"])
            return f"📁 У теці «{a['target']}»", self.card(did)
        if t == "archive":
            self.store.update_draft(did, keep_version=True, status="archived")
            return "🗄 В архіві", self.card(did)
        if t == "copy":
            bot = self.host.bot()
            if bot:
                await bot.send_message(chat, self.draft_text(d)[:4096])
            return "📋 Надіслав чистим текстом — утримуйте, щоб скопіювати", None
        if t == "delete":
            return None, (f"🗑 <b>Видалити новину #{did}?</b>\n{esc(d['headline'])}", [[("🗑 Так, видалити", self.cb("delok", did)), ("« Ні", self.cb("d", did))]])
        return "Невідома дія", None

    def note(self, did, line):
        d = self.store.draft(did)
        self.store.update_draft(did, keep_version=True, notes=((d["notes"] + "\n") if d["notes"] else "") + line)

    async def send_to_bot(self, did, bid):
        M = self.hub()
        if not M:
            return "🧭 Мої боти недоступні"
        b = M.bot_get(bid)
        if not b:
            return "Такого бота вже немає в «🧭 Мої боти»"
        d = self.store.draft(did)
        eid, why = M.event_add(0, "news", self.draft_text(d), "orion")
        if not eid:
            return f"Не надіслано: {why}"
        ok, detail = await M.deliver(M.event_get(eid), b)
        self.note(did, f"🤖 {'надіслано' if ok else 'НЕ надіслано'} боту «{b['name']}»" + (f": {detail}" if detail else ""))
        return (f"🤖 Надіслано «{b['name']}»" if ok else f"⚠️ {detail[:150]}")

    # ================================================================ екрани вибору
    def pick_bot_screen(self, did, aid=None):
        bots = self.child_bots()
        rows = [[(f"🤖 {b['name'][:28]}", self.cb("tobot", did, b["id"]))] for b in bots]
        rows.append([("🧭 Мої боти", "m:pl:hub"), ("« Новина", self.cb("d", did))])
        text = "🤖 <b>Якому боту надіслати на перевірку?</b>" if bots else ("🤖 У «🧭 Мої боти» ще немає дочірніх ботів. Додайте бота там — "
                                                                       "і він з'явиться тут.")
        return text, rows

    def pick_folder_screen(self, did):
        rows = [[(f"📁 {f}", self.cb("tofld", did, i))] for i, f in enumerate(self.folders())]
        rows.append([("➕ Нова тека", self.cb("fldnew", did)), ("« Новина", self.cb("d", did))])
        return "📁 <b>У яку теку?</b>", rows

    def more_screen(self, did):
        d = self.store.draft(did)
        rows = [[("📏 Довжина", self.cb("len", did)), ("🔎 Звірити", self.cb("chk", did)), ("✍️ Стиль", self.cb("sty", did))],
                [("📄 Оригінал", self.cb("src", did)), (f"🕘 Версії · {d['version']}", self.cb("ver", did))]]
        if d["status"] in ("draft", "length_mismatch"):
            rows.append([("❌ Відхилити", self.cb("no", did))])
        else:
            rows.append([("↩️ Повернути в чернетки", self.cb("re", did))])
        rows.append([("« Новина", self.cb("d", did))])
        return f"⋯ <b>Ще дії для #{did}</b>", rows

    # ================================================================ налаштування кнопок
    def actions_screen(self):
        acts = self.actions()
        lines = ["🧩 <b>Кнопки під новиною</b>", "", "Ці кнопки стоять під кожною написаною новиною — після команди, пульта, розкладу чи пересланого. "
                 "Натискання робить саме те, що тут запрограмовано.", ""]
        rows = []
        for n, a in enumerate(acts):
            icon, what = TYPES.get(a["type"], ("•", a["type"]))
            tgt = self.target_name(a)
            lines.append(f"{n + 1}. {esc(a['label'])} — {what}" + (f": {esc(tgt)}" if tgt else ""))
            rows.append([(f"✏️ {a['label'][:22]}", self.cb("ace", a["id"])), ("⬆️", self.cb("acm", a["id"], "up")), ("🗑", self.cb("acd", a["id"]))])
        rows.append([("➕ Нова кнопка", self.cb("acn")), ("↩️ Типовий набір", self.cb("acr"))])
        rows.append([("« Налаштування новинарні", self.cb("H2"))])
        return "\n".join(lines)[:4000], rows

    def target_name(self, a):
        if a["type"] == "bot":
            if not a.get("target"):
                return "щоразу питати"
            M = self.hub()
            b = M.bot_get(int(a["target"])) if M else None
            return b["name"] if b else "бот видалений"
        if a["type"] == "folder":
            return a.get("target") or "щоразу питати"
        if a["type"] == "chat":
            return a.get("target") or "не вказано"
        if a["type"] == "publish":
            return self.store.get("publish_title") or "канал не підключено"
        return ""

    def action_edit_screen(self, aid):
        a = next((x for x in self.actions() if x["id"] == aid), None)
        if not a:
            return "Кнопку не знайдено.", [[("« Кнопки", self.cb("acs"))]]
        rows = [[(("● " if a["type"] == k else "") + f"Дія: {v[1].lower()}", self.cb("act2", aid, k))] for k, v in TYPES.items()]
        if a["type"] == "bot":
            rows.append([("🎯 Обрати бота", self.cb("actg", aid))])
        if a["type"] == "folder":
            rows.append([("🎯 Обрати теку", self.cb("actg", aid))])
        if a["type"] == "chat":
            rows.append([("🎯 Вказати чат", self.cb("actg", aid))])
        rows.append([("✏️ Підпис кнопки", self.cb("acl", aid)), ("« Кнопки", self.cb("acs"))])
        return (f"✏️ <b>{esc(a['label'])}</b>\nЩо робить: {TYPES[a['type']][1]}" + (f" → {esc(self.target_name(a))}" if self.target_name(a) else "")
                + "\n\nОберіть дію:"), rows

    def action_target_screen(self, aid):
        a = next((x for x in self.actions() if x["id"] == aid), None)
        if not a:  # кнопку вже видалили, а повідомлення зі старою клавіатурою лишилось
            return "Цієї кнопки вже немає.", [[("« Кнопки", self.cb("acs"))]]
        if a["type"] == "bot":
            rows = [[("❔ Щоразу питати", self.cb("actt", aid, "-"))]] + [[(f"🤖 {b['name'][:28]}", self.cb("actt", aid, b["id"]))] for b in self.child_bots()]
            return "🎯 <b>Якому боту надсилати?</b>", rows + [[("« Кнопка", self.cb("ace", aid))]]
        if a["type"] == "folder":
            rows = [[("❔ Щоразу питати", self.cb("actt", aid, "-"))]] + [[(f"📁 {f}", self.cb("actt", aid, i))] for i, f in enumerate(self.folders())]
            return "🎯 <b>Яка тека?</b>", rows + [[("« Кнопка", self.cb("ace", aid))]]
        return None

    # ================================================================ розклад
    def schedules(self):
        s = self.store.get("schedules")
        return s if isinstance(s, list) else []

    def sched_screen(self, key):
        mine = [s for s in self.schedules() if s["key"] == key]
        lines = ["⏰ <b>За розкладом</b>", "", "Бот сам знайде й напише в заданий час; новина прийде з вашими кнопками.", ""]
        rows = []
        for s in mine:
            lines.append(f"• {s['time']} · {', '.join(DAYS[d] for d in s['days']) if len(s['days']) < 7 else 'щодня'} · {s['size']}")
            rows.append([(f"🗑 {s['time']} {s['size']}", self.cb("sdel", s["id"]))])
        if not mine:
            lines.append("Поки немає.")
        rows.append([("➕ Додати час", self.cb("sadd", key))])
        rows.append([("« Тема", self.cb("Dm", key))])
        return "\n".join(lines), rows

    async def _sched_loop(self):
        __import__("notify").KIND.set("news")  # 🔔 тип сповіщень для режимів людини
        await asyncio.sleep(40)
        done = set()
        while True:
            try:
                if self.on():
                    tz = self.host.tz
                    import datetime as _dt
                    now = _dt.datetime.now(tz) if tz else _dt.datetime.now()
                    hm, wd = now.strftime("%H:%M"), now.weekday()
                    for s in self.schedules():
                        k = (s["id"], now.strftime("%Y-%m-%d %H:%M"))
                        if s["time"] == hm and wd in s["days"] and k not in done:
                            done.add(k)
                            for owner in self.host.admins()[:1]:
                                self._bg(self.run_mode(owner, s["key"], s["size"]))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("schedule loop")
            await asyncio.sleep(30)

    # ================================================================ маршрути
    async def actions_route(self, chat, a, x, y):
        if a == "act":
            return await self.run_action(chat, int(x), y)
        if a == "more":
            return None, self.more_screen(int(x))
        if a == "tobot":
            return await self.send_to_bot(int(x), int(y)), self.card(int(x))
        if a == "tofld":
            f = self.folders()[int(y)]
            self.store.update_draft(int(x), keep_version=True, folder=f)
            return f"📁 У теці «{f}»", self.card(int(x))
        if a == "fldnew":
            self.ask(chat, "fldnew", int(x))
            return None, ("📁 Назва нової теки:", [[("✖️ Скасувати", "m:cancel")]])
        if a == "delok":
            with self.store.db() as con:
                con.execute("DELETE FROM drafts WHERE id=?", (int(x),))
            return "🗑 Видалено", self.list_screen("new", 0)
        if a == "acs":
            return None, self.actions_screen()
        if a == "ace":
            return None, self.action_edit_screen(x)
        if a == "act2":
            acts = self.actions()
            for it in acts:
                if it["id"] == x:
                    it["type"], it["target"] = y, ""
                    if it["label"].split(" ")[0] in [v[0] for v in TYPES.values()]:
                        it["label"] = TYPES[y][0] + " " + it["label"].split(" ", 1)[-1]
            self.save_actions(acts)
            return None, self.action_edit_screen(x)
        if a == "actg":
            act = next((it for it in self.actions() if it["id"] == x), None)
            if act and act["type"] == "chat":
                self.ask(chat, "actchat", x)
                return None, ("💬 Вкажіть чат або канал: <code>@username</code> або числовий ID (<code>-100…</code>). Бот має бути там учасником з правом писати.",
                              [[("✖️ Скасувати", "m:cancel")]])
            return None, self.action_target_screen(x)
        if a == "actt":
            acts = self.actions()
            for it in acts:
                if it["id"] == x:
                    if y == "-":
                        it["target"] = ""
                    elif it["type"] == "folder":
                        it["target"] = self.folders()[int(y)]
                    else:
                        it["target"] = str(y)
            self.save_actions(acts)
            return "Збережено", self.action_edit_screen(x)
        if a == "acl":
            self.ask(chat, "aclabel", x)
            return None, ("✏️ Новий підпис кнопки (з емодзі на початку), наприклад «🌿 У GreenPost-бот»:", [[("✖️ Скасувати", "m:cancel")]])
        if a == "acm":
            acts = self.actions()
            i = next((n for n, it in enumerate(acts) if it["id"] == x), 0)
            if i > 0:
                acts[i - 1], acts[i] = acts[i], acts[i - 1]
            self.save_actions(acts)
            return None, self.actions_screen()
        if a == "acd":
            self.save_actions([it for it in self.actions() if it["id"] != x] or [dict(DEFAULT_ACTIONS[0])])
            return "Кнопку прибрано", self.actions_screen()
        if a == "acn":
            acts = self.actions()
            acts.append({"id": "a" + secrets.token_hex(3), "label": "🤖 Новому боту", "type": "bot", "target": ""})
            self.save_actions(acts)
            return None, self.action_edit_screen(acts[-1]["id"])
        if a == "acr":
            self.save_actions([dict(x) for x in DEFAULT_ACTIONS])
            return "Типовий набір", self.actions_screen()
        if a == "sch":
            return None, self.sched_screen(x)
        if a == "sadd":
            self.ask(chat, "sadd", x)
            return None, ("⏰ Час і розмір, наприклад:\n<code>08:30 коротко</code> — щодня\n<code>19:00 новина пн-пт</code>\n<code>10:00 велика сб,нд</code>",
                          [[("✖️ Скасувати", "m:cancel")]])
        if a == "sdel":
            self.store.set("schedules", [s for s in self.schedules() if s["id"] != x])
            return "Прибрано", None
        if a == "fld":
            ds = self.store.drafts(None, limit=40)
            ds = [d for d in ds if d.get("folder") == self.folders()[int(x)]]
            rows = [[(f"#{d['id']} {d['headline'][:34]}", self.cb("d", d["id"]))] for d in ds[:10]] + [[("« Теки", self.cb("flds"))]]
            return None, (f"📁 <b>{esc(self.folders()[int(x)])}</b>" + ("" if ds else "\n\nПорожньо."), rows)
        if a == "flds":
            rows = [[(f"📁 {f}", self.cb("fld", i))] for i, f in enumerate(self.folders())]
            rows.append([("🗄 Архів новин", self.cb("ls", "arc")), ("« Архів", self.cb("H5"))])
            return None, ("📁 <b>Теки новин</b>", rows)
        return None, None

    async def actions_text(self, chat, key, data, text):
        t = text.strip()
        if key == "fldnew":
            fl = self.folders() + [t[:40]]
            self.store.set("folders", fl)
            self.store.update_draft(int(data), keep_version=True, folder=t[:40])
            return f"📁 У теці «{t[:40]}»", self.card(int(data))
        if key == "actchat":
            acts = self.actions()
            for it in acts:
                if it["id"] == data:
                    it["target"] = t.split()[0][:64]
            self.save_actions(acts)
            return "Збережено", self.action_edit_screen(data)
        if key == "aclabel":
            acts = self.actions()
            for it in acts:
                if it["id"] == data:
                    it["label"] = t[:30]
            self.save_actions(acts)
            return "Збережено", self.action_edit_screen(data)
        if key == "sadd":
            import re
            m = re.match(r"(\d{1,2})[:.](\d{2})\s*(коротко|новина|велика)?\s*(.*)", t.lower())
            if not m:
                return "Формат: 08:30 коротко пн-пт", self.sched_screen(data)
            hm = f"{int(m.group(1)):02d}:{m.group(2)}"
            size = {"коротко": "short", "новина": "news", "велика": "big"}.get(m.group(3) or "новина")
            days = list(range(7))
            rest = m.group(4).strip()
            if rest:
                rng = re.match(r"(\w\w)\s*-\s*(\w\w)", rest)
                if rng and rng.group(1) in DAYS and rng.group(2) in DAYS:
                    a_, b_ = DAYS.index(rng.group(1)), DAYS.index(rng.group(2))
                    days = list(range(a_, b_ + 1))
                else:
                    days = [DAYS.index(x) for x in re.split(r"[,\s]+", rest) if x in DAYS] or days
            s = self.schedules() + [{"id": "s" + secrets.token_hex(3), "key": data, "size": size, "time": hm, "days": days}]
            self.store.set("schedules", s)
            return "⏰ Додано", self.sched_screen(data)
        return None, None
