"""🧾 Зведення: режим збору (бот лише відповідає «✅ Прийнято») і шаблонні кнопки «Готуй зведення»."""
from __future__ import annotations

import io
import json
import logging
import secrets
import time

from plugins import esc
from .engine import JSON_SPEC, clean, system_prompt

log = logging.getLogger("orion.plugins.newsroom.digest")

SCHEMA = """
CREATE TABLE IF NOT EXISTS collect (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'text',
    text TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', used INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS collect_chat ON collect(chat, used);
"""
DEFAULT_TEMPLATES = [
    {"id": "t1", "label": "🧾 Зведення 2200", "length": 2200, "dest": "review", "clear": True,
     "ideas": "Повне зведення новин за областями, Київ першим; загиблих і поранених не рахувати двічі; непідтверджене позначати."},
    {"id": "t2", "label": "🧾 Зведення 2000", "length": 2000, "dest": "review", "clear": True,
     "ideas": "Зведення новин за областями, Київ першим; загиблих і поранених не рахувати двічі; непідтверджене позначати."},
    {"id": "t3", "label": "🧾 Зведення 1000", "length": 1000, "dest": "review", "clear": True,
     "ideas": "Коротке зведення: лише головне, кожна подія — одне речення, найважливіше першим."},
]


DEFAULT_SCENARIOS = [
    {"id": "s1", "label": "📍 За областями", "prompt": "Структура за областями: кожна область — окремий абзац, що починається її назвою й двокрапкою; Київ і Київська область першими."},
    {"id": "s2", "label": "🔥 Головне зверху", "prompt": "Перший абзац — найважливіша подія з наслідками; далі за спаданням важливості; дрібне — одним реченням наприкінці."},
    {"id": "s3", "label": "⏱ Хронологія", "prompt": "Хронологічний порядок подій із часом там, де він відомий; без повторів; наприкінці — короткий підсумок."},
    {"id": "s4", "label": "📊 Цифри й факти", "prompt": "Наголос на цифрах, датах, сумах, кількостях; кожна цифра з джерелом; суперечливі цифри — поруч із поясненням."},
    {"id": "s5", "label": "🗣 Для соцмереж", "prompt": "Живо й коротко для соцмереж: чіпкий перший рядок, короткі абзаци, без канцеляриту, 2–3 хештеги в кінці."},
    {"id": "s6", "label": "🧾 Сухий звіт", "prompt": "Нейтральний інформаційний звіт: лише факти, без оцінок і прикметників, пунктами там, де можна."},
]


def digest_prompt(items, tpl, scenario=""):
    lines = []
    for n, it in enumerate(items, 1):
        head = f"[{n}]" + (f" {it['source']}" if it["source"] else "") + (f" {it['url']}" if it["url"] else "")
        lines.append(head + "\n" + (it["text"] or "")[:3000])
    ln = int(tpl.get("length") or 0)
    return ("Склади ОДНУ статтю-зведення з усіх матеріалів нижче. Кожен факт — лише з матеріалів, з атрибуцією; повтори об'єднуй, "
            "суперечності називай; у sources — джерела й посилання матеріалів; у unconfirmed — непідтверджене; у notes — чого бракує.\n"
            f"Ідеї й вказівки редактора: {tpl.get('ideas') or '—'}\n"
            + (f"Сценарій (пріоритетний): {scenario}\n" if scenario else "")
            + (f"Довжина body — РІВНО {ln} символів разом із пробілами й переносами.\n" if ln else "")
            + "\nМАТЕРІАЛИ:\n" + "\n\n".join(lines)[:22000] + "\n\n" + JSON_SPEC)


