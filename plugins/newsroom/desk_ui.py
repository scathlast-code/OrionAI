"""🧭 Пульт новинарні: тематичні кнопки, теги, знахідки з діями, історії, наші ресурси, блокування, антиспам, шаблони."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time

from plugins import esc
from .desk import (BLOCK_KINDS, FOLDERS, MODES, REL, SIZES, TEMPLATES, angles_prompt, article_blocks, compose_prompt, docx_bytes,
                   domain, google_news_url, parse_google_news, parse_websearch, priority, relation_prompt, score_prompt, source_search_queries)
from .engine import clean, system_prompt


class Progress:
    """Одне повідомлення, що оновлюється: що зараз робиться, крок N з M і скільки секунд минуло."""

    def __init__(self, host, chat, title):
        self.host, self.chat, self.title = host, chat, title
        self.msg, self.stage, self.t0, self.task = None, "", time.time(), None

    def _text(self, extra=""):
        sec = int(time.time() - self.t0)
        return (f"⏳ <b>{esc(self.title)}</b>\n{self.stage} · ⏱ {sec} с"
                + "\n<i>Пошук зазвичай 5–20 с, написання ШІ — 10–45 с.</i>" + extra)

    async def start(self, stage):
        self.stage = stage
        self.msg = await self.host.notify(self.chat, self._text())
        self.task = asyncio.get_running_loop().create_task(self._tick())
        return self

    async def _tick(self):
        try:
            while True:
                await asyncio.sleep(4)
                await self._edit(self._text())
        except asyncio.CancelledError:
            pass

    async def _edit(self, text, rows=None):
        bot = self.host.bot()
        if not (bot and self.msg):
            return
        markup = None
        if rows:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in r] for r in rows])
        try:
            await bot.edit_message_text(text[:4096], chat_id=self.chat, message_id=self.msg.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:  # noqa: BLE001 — «не змінилось» тощо
            pass

    async def set(self, stage):
        self.stage = stage
        await self._edit(self._text())

    async def done(self, text, rows=None):
        if self.task:
            self.task.cancel()
        sec = int(time.time() - self.t0)
        await self._edit(f"{text} · ⏱ {sec} с", rows)
from .feeds import fetch

log = logging.getLogger("orion.plugins.newsroom.desk")
TAG_IVS = [(1800, "30 хв"), (3600, "1 год"), (10800, "3 год"), (21600, "6 год"), (86400, "доба")]
SORTS = [("prio", "⭐ Важливість"), ("time", "⏱ Час"), ("source", "📡 Джерело")]


def short(s, n=40):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def age(ts):
    if not ts:
        return "час невідомий"
    m = int((time.time() - ts) // 60)
    return "щойно" if m < 1 else f"{m} хв тому" if m < 60 else f"{m // 60} год тому" if m < 2880 else time.strftime("%d.%m.%Y", time.localtime(ts))


def prio_mark(p):
    return "🔴" if p >= 8 else "🟠" if p >= 5.5 else "🟡" if p >= 3 else "⚪️"


class DeskUI:
    # ================================================================ пошук і оцінка
    def media_topics(self):
        return self.store.get("media_topics") or ", ".join(t["title"].split(" ", 1)[-1] for t in self.desk.topics())

    async def web_candidates(self, queries, hours=24, per_query=12):
        seen, out = set(), []
        for q in queries:
            got = []
            try:
                got = parse_google_news(await fetch(google_news_url(q, hours)))[:per_query]
            except Exception as exc:  # noqa: BLE001
                log.info("google news failed for %s: %s", q, exc)
            if not got and self.host.search:
                try:
                    text, _err = await self.host.search(q)
                    got = parse_websearch(text or "")[:per_query]
                except Exception:  # noqa: BLE001
                    got = []
            for c in got:
                key = (c.get("url") or "").split("?")[0] or c["title"].lower()
                tkey = " ".join(c["title"].lower().split())[:80]
                if key in seen or tkey in seen:
                    continue
                seen |= {key, tkey}
                why = self.desk.blocked(c["title"] + " " + c.get("snippet", ""), c.get("source", ""), c.get("src_url") or c.get("url", ""))
                why = why or self.desk.is_spam(c["title"] + " " + c.get("snippet", ""), self.store.get("spam_level") or "soft")
                if why:
                    self.desk.log_spam(why, "web", c.get("source", ""), c.get("url", ""), c["title"] + "\n" + c.get("snippet", ""))
                    continue
                out.append(c)
        return out

    async def source_candidates(self, source_url, keywords="", hours=24):
        """Fallback when source API/RSS is unavailable; search and filter by source domain.

        Google News first, then configured web search. Returns candidates, not verified facts.
        """
        queries = source_search_queries(source_url, keywords, hours)
        target = domain(source_url)
        candidates = await self.web_candidates(queries, hours=hours)
        # Google News links can be redirects; source metadata is the best available hint.
        # Never claim a match solely because it appeared in a search result.
        result = []
        for c in candidates:
            origin = domain(c.get("src_url") or "") or domain(c.get("url") or "")
            if origin == target or origin.endswith("." + target):
                c = dict(c)
                c["discovery_method"] = "google_news_or_search_fallback"
                c["verified"] = False
                result.append(c)
        return result

    async def score(self, cands):
        """ШІ-оцінка важливості, відповідності, достовірності. Падіння моделі — евристика, не помилка."""
        res = {}
        try:
            data, _m = await self.engine._json(score_prompt(cands[:20], self.media_topics()), "Ти — випусковий редактор. Відповідай лише JSON.",
                                               "📰 Пульт: оцінка")
            for it in data.get("items") or []:
                n = int(it.get("n") or 0)
                if 1 <= n <= len(cands):
                    res[n - 1] = it
        except Exception as exc:  # noqa: BLE001
            log.info("score failed: %s", exc)
        out = []
        for i, c in enumerate(cands):
            sc = res.get(i, {})
            out.append((c, sc, priority(sc, c.get("published")) if sc else None))
        return out

    async def save_finds(self, origin, ref, scored):
        ids = []
        for c, sc, pr in scored:
            if sc.get("spam"):
                self.desk.log_spam("спам за оцінкою ШІ: " + str(sc.get("why", "")), "web", c.get("source", ""), c.get("url", ""), c["title"])
                continue
            fid = self.desk.add_find(origin, ref, c, sc, pr)
            if fid:
                ids.append(fid)
        return ids

    async def material_text(self, f):
        """Повний текст сторінки знахідки (якщо вдається відкрити), інакше заголовок і опис."""
        text = ""
        if self.host.browse and f.get("url"):
            try:
                page, _err = await self.host.browse(f["url"])
                text = (page or "")[:5000]
            except Exception:  # noqa: BLE001
                text = ""
        return text or (f["title"] + "\n" + (f.get("snippet") or ""))

    # ================================================================ написання
    def prompt_rows(self):
        return [[("📋 Промт для іншого ШІ", self.cb("Pr")), ("🔁 Іншою моделлю", self.cb("Prr"))]]

    async def compose(self, chat, finds, size, template_key=None, extra_note="", avoid=None):
        """Біг ньюз: кілька знахідок -> одна публікація за шаблоном. -> id чернетки"""
        items = []
        for f in finds[:5]:
            items.append(dict(f, text=await self.material_text(f)))
        story_id = finds[0].get("story_id")
        ours = [r for r in self.desk.story_refs(story_id) if r["kind"] == "ours"] if story_id else []
        ours_ctx = "\n".join(f"- {r['title']} ({r['url']})" for r in ours)
        tkey = template_key or size
        custom = self.store.get("tpl_" + tkey) or TEMPLATES[tkey][1]
        prompt = compose_prompt(items, size if size in ("short", "news", "big") else "news", custom, self.media_topics(), ours_ctx)
        sysp = system_prompt(self.store.get("rules") or "")
        # запам'ятовуємо промт: його можна скопіювати в будь-який інший ШІ або повторити іншою моделлю
        self.store.set(f"lastcomp:{chat}", json.dumps({"finds": [dict(f) for f in finds[:5]], "size": size, "tkey": tkey, "note": extra_note,
                                                       "prompt": prompt, "system": sysp, "model": ""}, ensure_ascii=False, default=str))
        data, model = await self.engine._json(prompt + "\n\n" + __import__("plugins.newsroom.engine", fromlist=["JSON_SPEC"]).JSON_SPEC,
                                              sysp, f"📰 Пульт: {SIZES.get(size, ('',))[0]}", avoid=avoid)
        try:
            lc = json.loads(self.store.get(f"lastcomp:{chat}") or "{}")
            lc["model"] = model
            self.store.set(f"lastcomp:{chat}", json.dumps(lc, ensure_ascii=False, default=str))
        except ValueError:
            pass
        data = clean(data)
        if data["skip"]:
            await self.host.notify(chat, f"ℹ️ {esc(model)} не написала: " + esc(data["notes"] or "модель вирішила, що матеріалу недостатньо"), self.prompt_rows())
            return None
        for f in finds[:5]:
            if f.get("url") and f["url"] not in data["sources"]:
                data["sources"].append(f["url"])
        if extra_note:
            data["notes"] = (extra_note + " " + data["notes"]).strip()
        kind = "article" if size == "big" else "news"
        did = self.store.add_draft(None, data, kind=kind, model=model, status="draft")
        self._autocheck(did)  # 🔎 кожна чернетка перевіряється одразу: факти й мова
        if story_id:
            self.desk.add_ref(story_id, "draft", did, data["headline"], relation=tkey)
        for f in finds[:5]:
            self.desk.update_find(f["id"], status="used")
        await self.host.notify(chat, *self.card(did))
        if size == "big" and self.store.get("word_bilingual"):
            await self.send_word(chat, did)
        return did

    async def send_word(self, chat, did):
        d = self.store.draft(did)
        ru = None
        try:
            tr, _m = await self.engine._json(
                "Переклади новину російською мовою. Факти, імена, цифри — без змін, назви українських населених пунктів — у "
                "загальноприйнятій російській транслітерації. Відповідай ЛИШЕ JSON {\"headline\": \"…\", \"body\": \"…\"}.\n\n"
                + json.dumps({"headline": d["headline"], "body": d["body"]}, ensure_ascii=False), "Ти — перекладач новин.", "📰 Пульт: переклад RU")
            ru = {"headline": str(tr.get("headline") or ""), "body": str(tr.get("body") or ""), "sources": d["sources"]}
        except Exception as exc:  # noqa: BLE001
            log.info("translation failed: %s", exc)
        sections = [("UA", article_blocks(d, "Українська версія"))]
        if ru and ru["body"]:
            sections.append(("RU", article_blocks(ru, "Русская версия", "Источники")))
        bot = self.host.bot()
        if bot:
            from telegram import InputFile
            name = f"statia_{did}_UA{'_RU' if len(sections) > 1 else ''}.docx"
            await bot.send_document(chat, InputFile(io.BytesIO(docx_bytes(sections)), filename=name),
                                    caption="📄 Стаття у Word: " + (" українська + російська" if len(sections) > 1 else " українська (переклад не вдався)"))

    # ================================================================ режими й теми
    def mode_queries(self, key):
        topics = self.desk.topics()
        if key in [t["key"] for t in topics]:
            kw = [k.strip() for k in self.desk.topic(key)["keywords"].split(",") if k.strip()]
            return [" OR ".join(kw[:4]), " OR ".join(kw[4:8]) or kw[0]], 48
        first = [t["keywords"].split(",")[0].strip() for t in topics]
        if key == "hot":
            return [" OR ".join(first), "терміново OR щойно " + first[0]], 6
        if key == "blog":
            return [f"(блог OR колонка OR \"думка\" OR \"авторська\") {k}" for k in first[:3]], 168
        return [" OR ".join(first)], 48

    async def run_mode(self, chat, key, size):
        title = MODES.get(key, (None,))[0] or (self.desk.topic(key) or {}).get("title", key)
        pg = await Progress(self.host, chat, f"{title} · {SIZES[size][0]}").start("🔎 Крок 1 з 3: шукаю свіже в інтернеті…")
        try:
            queries, hours = self.mode_queries(key)
            angle_note = ""
            if key in ("ever", "prov"):
                facts = ""
                if key == "prov":
                    hot = await self.web_candidates(*self.mode_queries("hot"))
                    facts = "\n".join(f"- {c['title']} ({c.get('source')})" for c in hot[:12])
                data, _m = await self.engine._json(angles_prompt(key, self.media_topics(), facts), "Ти — головний редактор.", f"📰 Пульт: {title}")
                angles = (data.get("angles") or [])[:5]
                queries = [a.get("query") or a.get("title") for a in angles if a.get("query") or a.get("title")] or queries
                hours = 24 * 30 if key == "ever" else 72
                angle_note = "Кути: " + "; ".join(short(a.get("title"), 60) for a in angles[:5])
            cands = await self.web_candidates(queries, hours)
            if not cands:
                return await pg.done(f"🔎 {esc(title)}: нічого свіжого не знайшов (або все відсіяли блокування й антиспам)",
                                     [[("🚫 Блокування", self.cb("Bk")), ("🔎 Знайти й написати", self.cb("D"))]])
            await pg.set(f"⭐ Крок 2 з 3: оцінюю знайдене ({len(cands)})…")
            scored = await self.score(cands)
            run = f"{key}-{int(time.time())}"
            ids = await self.save_finds("mode", run, scored)
            fs = self.desk.finds(ids=ids, sort="prio", limit=30)
            if not fs:
                return await self.host.notify(chat, "🔎 Усе знайдене схоже на рекламу чи спам — дивіться 🧹 Антиспам.")
            await self.host.notify(chat, *self.finds_screen("mode", run, "prio", 0, title=f"{title} · {SIZES[size][0]}"))
            groups = {}
            for f in fs:
                groups.setdefault(f["story_id"], []).append(f)
            best = max(groups.values(), key=lambda g: (sum(x["priority"] for x in g[:3]), len(g)))
            tkey = "big" if size == "big" else size
            await pg.set("✍️ Крок 3 з 3: ШІ пише чернетку…")
            did = await self.compose(chat, best, size, tkey, (f"{title}. " + angle_note).strip())
            await pg.done(f"✅ {esc(title)}: чернетку готово — нижче" if did else f"ℹ️ {esc(title)}: чернетку не написано", self.prompt_rows())
        except Exception as exc:  # noqa: BLE001
            log.exception("run_mode")
            await pg.done(f"⚠️ {esc(title)}: не вдалося — {esc(exc)[:300]}", self.prompt_rows())

    async def run_tag(self, tag, chat=None, notify=True):
        hours = max(2, int(tag["interval"] // 3600) + 2) if tag["last_run"] else 24
        cands = await self.web_candidates([tag["query"]], hours, per_query=15)
        ids = []
        for c in cands:
            fid = self.desk.add_find("tag", tag["id"], c)
            if fid:
                ids.append(fid)
        self.desk.x("UPDATE tags SET last_run=?, found=found+? WHERE id=?", (time.time(), len(ids), tag["id"]))
        if ids and notify:
            text, rows = self.finds_screen("tag", tag["id"], tag["sort"], 0, only_ids=ids, title=f"🏷 {tag['query']} · нових {len(ids)}")
            if chat:
                await self.host.notify(chat, text, rows)
            else:
                await self.host.notify_admins(text, rows)
        return len(ids)

    async def _tags_loop(self):
        __import__("notify").KIND.set("news")  # 🔔 тип сповіщень для режимів людини
        await asyncio.sleep(90)
        while True:
            try:
                if self.on():
                    for t in self.desk.tags():
                        if t["enabled"] and time.time() - t["last_run"] >= t["interval"]:
                            await self.run_tag(t)
                            await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("tags loop")
            await asyncio.sleep(120)

    # ================================================================ наші ресурси й історії
    async def check_against_ours(self, item, raw):
        """-> (relation, ours_posts, story_id). Оновлення/спростування нашого — окреме сповіщення з усіма згадками."""
        ours = self.desk.similar_ours(raw)
        if not ours:
            sid, _ = self.desk.assign_story(raw.split("\n")[0][:140], raw)
            return "", [], sid
        rel, what = "inshe", ""
        try:
            data, _m = await self.engine._json(relation_prompt(raw, ours), "Ти — випусковий редактор. Лише JSON.", "📰 Новинарня: звірка з нашими")
            rel = str(data.get("relation") or "inshe").lower()
            what = str(data.get("what") or "")[:200]
        except Exception as exc:  # noqa: BLE001
            log.info("relation failed: %s", exc)
            rel = "dopovnennia"
        sid = ours[0]["story_id"]
        title = raw.strip().split("\n")[0][:140]
        self.desk.add_ref(sid, "item", item["id"], title, item.get("source_url", ""), rel if rel in REL else "inshe", what, text=raw)
        self.store.set_item(item["id"], story_id=sid, relation=rel)
        if rel != "inshe":
            await self.notify_story(sid, item, rel, what)
        return rel, ours, sid

    def story_text(self, sid, highlight=None):
        st = self.desk.story(sid)
        refs = self.desk.story_refs(sid)
        ours = [r for r in refs if r["kind"] == "ours"]
        upd = [r for r in refs if r["kind"] in ("item", "find") and r["relation"] in ("dopovnennia", "onovlennia", "sprostuvannia", "povtor")]
        other = [r for r in refs if r not in ours and r not in upd]
        lines = [f"📚 <b>Історія #{sid}</b>: {esc(short(st['title'] if st else '', 90))}", ""]
        if ours:
            lines.append(f"🏠 <b>У нас ({len(ours)}):</b>")
            lines += [f"• {time.strftime('%d.%m %H:%M', time.localtime(r['at']))} — {esc(short(r['title'], 80))}" + (f"\n  {esc(r['url'])}" if r["url"] else "") for r in ours]
        if upd:
            lines.append(f"\n🔁 <b>Оновлення й реакції ({len(upd)}):</b>")
            lines += [f"• {REL.get(r['relation'], r['relation'])} · {time.strftime('%d.%m %H:%M', time.localtime(r['at']))} — {esc(short(r['title'], 70))}"
                      + (f" ({esc(r['note'])})" if r["note"] else "") + (f"\n  {esc(r['url'])}" if r["url"] else "") for r in upd]
        if other:
            lines.append(f"\n📎 <b>Інші матеріали ({len(other)}):</b>")
            lines += [f"• {r['kind']} · {esc(short(r['title'], 80))}" for r in other[-8:]]
        return "\n".join(lines)[:4000]

    async def notify_story(self, sid, item, rel, what):
        head = {"povtor": "♻️ <b>Це вже було в нас</b>", "dopovnennia": "➕ <b>Доповнення до нашої новини</b>",
                "onovlennia": "🔁 <b>Оновлення нашої новини</b>", "sprostuvannia": "❌ <b>Спростування нашої новини</b>"}.get(rel, "🔗 Схоже на нашу новину")
        new = f"\n\n🆕 Нове: {esc(short(item.get('raw_text', ''), 160))}" + (f"\n{esc(item.get('source_url'))}" if item.get("source_url") else "") + (f"\nЩо саме: {esc(what)}" if what else "")
        text = head + new + "\n\n" + self.story_text(sid)
        rows = [[("🔁 Написати оновлення", self.cb("Su", sid, "update")), ("❌ Написати спростування", self.cb("Su", sid, "refute"))],
                [("📚 Історія", self.cb("St", sid))]]
        await self._tell(item, text[:4000], rows)

    # ================================================================ екрани
    def D_home(self, chat):
        s = self.store
        t = self.desk.topics()
        text = ("🔎 <b>Знайти й написати</b>\n\n"
                "Оберіть, що шукати: бот знайде свіже в інтернеті, оцінить важливість і достовірність, згрупує схоже, "
                "звірить із нашими публікаціями й одразу напише — коротко, новину або велику статтю.\n\n"
                "Щоб лише стежити за темою без написання — «🏷 Стежити за темою».")
        rows = [[("📅 Теми дня", self.cb("obh"))],
                [(MODES[k][0], self.cb("Dm", k)) for k in ("hot", "ever")], [(MODES[k][0], self.cb("Dm", k)) for k in ("prov", "blog")]]
        tb = [(x["title"], self.cb("Dm", x["key"])) for x in t if "_" not in x["key"]]
        rows += [tb[i:i + 2] for i in range(0, len(tb), 2)]
        rows += [[("🧾 Зведення", self.cb("dg")), ("🏷 Стежити за темою", self.cb("Tg"))],
                 [("« Новинарня", self.cb())]]
        return text, rows

    def D_mode(self, key):
        title, desc = MODES.get(key, (None, None))
        if not title:
            tp = self.desk.topic(key)
            title, desc = tp["title"], "ключові слова: " + tp["keywords"]
        children = [t for t in self.desk.topics() if t["key"].startswith(key + "_")]
        rows = [[(t["title"], self.cb("Dm", t["key"]))] for t in children]
        rows += [[(SIZES[s][0], self.cb("Dgo", key, s))] for s in ("short", "news", "big")]
        rows.append([("🔎 Лише знайти, не писати", self.cb("Dfind", key)), ("⏰ За розкладом", self.cb("sch", key))])
        rows.append([("« Знайти й написати", self.cb("D"))])
        word = "\n📄 Велика стаття одразу йде у Word українською й російською." if self.store.get("word_bilingual") else ""
        return (f"{esc(title)}\n<i>{esc(desc)}</i>\n\nЩо написати?\n" + "\n".join(f"{SIZES[s][0]} — {SIZES[s][1]}" for s in SIZES) + word), rows

    def finds_screen(self, origin, ref, sort="prio", offset=0, only_ids=None, title=None, folder=None):
        fs = self.desk.finds(origin=origin, ref=ref, folder=folder, ids=only_ids, sort=sort, limit=9, offset=offset)
        name = title or ("🏷 " + (self.desk.tag(ref) or {}).get("query", "") if origin == "tag" else FOLDERS.get(folder, "🔎 Знайдене"))
        lines = [f"<b>{esc(name)}</b>", ""]
        rows, nums = [], []
        for n, f in enumerate(fs[:8], offset + 1):
            sc = f["score"]
            lines.append(f"{n}. {prio_mark(f['priority'])} <b>{esc(short(f['title'], 110))}</b>\n"
                         f"   {esc(f['source'] or domain(f['url']))} · {age(f['published'])}" + (f" · {esc(sc.get('topic'))}" if sc.get("topic") else "")
                         + (f"\n   {esc(short(f['snippet'], 140))}" if f["snippet"] else "") + (f"\n   {esc(f['url'][:120])}" if f["url"] else ""))
            nums.append((f"{n} ⋯", self.cb("Fi", f["id"])))
        if not fs:
            lines.append("Порожньо.")
        rows += [nums[i:i + 4] for i in range(0, len(nums), 4)]
        if origin and ref is not None and not only_ids:
            rows.append([(("● " if sort == k else "") + t, self.cb("Fl", origin, ref, k)) for k, t in SORTS])
            nav = []
            if offset:
                nav.append(("⬅️", self.cb("Fl", origin, ref, sort, max(0, offset - 8))))
            if len(fs) > 8:
                nav.append(("➡️", self.cb("Fl", origin, ref, sort, offset + 8)))
            if nav:
                rows.append(nav)
        if fs and origin == "tag":
            rows.append([("📚 Біг ньюз із верхніх", self.cb("Fbig", fs[0]["id"]))])
        rows.append([("🔎 Знайти й написати", self.cb("D"))])
        return "\n".join(lines)[:4000], rows

    def F_card(self, fid):
        f = self.desk.find(fid)
        if not f:
            return "Не знайдено.", [[("🔎 Знайти й написати", self.cb("D"))]]
        sc = f["score"]
        st = self.desk.story(f["story_id"]) if f["story_id"] else None
        text = (f"{prio_mark(f['priority'])} <b>{esc(f['title'])}</b>\n\n"
                f"📡 {esc(f['source'] or domain(f['url']))}" + (f" · ✍️ {esc(f['author'])}" if f["author"] else "") + f" · 🕘 {age(f['published'])}\n"
                f"⭐ Пріоритет {f['priority']}" + (f" · важливість {sc.get('importance')}/10 · тема {sc.get('fit')}/10 · {esc(sc.get('credibility', ''))}" if sc else "")
                + (f"\n💬 {esc(sc.get('why'))}" if sc.get("why") else "")
                + (f"\n\n{esc(f['snippet'])}" if f["snippet"] else "")
                + (f"\n\n📝 {esc(f['note'])}" if f["note"] else "")
                + (f"\n📚 В історії: {st['size']} матеріалів" + (" · 🏠 є в нас" if st["ours"] else "") if st else "")
                + (f"\n📁 {FOLDERS.get(f['folder'], '')}" if f["folder"] else "") + ("\n✅ вже використано" if f["status"] == "used" else ""))
        rows = [[("📱 Коротко", self.cb("Fw", fid, "short")), ("📰 Новина", self.cb("Fw", fid, "news")), ("📚 Багато", self.cb("Fw", fid, "big"))],
                [("✅ Підтвердити", self.cb("Fchk", fid)), ("❌ Спростувати", self.cb("Fref", fid))],
                [("✏️ Нотатка", self.cb("Fed", fid)), ("📁 Перемістити", self.cb("Fmv", fid))],
                [("🔗 Схожі", self.cb("St", f["story_id"])), ("🚫 Блокувати", self.cb("Fbl", fid))],
                [("🗑 Видалити", self.cb("Fdel", fid))]]
        if f["url"].startswith("http"):
            rows.append([("🌐 Відкрити джерело", "url:" + f["url"])])
        rows.append([("🔎 Знайти й написати", self.cb("D"))])
        return text[:4000], rows

    def tags_screen(self):
        ts = self.desk.tags()
        lines = ["🏷 <b>Теги</b>", "", "Тег лише стежить: шукає новини за запитом і надсилає заголовки, опис і посилання. "
                 "Писати — з картки знахідки (⋯) або кнопками пульта.", ""]
        rows = []
        for t in ts:
            iv = dict(TAG_IVS).get(t["interval"], f"{t['interval'] // 60} хв")
            lines.append(f"{'🟢' if t['enabled'] else '⚪️'} <b>{esc(t['query'])}</b> · кожні {iv} · знайдено {t['found']} · {age(t['last_run']) if t['last_run'] else 'ще не запускався'}")
            rows.append([(("🟢 " if t["enabled"] else "⚪️ ") + short(t["query"], 26), self.cb("Tgi", t["id"]))])
        if not ts:
            lines.append("Поки жодного. Приклади: «Укренерго відключення», «сміттєзвалище OR звалище», «\"Зелений фронт\"».")
        rows.append([("➕ Новий тег", self.cb("Tgadd")), ("« Джерела", self.cb("H1"))])
        return "\n".join(lines)[:4000], rows

    def tag_screen(self, tid):
        t = self.desk.tag(tid)
        if not t:
            return "Тег не знайдено.", [[("« Теги", self.cb("Tg"))]]
        ivs = [(("● " if t["interval"] == v else "") + n, self.cb("Tgiv", tid, v)) for v, n in TAG_IVS]
        rows = [[(("⏸ Вимкнути" if t["enabled"] else "▶️ Увімкнути"), self.cb("Tgt", tid)), ("🔄 Шукати зараз", self.cb("Tgrun", tid))],
                ivs[:3], ivs[3:],
                [(("● " if t["sort"] == k else "") + n, self.cb("Tgs", tid, k)) for k, n in SORTS],
                [("📋 Знайдене", self.cb("Fl", "tag", tid, t["sort"])), ("✏️ Запит", self.cb("Tged", tid))],
                [("🗑 Видалити тег", self.cb("Tgdel", tid)), ("« Теги", self.cb("Tg"))]]
        return (f"🏷 <b>{esc(t['query'])}</b>\nЗапит підтримує OR, лапки для точної фрази й мінус-слова (-реклама).\n"
                f"Знайдено всього: {t['found']} · сортування дайджесту: {dict(SORTS)[t['sort']]}"), rows

    def topics_screen(self):
        lines = ["🗂 <b>Теми кнопок пульта</b>", "", "Кожна тема — кнопка швидкого пошуку й написання. Ключові слова — через кому."]
        rows = []
        for t in self.desk.topics():
            lines.append(f"\n{esc(t['title'])}: {esc(short(t['keywords'], 160))}")
            rows.append([(f"✏️ {t['title']}", self.cb("Tpe", t["id"])), ("🗑", self.cb("Tpdel", t["id"]))])
        rows.append([("➕ Нова тема", self.cb("Tpadd")), ("« Як писати", self.cb("H3"))])
        return "\n".join(lines)[:4000], rows

    def blocks_screen(self):
        lines = ["🚫 <b>Блокування</b>", "", "Заблоковане не потрапляє ні в новинарню, ні в знахідки, ні в теги — з будь-якого джерела."]
        rows = []
        for k, title in BLOCK_KINDS.items():
            bs = self.desk.blocks(k)
            lines.append(f"\n<b>{title}</b>: " + (", ".join(f"{esc(b['value'])} ({b['hits']})" for b in bs) if bs else "—"))
            rows.append([(f"➕ {title}", self.cb("Bka", k))])
        allb = self.desk.blocks()
        if allb:
            rows.append([("🗑 Прибрати блокування", self.cb("Bkl"))])
        rows.append([("« Фільтри", self.cb("H4"))])
        return "\n".join(lines)[:4000], rows

    def blocks_del_screen(self):
        rows = [[(f"🗑 {BLOCK_KINDS.get(b['kind'], b['kind']).split(' ')[0]} {short(b['value'], 28)}", self.cb("Bkd", b["id"]))] for b in self.desk.blocks()[:40]]
        rows.append([("« Блокування", self.cb("Bk"))])
        return "🗑 <b>Що розблокувати?</b>", rows

    def spam_screen(self):
        lvl = self.store.get("spam_level") or "soft"
        logs = self.desk.q("SELECT * FROM spamlog ORDER BY id DESC LIMIT 8")
        lines = ["🧹 <b>Антиспам</b>", "",
                 "Бот сам відсіює рекламу, розіграші, казино, «заробіток», капслок, купи посилань і емодзі, телефони для продажу; "
                 "ШІ додатково позначає рекламу й клікбейт при оцінці. Відсіяне не витрачає ліміти моделей.",
                 f"\nРівень: <b>{ {'off': 'вимкнено', 'soft': 'мʼякий', 'strict': 'суворий'}[lvl] }</b>",
                 f"Відсіяно всього: {self.desk.q('SELECT COUNT(*) AS n FROM spamlog')[0]['n']}", "\n<b>Останнє відсіяне:</b>"]
        rows = [[(("● " if lvl == k else "") + t, self.cb("Spl", k)) for k, t in (("off", "Вимк."), ("soft", "Мʼякий"), ("strict", "Суворий"))]]
        for r in logs:
            lines.append(f"• {esc(short(r['text'], 70))}\n  <i>{esc(r['reason'])}</i>")
            rows.append([(f"↩️ Не спам: {short(r['text'], 24)}", self.cb("Spr", r["id"]))])
        if not logs:
            lines.append("— нічого")
        rows.append([("« Фільтри", self.cb("H4"))])
        return "\n".join(lines)[:4000], rows

    def ours_screen(self):
        lists = [l for l in self.lists.lists() if l["auto"] == "ours"]
        rss = [s for s in self.store.sources("rss") if s.get("ours")]
        recent = self.desk.q("SELECT * FROM ours_posts ORDER BY at DESC LIMIT 6")
        lines = ["🏠 <b>Наші ресурси</b>", "",
                 "Звідси новини <b>не беруться</b>, але бот стежить за нашими публікаціями, щоб не повторюватись і бачити відгомін: репости, "
                 "цитування, перекази, згадки, плагіат. Кожну нову новину він "
                 "звіряє з нашими: ♻️ повтор, ➕ доповнення, 🔁 оновлення, ❌ спростування. Тоді бот надсилає посилання на всі наші "
                 "згадки й усі оновлення цієї історії. Усе, що публікує сама новинарня, теж потрапляє сюди.",
                 f"\nКанали, чати й соцмережі: {sum(l['n'] for l in lists)} (з них соцмереж {self.social_count(True)}) · сайти (RSS): {len(rss)} · "
                 f"публікацій у базі: {self.desk.ours_count()}",
                 "\n<b>Останні наші:</b>"]
        lines += [f"• {time.strftime('%d.%m', time.localtime(r['at']))} {esc(short(r['title'], 80))}" for r in recent] or ["— поки немає"]
        lid = lists[0]["id"] if lists else None
        c = self.echo.counts()
        lines.insert(4, f"📣 Відгомін: {sum(c.values())}" + (f" · 🚩 плагіат {c['plagiarism']}" if c.get("plagiarism") else ""))
        rows = [[("📊 Звіт за місяць", self.cb("rp")), ("📣 Відгомін наших новин", self.cb("echo"))],
                [("📋 Наші канали й чати", self.cb("Lo", lid) if lid else self.cb("Ouc")), ("🌐 Наші соцмережі", self.cb("SNadd", "ours"))],
                [("🌐 Наш сайт (RSS)", self.cb("Ours")), ("➕ Додати нашу публікацію", self.cb("Oua"))],
                [("📚 Історії з нашими", self.cb("Sl", 1)), ("« Джерела", self.cb("H1"))]]
        return "\n".join(lines)[:4000], rows

    def stories_screen(self, only_ours=False):
        ss = self.desk.q("SELECT * FROM stories" + (" WHERE ours=1" if only_ours else "") + " ORDER BY updated DESC LIMIT 12")
        lines = ["📚 <b>Історії</b> — схожі новини й теми, згруповані разом", ""]
        rows = []
        for s in ss:
            lines.append(f"• #{s['id']} {'🏠 ' if s['ours'] else ''}{esc(short(s['title'], 80))} · {s['size']} матеріалів · {age(s['updated'])}")
            rows.append([(f"{'🏠 ' if s['ours'] else ''}#{s['id']} {short(s['title'], 28)}", self.cb("St", s["id"]))])
        if not ss:
            lines.append("Поки порожньо.")
        rows.append([(("Усі історії" if only_ours else "🏠 Лише з нашими"), self.cb("Sl", 0 if only_ours else 1)), ("« Архів", self.cb("H5"))])
        return "\n".join(lines)[:4000], rows

    def templates_screen(self):
        lines = ["📐 <b>Шаблони написання</b>", ""]
        rows = []
        for k, (title, base) in TEMPLATES.items():
            own = self.store.get("tpl_" + k)
            lines.append(f"<b>{title}</b>{' ✏️ свій' if own else ''}\n{esc(short(own or base, 220))}\n")
            rows.append([(f"✏️ {title}", self.cb("Wte", k))] + ([("↩️ Базовий", self.cb("Wtr", k))] if own else []))
        rows.append([("« Як писати", self.cb("H3"))])
        return "\n".join(lines)[:4000], rows

    def desk_settings(self):
        s = self.store
        text = ("⚙️ <b>Параметри пульта</b>\n\n"
                f"Тематика медіа (для оцінки «пасує темі», вічних тем і провокацій): {esc(self.media_topics())}\n\n"
                f"📄 Велика стаття одразу у Word українською й російською: {'так — сценарій погоджено' if s.get('word_bilingual') else 'ні'}")
        rows = [[("✏️ Тематика медіа", self.cb("Dst"))],
                [(("📄 Word UA+RU: так" if s.get("word_bilingual") else "📄 Word UA+RU: ні"), self.cb("Dwb"))],
                [("« Знайти й написати", self.cb("D"))]]
        return text, rows

    # ================================================================ дії
    async def desk_route(self, chat, a, args):
        x = args[0] if args else None
        y = args[1] if len(args) > 1 else None
        z = args[2] if len(args) > 2 else None
        D = self.desk
        cancel = [[("✖️ Скасувати", "m:cancel")]]
        if a == "D":
            return None, self.D_home(chat)
        if a == "Dm":
            return None, self.D_mode(x)
        if a == "Dgo":
            self._bg(self.run_mode(chat, x, y))
            return "🔎 Шукаю й пишу…", ("🔎 <b>Шукаю, оцінюю й пишу</b>\nУ чаті з ботом з'явиться повідомлення з ходом роботи (крок і секунди), потім — список знайденого й чернетка.\nЯкщо ШІ відмовиться — там будуть «📋 Промт для іншого ШІ» і «🔁 Іншою моделлю».",
                                       [[("🔎 Знайти й написати", self.cb("D"))]])
        if a == "Pr":  # 📋 промт — щоб скопіювати в будь-який інший ШІ
            lc = json.loads(self.store.get(f"lastcomp:{chat}") or "{}")
            if not lc.get("prompt"):
                return "Промту ще немає", None
            full = f"{lc['system']}\n\n{lc['prompt']}"
            if len(full) > 3300:
                bot = self.host.bot()
                if bot:
                    from telegram import InputFile
                    await bot.send_document(chat, InputFile(io.BytesIO(full.encode("utf-8")), filename="prompt.txt"),
                                            caption="📋 Повний промт — відкрийте й скопіюйте в будь-який ШІ.")
                return "📋 Промт надіслано файлом", None
            return None, (f"📋 <b>Промт</b> (писала: {esc(lc.get('model') or '—')})\nТоркніться тексту, щоб скопіювати, і вставте в будь-який ШІ:\n\n<pre>{esc(full)}</pre>",
                          [[("🔁 Іншою моделлю", self.cb("Prr"))], [("« Знайти й написати", self.cb("D"))]])
        if a == "Prr":  # 🔁 те саме завдання — іншою моделлю (без тієї, що відмовилась)
            lc = json.loads(self.store.get(f"lastcomp:{chat}") or "{}")
            if not lc.get("prompt"):
                return "Немає що повторювати", None

            async def again():
                pg = await Progress(self.host, chat, "Інша модель").start(f"✍️ Пише інша модель (не {lc.get('model') or '—'})…")
                try:
                    did = await self.compose(chat, lc["finds"], lc["size"], lc["tkey"], lc.get("note", ""), avoid=lc.get("model") or None)
                    await pg.done("✅ Готово — чернетка нижче" if did else "ℹ️ Чернетку не написано", self.prompt_rows())
                except Exception as exc:  # noqa: BLE001
                    await pg.done(f"⚠️ Не вдалося: {esc(exc)[:300]}", self.prompt_rows())
            self._bg(again())
            return "🔁 Передаю іншій моделі…", None
        if a == "Dfind":
            async def only_find():
                queries, hours = self.mode_queries(x)
                cands = await self.web_candidates(queries, hours)
                run = f"{x}-{int(time.time())}"
                ids = await self.save_finds("mode", run, await self.score(cands)) if cands else []
                if not ids:
                    return await self.host.notify(chat, "🔎 Нічого свіжого не знайшов.")
                await self.host.notify(chat, *self.finds_screen("mode", run, "prio", 0))
            self._bg(only_find())
            return "🔎 Шукаю…", ("🔎 Шукаю — список прийде окремим повідомленням.", [[("🔎 Знайти й написати", self.cb("D"))]])
        if a == "Ds":
            return None, self.desk_settings()
        if a == "Dst":
            self.ask(chat, "Dst")
            return None, ("✏️ Опишіть тематику медіа кількома словами, наприклад: «екологія, агро, енергетика, здоровʼя, суспільство України».", cancel)
        if a == "Dwb":
            self.store.set("word_bilingual", not self.store.get("word_bilingual"))
            return None, self.desk_settings()
        # ----- знахідки
        if a == "Fl":
            return None, self.finds_screen(x, y, z or "prio", int(args[3]) if len(args) > 3 else 0)
        if a == "Fo":
            rows = [[(t, self.cb("Ff", k))] for k, t in FOLDERS.items()] + [[("« Архів", self.cb("H5"))]]
            return None, ("📁 <b>Папки знахідок</b>", rows)
        if a == "Ff":
            return None, self.finds_screen(None, None, "time", 0, folder=x)
        if a == "Fi":
            return None, self.F_card(int(x))
        if a == "Fw":
            f = D.find(x)
            same = [g for g in D.finds(sort="prio", limit=40) if g["story_id"] == f["story_id"] and g["id"] != f["id"]] if f["story_id"] else []
            self._bg(self.compose(chat, [f] + same[:4], y, y))
            return "✍️ Пишу…", ("✍️ <b>Пишу</b>" + (f" з {1 + len(same[:4])} матеріалів цієї історії" if same else "") + " — чернетка прийде окремим повідомленням.",
                               [[("« Знахідка", self.cb("Fi", x))]])
        if a == "Fbig":
            f = D.find(x)
            fs = D.finds(origin=f["origin"], ref=f["ref"], sort="prio", limit=5)
            self._bg(self.compose(chat, fs, "big", "big"))
            return "📚 Пишу біг ньюз…", ("📚 Пишу одну велику публікацію з верхніх знахідок — прийде окремим повідомленням.", [[("🔎 Знайти й написати", self.cb("D"))]])
        if a == "Fchk":
            async def chk():
                f = D.find(x)
                rep = await self.engine.check({"headline": f["title"], "body": f["snippet"] or f["title"]}, self.host.search) if self.host.search else "вебпошук недоступний"
                D.update_find(x, note=("🔎 " + rep)[:1000])
                await self.host.notify(chat, *self.F_card(int(x)))
            self._bg(chk())
            return "🔎 Перевіряю…", ("🔎 Перевіряю факти пошуком — звіт додасться до картки.", [[("« Знахідка", self.cb("Fi", x))]])
        if a == "Fref":
            f = D.find(x)
            self._bg(self.compose(chat, [f], "news", "refute", "Запит на спростування."))
            return "❌ Готую спростування…", ("❌ Готую спростування. Якщо підтверджених доказів немає, бот так і скаже, а не вигадуватиме.",
                                             [[("« Знахідка", self.cb("Fi", x))]])
        if a == "Fed":
            self.ask(chat, "Fed", int(x))
            return None, ("✏️ Нотатка до знахідки (або «-», щоб очистити):", cancel)
        if a == "Fmv":
            rows = [[("→ " + t, self.cb("Fmvt", x, k))] for k, t in FOLDERS.items()] + [[("📂 Без папки", self.cb("Fmvt", x, "none"))], [("« Знахідка", self.cb("Fi", x))]]
            return None, ("📁 <b>Куди перемістити?</b>", rows)
        if a == "Fmvt":
            D.update_find(x, folder="" if y == "none" else y)
            return "Переміщено", self.F_card(int(x))
        if a == "Fdel":
            D.update_find(x, status="deleted")
            return "Видалено", ("🗑 Знахідку видалено.", [[("🔎 Знайти й написати", self.cb("D"))]])
        if a == "Fbl":
            f = D.find(x)
            rows = [[(f"📡 Джерело «{short(f['source'] or domain(f['url']), 22)}»", self.cb("Fblv", x, "source"))]]
            if domain(f["url"]) and "news.google" not in domain(f["url"]):
                rows.append([(f"🌐 Сайт {short(domain(f['url']), 24)}", self.cb("Fblv", x, "domain"))])
            rows += [[("🔤 Тему / слово…", self.cb("Bka", "word")), ("👤 Людину…", self.cb("Bka", "person"))], [("« Знахідка", self.cb("Fi", x))]]
            return None, ("🚫 <b>Що блокувати?</b>", rows)
        if a == "Fblv":
            f = D.find(x)
            D.add_block(y, (f["source"] if y == "source" else domain(f["url"])) or "")
            D.update_find(x, status="deleted")
            return "🚫 Заблоковано", self.blocks_screen()
        # ----- теги
        if a == "Tg":
            return None, self.tags_screen()
        if a == "Tgadd":
            self.ask(chat, "Tgadd")
            return None, ("🏷 Напишіть запит тегу. Можна OR, \"точна фраза\", -мінус-слово. Кілька тегів — кожен з нового рядка.", cancel)
        if a == "Tgi":
            return None, self.tag_screen(int(x))
        if a == "Tgt":
            t = D.tag(x)
            D.x("UPDATE tags SET enabled=? WHERE id=?", (0 if t["enabled"] else 1, int(x)))
            return None, self.tag_screen(int(x))
        if a == "Tgiv":
            D.x("UPDATE tags SET interval=? WHERE id=?", (int(y), int(x)))
            return "Збережено", self.tag_screen(int(x))
        if a == "Tgs":
            D.x("UPDATE tags SET sort=? WHERE id=?", (y, int(x)))
            return None, self.tag_screen(int(x))
        if a == "Tgrun":
            n = await self.run_tag(D.tag(x), chat=chat)
            return (f"Нових: {n}" if n else "Нового немає"), self.tag_screen(int(x))
        if a == "Tged":
            self.ask(chat, "Tged", int(x))
            return None, ("✏️ Новий запит тегу:", cancel)
        if a == "Tgdel":
            D.x("DELETE FROM tags WHERE id=?", (int(x),))
            return "Тег видалено", self.tags_screen()
        # ----- теми
        if a == "Tp":
            return None, self.topics_screen()
        if a == "Tpadd":
            self.ask(chat, "Tpadd")
            return None, ("🗂 Нова тема: <code>емодзі Назва | ключові, слова, через кому</code>\nНаприклад: <code>🏙 Міста | урбаністика, транспорт, ЖКГ</code>", cancel)
        if a == "Tpe":
            self.ask(chat, "Tpe", int(x))
            return None, ("✏️ Нові ключові слова для теми (через кому). Можна також «Назва | слова».", cancel)
        if a == "Tpdel":
            r = D.q("SELECT key FROM topics WHERE id=?", (int(x),))
            if r:
                self.store.set("topic_removed_" + r[0]["key"], True)  # щоб типова тема не поверталась сама
            D.x("DELETE FROM topics WHERE id=?", (int(x),))
            return "Тему прибрано", self.topics_screen()
        # ----- блокування й спам
        if a == "Bk":
            return None, self.blocks_screen()
        if a == "Bka":
            self.ask(chat, "Bka", x)
            return None, (f"🚫 {BLOCK_KINDS[x]} — що блокувати? Кілька — через кому.", cancel)
        if a == "Bkl":
            return None, self.blocks_del_screen()
        if a == "Bkd":
            D.del_block(x)
            return "Розблоковано", self.blocks_del_screen()
        if a == "Sp":
            return None, self.spam_screen()
        if a == "Spl":
            self.store.set("spam_level", x)
            return None, self.spam_screen()
        if a == "Spr":
            r = D.q("SELECT * FROM spamlog WHERE id=?", (int(x),))
            if r:
                D.x("DELETE FROM spamlog WHERE id=?", (int(x),))
                self.store.add_item("manual", r[0]["text"], r[0]["source"], r[0]["url"], requested_by=chat)
                iid = self.store.queued()[-1]
                self._enqueue(iid)
            return "↩️ Повернуто в роботу", self.spam_screen()
        # ----- наші ресурси, історії
        if a == "Ou":
            return None, self.ours_screen()
        if a == "Ouc":
            lid = self.lists.auto_list("ours")
            return None, self.L_list(lid)
        if a == "Ours":
            rows = [[(("🏠 " if s.get("ours") else "▫️ ") + short(s["title"] or s["ref"], 30), self.cb("Ourt", s["id"]))] for s in self.store.sources("rss")]
            rows += [[("➕ Додати RSS", self.cb("soadd"))], [("« Наші ресурси", self.cb("Ou"))]]
            return None, ("🌐 <b>Які RSS-стрічки — наші?</b>\n🏠 — наша (не беремо, лише звіряємо). Додайте RSS свого сайту в «📡 RSS» і позначте тут.", rows)
        if a == "Ourt":
            s = self.store.source(int(x))
            self.store.set_source(int(x), ours=0 if s.get("ours") else 1)
            return None, (await self.desk_route(chat, "Ours", []))[1]
        if a == "Oua":
            self.ask(chat, "Oua")
            return None, ("➕ Вставте нашу публікацію: посилання першим рядком (необовʼязково), далі текст. Кілька — розділіть рядком «---».", cancel)
        if a == "Sl":
            return None, self.stories_screen(x == "1")
        if a == "St":
            if not x or x == "None":
                return "Історії немає", None
            refs = D.story_refs(x)
            rows = [[("🔁 Написати оновлення", self.cb("Su", x, "update")), ("📚 Біг ньюз з історії", self.cb("Su", x, "big"))],
                    [("❌ Спростування", self.cb("Su", x, "refute")), ("📚 Історії", self.cb("Sl"))]]
            return None, (self.story_text(x), rows)
        if a == "Su":
            refs = D.story_refs(x)
            mats = []
            for r in refs:
                if r["kind"] in ("find", "item") and r["relation"] != "povtor":
                    mats.append({"id": 0, "title": r["title"], "url": r["url"], "source": domain(r["url"]), "published": r["at"],
                                 "snippet": r["text"][:1500], "story_id": int(x)})
            if not mats:
                return "У історії немає нових матеріалів", None
            fin = [dict(m, id=-1) for m in mats[-5:]]

            async def go():
                for m in fin:
                    m["id"] = 0
                await self.compose(chat, fin, "big" if y == "big" else "news", y if y in TEMPLATES else "news")
            self._bg(go())
            return "✍️ Пишу…", ("✍️ Пишу на основі історії — чернетка прийде окремим повідомленням.", [[("📚 Історія", self.cb("St", x))]])
        # ----- шаблони
        if a == "Wt":
            return None, self.templates_screen()
        if a == "Wte":
            self.ask(chat, "Wte", x)
            return None, (f"📐 Опишіть свій шаблон «{TEMPLATES[x][0]}» — структуру, тон, обсяг. Базовий:\n<i>{esc(TEMPLATES[x][1])}</i>", cancel)
        if a == "Wtr":
            self.store.set("tpl_" + x, "")
            return "Повернуто базовий", self.templates_screen()
        return None, self.D_home(chat)

    async def desk_text(self, chat, key, data, text):
        D = self.desk
        t = text.strip()
        if key == "Dst":
            self.store.set("media_topics", t[:300])
            return "Збережено", self.desk_settings()
        if key == "Fed":
            D.update_find(data, note="" if t in ("-", "—") else t[:800])
            return "Збережено", self.F_card(data)
        if key == "Tgadd":
            n = 0
            for line in [x.strip() for x in t.split("\n") if x.strip()][:20]:
                D.x("INSERT INTO tags(query) VALUES(?)", (line[:200],))
                n += 1
            return f"Додано тегів: {n}", self.tags_screen()
        if key == "Tged":
            D.x("UPDATE tags SET query=? WHERE id=?", (t[:200], int(data)))
            return "Збережено", self.tag_screen(data)
        if key in ("Tpadd", "Tpe"):
            title, _, kw = t.partition("|")
            if key == "Tpadd":
                if not kw.strip():
                    return "Формат: Назва | слова", self.topics_screen()
                k = "t" + str(int(time.time()))
                D.x("INSERT INTO topics(key,title,keywords,position) VALUES(?,?,?,99)", (k, title.strip()[:40], kw.strip()[:500]))
            else:
                if kw.strip():
                    D.x("UPDATE topics SET title=?, keywords=? WHERE id=?", (title.strip()[:40], kw.strip()[:500], int(data)))
                else:
                    D.x("UPDATE topics SET keywords=? WHERE id=?", (t[:500], int(data)))
            return "Збережено", self.topics_screen()
        if key == "Bka":
            for v in t.split(","):
                D.add_block(data, v)
            return "🚫 Заблоковано", self.blocks_screen()
        if key == "Oua":
            n = 0
            for chunk in [c.strip() for c in t.split("\n---") if c.strip()]:
                first, _, rest = chunk.partition("\n")
                url = first.strip() if first.strip().startswith("http") else ""
                body = rest.strip() if url else chunk
                if D.add_ours("вручну", url, "", body or url):
                    n += 1
            return f"Додано: {n}", self.ours_screen()
        if key == "Wte":
            self.store.set("tpl_" + data, t[:2000])
            return "Шаблон збережено", self.templates_screen()
        return None, self.D_home(chat)