class DigestMode:
    def _dinit(self):
        if getattr(self, "_digest_ready", False):
            return
        with self.store.db() as con:
            con.executescript(SCHEMA)
        self._digest_ready = True

    # ----- стан
    def collecting(self, chat):
        return chat in (self.store.get("collecting") or [])

    def set_collecting(self, chat, on):
        lst = [c for c in (self.store.get("collecting") or []) if c != chat] + ([chat] if on else [])
        self.store.set("collecting", lst)

    def collected(self, chat):
        self._dinit()
        with self.store.db() as con:
            con.row_factory = __import__("sqlite3").Row
            return [dict(r) for r in con.execute("SELECT * FROM collect WHERE chat=? AND used=0 ORDER BY id", (chat,))]

    def add_collected(self, chat, kind, text, source="", url=""):
        self._dinit()
        with self.store.db() as con:
            con.execute("INSERT INTO collect(chat,at,kind,text,source,url) VALUES(?,?,?,?,?,?)", (chat, time.time(), kind, text[:20000], source[:200], url[:500]))
            return con.execute("SELECT COUNT(*) FROM collect WHERE chat=? AND used=0", (chat,)).fetchone()[0]

    def clear_collected(self, chat, used_only=False):
        self._dinit()
        with self.store.db() as con:
            con.execute("DELETE FROM collect WHERE chat=?" + (" AND used=1" if used_only else ""), (chat,))

    def scenarios(self):
        s = self.store.get("digest_scenarios")
        return s if isinstance(s, list) and s else [dict(x) for x in DEFAULT_SCENARIOS]

    def scenario_pick_screen(self, tid):
        t = next((x for x in self.templates() if x["id"] == tid), None)
        n = len(self.collected((self.host.admins() or [0])[0]))
        rows = [[(sc["label"][:30], self.cb("csg", tid, sc["id"]))] for sc in self.scenarios()]
        rows = [sum(rows[i:i + 2], []) for i in range(0, len(rows), 2)]
        rows.append([("✍️ Свій промт", self.cb("csp", tid)), ("▶️ Без сценарію", self.cb("csg", tid, "-"))])
        rows.append([("🎭 Редагувати сценарії", self.cb("csl")), ("« Зведення", self.cb("dg"))])
        return (f"🎭 <b>{esc(t['label'] if t else 'Зведення')}</b> · матеріалів {n}\n\nОберіть сценарій — він підкаже ШІ, як скласти статтю "
                "(доповнює ідеї шаблону):\n\n" + "\n".join(f"{sc['label']} — <i>{esc(sc['prompt'][:90])}</i>" for sc in self.scenarios())), rows

    def scenarios_screen(self):
        lines = ["🎭 <b>Сценарії зведень</b>", "", "Готові промти, які бот пропонує обрати перед кожним «Готуй: …»."]
        rows = []
        for sc in self.scenarios():
            lines.append(f"\n<b>{esc(sc['label'])}</b>\n<i>{esc(sc['prompt'][:220])}</i>")
            rows.append([(f"✏️ {sc['label'][:24]}", self.cb("cse", sc["id"])), ("🗑", self.cb("csd", sc["id"]))])
        rows.append([("➕ Новий сценарій", self.cb("csn")), ("« Шаблони зведень", self.cb("ctp"))])
        return "\n".join(lines)[:4000], rows

    def templates(self):
        t = self.store.get("digest_templates")
        return t if isinstance(t, list) and t else [dict(x) for x in DEFAULT_TEMPLATES]

    # ----- прийом у режимі збору
    async def collect_message(self, msg):
        chat = msg.chat_id
        origin, name, url = self._origin(msg)
        text = msg.text or msg.caption or ""
        links = self._links(msg)
        if links:
            text += "\nПосилання: " + " ".join(links)
        kind = "photo" if msg.photo else ("forward" if origin == "forward" else "text")
        if msg.photo and not text:
            text = "[фото без підпису]"
        n = self.add_collected(chat, kind, text, name, url)
        await msg.reply_text(f"✅ Прийнято · {n}")

    async def collect_document(self, msg):
        doc = msg.document
        chat = msg.chat_id
        name = doc.file_name or "файл"
        text = ""
        if (doc.file_size or 0) <= 2 * 1024 * 1024 and name.lower().endswith((".txt", ".md", ".csv")):
            raw = bytes(await (await doc.get_file()).download_as_bytearray())
            for enc in ("utf-8-sig", "cp1251"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        n = self.add_collected(chat, "file", (msg.caption or "") + ("\n" if msg.caption else "") + (text or f"[файл {name} — текст не витягнуто]"), name)
        await msg.reply_text(f"✅ Прийнято · {n}")

    # ----- екрани
    def digest_screen(self, chat=None):
        chat = chat or (self.host.admins() or [0])[0]
        items = self.collected(chat)
        on = self.collecting(chat)
        text = ("🧾 <b>Зведення</b>\n\n"
                + ("🟢 <b>Збір увімкнено</b> — усе, що надсилаєте, складається сюди без обробки; бот відповідає лише «✅ Прийнято».\n"
                   if on else "Увімкніть збір і закидайте новини й тексти — потім одна шаблонна кнопка напише статтю.\n")
                + f"\nЗібрано: <b>{len(items)}</b>"
                + (f" · останнє {time.strftime('%H:%M', time.localtime(items[-1]['at']))}" if items else ""))
        rows = [[(("⏹ Зупинити збір" if on else "▶️ Почати збір"), self.cb("cgo")), ("📋 Що зібрано", self.cb("cls"))]]
        tb = [(f"Готуй: {t['label']}"[:40], self.cb("cmk", t["id"])) for t in self.templates()]
        rows += [tb[i:i + 2] for i in range(0, len(tb), 2)]
        rows.append([("➕ Новини ньюзруму за добу на тему…", self.cb("cday")), ("🗑 Очистити зібране", self.cb("cclr"))])
        rows.append([("⚙️ Шаблони зведень", self.cb("ctp")), ("« Знайти й написати", self.cb("D"))])
        return text, rows

    def collected_screen(self, chat):
        items = self.collected(chat)
        icon = {"text": "💬", "forward": "↪️", "photo": "🖼", "file": "📎"}
        lines = [f"📋 <b>Зібрано: {len(items)}</b>", ""]
        for n, it in enumerate(items[-25:], max(1, len(items) - 24)):
            lines.append(f"{n}. {icon.get(it['kind'], '•')} " + (f"<i>{esc(it['source'][:30])}</i> — " if it["source"] else "") + esc(it["text"][:90].replace("\n", " ")))
        if not items:
            lines.append("Порожньо.")
        rows = [[("🗑 Прибрати останнє", self.cb("cpop")), ("« Зведення", self.cb("dg"))]]
        return "\n".join(lines)[:4000], rows

    def templates_screen_d(self):
        lines = ["⚙️ <b>Шаблони зведень</b>", "", "Кожен шаблон — кнопка «Готуй: …» з вашими ідеями, обсягом і адресатом."]
        rows = []
        for t in self.templates():
            dest = self.dest_name(t)
            lines.append(f"\n<b>{esc(t['label'])}</b> · {t.get('length') or 'вільна'} симв. · → {esc(dest)}"
                         + (" · очищати після" if t.get("clear") else "") + f"\n<i>{esc(t.get('ideas', '')[:200])}</i>")
            rows.append([(f"✏️ {t['label'][:26]}", self.cb("cte", t["id"])), ("🗑", self.cb("ctd", t["id"]))])
        rows.append([("➕ Новий шаблон", self.cb("ctn")), ("🎭 Сценарії", self.cb("csl"))])
        rows.append([("« Зведення", self.cb("dg"))])
        return "\n".join(lines)[:4000], rows

    def dest_name(self, t):
        d = t.get("dest") or "review"
        if d == "review":
            return "мені на перегляд"
        if d == "channel":
            return "одразу в канал " + (self.store.get("publish_title") or "(не підключено)")
        if d.startswith("bot:"):
            M = self.hub()
            b = M.bot_get(int(d[4:])) if M else None
            return f"дочірньому боту «{b['name']}»" if b else "бот видалений"
        return d

    def template_edit_screen(self, tid):
        t = next((x for x in self.templates() if x["id"] == tid), None)
        if not t:
            return "Шаблон не знайдено.", [[("« Шаблони", self.cb("ctp"))]]
        rows = [[("✏️ Назва", self.cb("ctl", tid)), ("💡 Ідеї й вказівки", self.cb("cti", tid))],
                [(f"📏 Обсяг: {t.get('length') or 'вільна'}", self.cb("ctz", tid)), (("🧹 Очищати: так" if t.get("clear") else "🧹 Очищати: ні"), self.cb("ctc", tid))],
                [(("● " if t.get("dest", "review") == "review" else "") + "👀 Мені на перегляд", self.cb("ctx", tid, "review"))],
                [(("● " if t.get("dest") == "channel" else "") + "📤 Одразу в канал", self.cb("ctx", tid, "channel"))]]
        for b in self.child_bots()[:8]:
            rows.append([(("● " if t.get("dest") == f"bot:{b['id']}" else "") + f"🤖 {b['name'][:26]}", self.cb("ctx", tid, f"bot:{b['id']}"))])
        rows.append([("« Шаблони", self.cb("ctp"))])
        return (f"✏️ <b>{esc(t['label'])}</b>\nАдресат: {esc(self.dest_name(t))}\n\n💡 {esc(t.get('ideas', ''))}"), rows

    def _save_tpl(self, tid, **kw):
        ts = self.templates()
        for t in ts:
            if t["id"] == tid:
                t.update(kw)
        self.store.set("digest_templates", ts)

    # ----- написання
    async def make_from_collected(self, chat, tid, scenario=""):
        t = next((x for x in self.templates() if x["id"] == tid), None)
        items = self.collected(chat)
        if not t:
            return await self.host.notify(chat, "Шаблон не знайдено.")
        if not items:
            return await self.host.notify(chat, "🧾 Нічого не зібрано. Увімкніть збір і надішліть новини.", [[("🧾 Зведення", self.cb("dg"))]])
        try:
            data, model = await self.engine._json(digest_prompt(items, t, scenario), system_prompt(self.store.get("rules") or ""), "🧾 Зведення")
            data = clean(data)
            ok = True
            if t.get("length"):
                data, ok, _ = await self.engine.fit_length(data, int(t["length"]), system_prompt(self.store.get("rules") or ""), "🧾 Зведення")
        except Exception as exc:  # noqa: BLE001
            return await self.host.notify(chat, f"⚠️ Зведення не вдалося: {esc(exc)[:300]}")
        for it in items:
            if it["url"] and it["url"] not in data["sources"]:
                data["sources"].append(it["url"])
        data["notes"] = (f"🧾 {t['label']} · з {len(items)} матеріалів. " + data.get("notes", "")).strip()
        did = self.store.add_draft(None, data, kind="digest", model=model, target=int(t.get("length") or 0),
                                   status="draft" if ok else "length_mismatch")
        self._autocheck(did)  # 🔎 кожна чернетка перевіряється одразу: факти й мова
        bundle = self.bundle_text(items)
        dest = t.get("dest") or "review"
        if dest.startswith("bot:"):
            msg = await self.send_bundle_to_bot(did, int(dest[4:]), bundle)
            await self.host.notify(chat, f"🧾 <b>{esc(t['label'])}</b> готове й відправлене: {esc(msg)}", [[("👀 Переглянути", self.cb("d", did))]])
        elif dest == "channel" and ok:
            self.store.update_draft(did, keep_version=True, status="approved", decided_by=chat)
            _ok, msg = await self.publish(did)
            await self.host.notify(chat, f"🧾 <b>{esc(t['label'])}</b>: {esc(msg)}", [[("👀 Переглянути", self.cb("d", did))]])
        else:
            await self.host.notify(chat, *self.card(did))
            bot = self.host.bot()
            if bot:
                from telegram import InputFile
                await bot.send_document(chat, InputFile(io.BytesIO(bundle.encode("utf-8")), filename=f"zibrani_materialy_{did}.txt"),
                                        caption=f"📎 Зібрані матеріали для «{t['label']}» · {len(items)}")
        self._mark_used(chat, [it["id"] for it in items])
        if t.get("clear"):
            self.clear_collected(chat, used_only=True)

    def bundle_text(self, items):
        out = [f"Зібрані матеріали · {len(items)} · {time.strftime('%d.%m.%Y %H:%M')}", ""]
        for n, it in enumerate(items, 1):
            out.append(f"[{n}] {time.strftime('%d.%m %H:%M', time.localtime(it['at']))}" + (f" · {it['source']}" if it["source"] else "")
                       + (f" · {it['url']}" if it["url"] else ""))
            out.append(it["text"])
            out.append("")
        return "\n".join(out)

    async def send_bundle_to_bot(self, did, bid, bundle):
        res = await self.send_to_bot(did, bid)
        M = self.hub()
        if M and not res.startswith(("⚠️", "Не")):
            b = M.bot_get(bid)
            for i in range(0, min(len(bundle), 16000), 3800):  # матеріали — кількома подіями, щоб не обрізались
                eid, _why = M.event_add(0, "news_materials", bundle[i:i + 3800], "orion")
                if eid:
                    await M.deliver(M.event_get(eid), b)
        return res

    def _mark_used(self, chat, ids):
        with self.store.db() as con:
            con.executemany("UPDATE collect SET used=1 WHERE id=?", [(i,) for i in ids])

    # ----- маршрути
    async def digest_route(self, chat, a, x, y):
        if a == "dg":
            return None, self.digest_screen(chat)
        if a == "cgo":
            on = not self.collecting(chat)
            self.set_collecting(chat, on)
            return ("🟢 Збір увімкнено — надсилайте" if on else "⏹ Збір зупинено"), self.digest_screen(chat)
        if a == "cls":
            return None, self.collected_screen(chat)
        if a == "cpop":
            items = self.collected(chat)
            if items:
                with self.store.db() as con:
                    con.execute("DELETE FROM collect WHERE id=?", (items[-1]["id"],))
            return "Прибрано", self.collected_screen(chat)
        if a == "cclr":
            self.clear_collected(chat)
            return "🗑 Очищено", self.digest_screen(chat)
        if a == "cday":
            rows = [[("➕ " + t["title"], self.cb("cdt", t["id"]))] for t in self.desk.topics()]
            rows = [sum(rows[i:i + 2], []) for i in range(0, len(rows), 2)]
            rows.append([("➕ 🗂 Усі теми", self.cb("cdt", 0)), ("« Зведення", self.cb("dg"))])
            return None, ("➕ <b>Новини ньюзруму за добу</b>\n\nНа яку тему додати до зібраного? Бот візьме чернетки, схвалені й опубліковані "
                          "новини за 24 години, що стосуються теми (за ключовими словами теми й категорією).\nТеми й слова — ✍️ Як писати → 🗂 Теми пошуку.", rows)
        if a == "cdt":
            day = time.time() - 86400
            topic = next((t for t in self.desk.topics() if t["id"] == int(x)), None) if x and x != "0" else None
            have = {it["text"][:80] for it in self.collected(chat)}
            n = 0
            for d in self.store.drafts(("approved", "published", "draft"), since=day, limit=200):
                if d["kind"] != "news":
                    continue
                text = d["headline"] + "\n" + d["body"]
                if topic and not self.desk.matches_topic(topic, text, d.get("category", "")):
                    continue
                if text[:80] in have:
                    continue
                self.add_collected(chat, "text", text, "ньюзрум #" + str(d["id"]), (d["sources"] or [""])[0])
                n += 1
            name = topic["title"] if topic else "усі теми"
            return (f"➕ {name}: додано {n}" if n else f"За добу немає новин на тему «{name}»"), self.digest_screen(chat)
        if a == "cmk":
            if not self.collected(chat):
                return "Нічого не зібрано", self.digest_screen(chat)
            return None, self.scenario_pick_screen(x)
        if a == "csg":
            sc = next((s_ for s_ in self.scenarios() if s_["id"] == y), None)
            self._bg(self.make_from_collected(chat, x, sc["prompt"] if sc else ""))
            return "🧾 Готую…", ("🧾 <b>Готую зведення</b>" + (f" · {esc(sc['label'])}" if sc else "")
                                + " — стаття й матеріали прийдуть окремо (або підуть адресату з шаблону).", [[("🧾 Зведення", self.cb("dg"))]])
        if a == "csp":
            self.ask(chat, "csp", x)
            return None, ("✍️ Напишіть свій промт для цього зведення — як скласти, на чому наголосити:", [[("✖️ Скасувати", "m:cancel")]])
        if a == "csl":
            return None, self.scenarios_screen()
        if a == "csd":
            self.store.set("digest_scenarios", [c for c in self.scenarios() if c["id"] != x] or [dict(DEFAULT_SCENARIOS[0])])
            return "Прибрано", self.scenarios_screen()
        if a == "csn":
            self.ask(chat, "csn")
            return None, ("➕ Новий сценарій: <code>емодзі Назва | промт</code>\nНаприклад: <code>🌿 Еко-фокус | Наголос на впливі на довкілля й тварин, що можуть зробити читачі.</code>", [[("✖️ Скасувати", "m:cancel")]])
        if a == "cse":
            self.ask(chat, "cse", x)
            sc = next((c for c in self.scenarios() if c["id"] == x), {})
            return None, (f"✏️ Новий текст сценарію «{esc(sc.get('label', ''))}» (або <code>Назва | промт</code>). Зараз:\n<i>{esc(sc.get('prompt', ''))}</i>", [[("✖️ Скасувати", "m:cancel")]])
        if a == "ctp":
            return None, self.templates_screen_d()
        if a == "cte":
            return None, self.template_edit_screen(x)
        if a == "ctd":
            self.store.set("digest_templates", [t for t in self.templates() if t["id"] != x] or [dict(DEFAULT_TEMPLATES[0])])
            return "Прибрано", self.templates_screen_d()
        if a == "ctn":
            ts = self.templates() + [{"id": "t" + secrets.token_hex(3), "label": "🧾 Новий шаблон", "length": 2000, "dest": "review", "clear": True, "ideas": ""}]
            self.store.set("digest_templates", ts)
            return None, self.template_edit_screen(ts[-1]["id"])
        if a == "ctx":
            self._save_tpl(x, dest=y)
            return None, self.template_edit_screen(x)
        if a == "ctc":
            t = next((t for t in self.templates() if t["id"] == x), {})
            self._save_tpl(x, clear=not t.get("clear"))
            return None, self.template_edit_screen(x)
        if a in ("ctl", "cti", "ctz"):
            self.ask(chat, a, x)
            q = {"ctl": "✏️ Назва кнопки (з емодзі), наприклад «🌿 Еко-тиждень»:",
                 "cti": "💡 Ідеї й вказівки для цього зведення: структура, тон, на чому наголосити, що пропускати:",
                 "ctz": "📏 Обсяг у символах (0 — вільний):"}[a]
            return None, (q, [[("✖️ Скасувати", "m:cancel")]])
        return None, None

    async def digest_text(self, chat, key, data, text):
        t = text.strip()
        if key == "csp":
            self._bg(self.make_from_collected(chat, data, t[:2000]))
            return "🧾 Готую…", ("🧾 <b>Готую зведення за вашим промтом</b> — результат прийде окремо.", [[("🧾 Зведення", self.cb("dg"))]])
        if key in ("csn", "cse"):
            label, sep, prompt = t.partition("|")
            scs = self.scenarios()
            if key == "csn":
                if not sep:
                    return "Формат: Назва | промт", self.scenarios_screen()
                scs.append({"id": "s" + secrets.token_hex(3), "label": label.strip()[:30], "prompt": prompt.strip()[:2000]})
            else:
                for c in scs:
                    if c["id"] == data:
                        if sep:
                            c["label"], c["prompt"] = label.strip()[:30], prompt.strip()[:2000]
                        else:
                            c["prompt"] = t[:2000]
            self.store.set("digest_scenarios", scs)
            return "Збережено", self.scenarios_screen()
        if key == "ctl":
            self._save_tpl(data, label=t[:40])
        elif key == "cti":
            self._save_tpl(data, ideas=t[:2000])
        elif key == "ctz":
            self._save_tpl(data, length=int(t) if t.isdigit() else 0)
        return "Збережено", self.template_edit_screen(data)
