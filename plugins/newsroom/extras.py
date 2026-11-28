"""📰 Додатково до новини й каналу:
🔍 SEO для сайту — заголовок для пошуку (до 60), опис (до 160), адреса сторінки латиницею (офіційна транслітерація), ключові слова.
🇬🇧 Англійська версія — переклад у новинному стилі без додавання фактів.
📊 Опитування й вікторина до новини — у межах Telegram (питання до 300, варіанти до 100, 2–10 варіантів), у канал або собі.
✏️ Оновити опубліковане — «Оновлено / Виправлено / Спростування» з часом у самому дописі в каналі + 🧾 журнал виправлень.
🔴 Онлайн-трансляція — один допис у каналі, у який бот дописує оновлення з часом (новіші вгорі); повний — продовження новим дописом.
📆 Рік тому ми писали — наші матеріали цієї дати з минулих років (і щоранку, якщо ввімкнути).
📬 Тижнева розсилка — найсильніші матеріали тижня: допис для каналу й HTML-лист для поштових сервісів."""
from __future__ import annotations

import datetime as dt
import html as _html
import logging
import re
import time

from plugins import esc

log = logging.getLogger("orion.newsroom.extras")

# Офіційна транслітерація (постанова КМУ № 55 від 27.01.2010); на початку слова — є, ї, й, ю, я інакше
TR = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i",
      "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
      "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "", "’": "", "ʼ": ""}
TR_START = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def translit(text):
    out, prev_letter = [], False
    low = (text or "").lower().replace("зг", "zgh")
    for ch in low:
        if ch in TR:
            out.append(TR_START[ch] if (ch in TR_START and not prev_letter) else TR[ch])
            prev_letter = True
        else:
            out.append(ch)
            prev_letter = ch.isalpha()
    return "".join(out)


def slugify(text, limit=70):
    s = re.sub(r"[^a-z0-9]+", "-", translit(text)).strip("-")
    if len(s) > limit:
        s = s[:limit].rsplit("-", 1)[0]
    return s


def cut(text, n):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= n:
        return text
    return text[:n - 1].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def poll_ok(p, quiz=False):
    """Перевірка під межі Telegram -> очищене опитування або None."""
    q = cut(p.get("q") or p.get("question"), 300)
    opts = [cut(o, 100) for o in (p.get("options") or []) if str(o).strip()]
    opts = list(dict.fromkeys(opts))[:10]
    if not q or len(opts) < 2:
        return None
    out = {"q": q, "options": opts}
    if quiz:
        try:
            c = int(p.get("correct"))
        except (TypeError, ValueError):
            return None
        if not 0 <= c < len(opts):
            return None
        out["correct"] = c
        out["explanation"] = cut(p.get("explanation"), 200)
    return out


CORR = {"upd": ("✏️ Оновлено", "нові дані"), "fix": ("🛠 Виправлено", "помилка в дописі"), "ref": ("❗️ Спростування", "інформація не підтвердилась")}


class ExtrasUI:
    """Мікс до «Новинарні»."""

    def _now_local(self):
        return dt.datetime.now(self.host.tz) if self.host.tz else dt.datetime.now()

    # ---------------------------------------------------------------- SEO
    async def seo_make(self, did):
        d = self.store.draft(did)
        data, _m = await self.engine._json(
            "Підготуй SEO для новини на сайті. title — заголовок для пошуку до 60 символів (головне слово на початку, без клікбейту); "
            "description — опис до 160 символів; keywords — 5–8 пошукових фраз українською, якими люди шукатимуть цю новину. "
            "Факти — тільки з тексту. Відповідай ЛИШЕ JSON {\"title\": \"…\", \"description\": \"…\", \"keywords\": [\"…\"]}.\n\n"
            f"Заголовок: {d['headline']}\n\n{d['body'][:4000]}", "Ти — SEO-редактор новинного сайту.", "🔍 SEO")
        seo = {"title": cut(data.get("title") or d["headline"], 60), "description": cut(data.get("description"), 160),
               "keywords": [cut(k, 60) for k in (data.get("keywords") or []) if str(k).strip()][:8]}
        seo["slug"] = slugify(seo["title"])
        self.store.set(f"seo:{did}", seo)
        return seo

    def seo_screen(self, did):
        d = self.store.draft(did)
        seo = self.store.get(f"seo:{did}")
        if not seo:
            return (f"🔍 <b>SEO для сайту · #{did}</b>\n\n<b>{esc(d['headline'])}</b>\n\nПідготую заголовок для пошуку (до 60 символів), "
                    "опис (до 160), адресу сторінки латиницею (офіційна транслітерація) і ключові слова.",
                    [[("✨ Підготувати SEO", self.cb("seog", did))], [("« Новина", self.cb("d", did))]])
        L = [f"🔍 <b>SEO для сайту · #{did}</b>", "", f"<b>Заголовок для пошуку</b> · {len(seo['title'])}/60", f"<pre>{esc(seo['title'])}</pre>",
             f"<b>Опис</b> · {len(seo['description'])}/160", f"<pre>{esc(seo['description'])}</pre>",
             "<b>Адреса сторінки</b>", f"<pre>{esc(seo['slug'])}</pre>", "<b>Ключові слова</b>", f"<pre>{esc(', '.join(seo['keywords']))}</pre>",
             "👆 Натисніть на текст у рамці — скопіюється."]
        return "\n".join(L), [[("🔁 Інше SEO", self.cb("seog", did))], [("« Новина", self.cb("d", did))]]

    # ---------------------------------------------------------------- англійська
    async def en_make(self, did):
        d = self.store.draft(did)
        data, _m = await self.engine._json(
            "Translate this Ukrainian news into English in a neutral news style for an international audience. Do not add facts. "
            "Use Ukrainian place names in official transliteration (Kyiv, Lviv, Kharkiv). Reply ONLY JSON {\"headline\": \"…\", \"body\": \"…\"}.\n\n"
            f"Headline: {d['headline']}\n\n{d['body'][:6000]}", "You are a news translator.", "🇬🇧 Англійська версія")
        en = {"headline": str(data.get("headline") or "").strip(), "body": str(data.get("body") or "").strip(), "v": d.get("version")}
        self.store.set(f"en:{did}", en)
        return en

    def en_screen(self, did):
        d = self.store.draft(did)
        en = self.store.get(f"en:{did}")
        if not en or not en.get("body"):
            return (f"🇬🇧 <b>Англійська версія · #{did}</b>\n\nПерекладу новину англійською в новинному стилі — без додавання фактів, "
                    "з офіційними назвами міст (Kyiv, Lviv).", [[("✨ Перекласти", self.cb("eng", did))], [("« Новина", self.cb("d", did))]])
        warn = "\n⚠️ Новину змінено після перекладу — перекладіть ще раз." if en.get("v") != d.get("version") else ""
        return (f"🇬🇧 <b>Англійська версія · #{did}</b>{warn}\n\n<pre>{esc(en['headline'])}\n\n{esc(en['body'])}</pre>\n👆 Натисніть, щоб скопіювати."[:4000],
                [[("🔁 Перекласти ще раз", self.cb("eng", did))], [("« Новина", self.cb("d", did))]])

    # ---------------------------------------------------------------- опитування
    async def poll_make(self, did):
        d = self.store.draft(did)
        data, _m = await self.engine._json(
            "Склади до новини: poll — опитування думки читачів (нейтральне, без маніпуляцій, 3–5 варіантів), і quiz — вікторину "
            "на знання фактів САМЕ з цієї новини (4 варіанти, один правильний, correct — номер з 0, explanation до 200 символів). "
            "Питання до 300 символів, варіанти до 100. Відповідай ЛИШЕ JSON {\"poll\": {\"q\": \"…\", \"options\": [\"…\"]}, "
            "\"quiz\": {\"q\": \"…\", \"options\": [\"…\"], \"correct\": 0, \"explanation\": \"…\"}}.\n\n"
            f"{d['headline']}\n\n{d['body'][:4000]}", "Ти — SMM-редактор новинного видання.", "📊 Опитування")
        res = {"poll": poll_ok(data.get("poll") or {}), "quiz": poll_ok(data.get("quiz") or {}, quiz=True)}
        self.store.set(f"poll:{did}", res)
        return res

    def poll_screen(self, did):
        res = self.store.get(f"poll:{did}")
        if not res:
            return (f"📊 <b>Опитування до новини #{did}</b>\n\nСкладу опитування думки читачів і вікторину на знання фактів із новини.",
                    [[("✨ Скласти", self.cb("plg", did))], [("« Новина", self.cb("d", did))]])
        L = [f"📊 <b>Опитування до новини #{did}</b>", ""]
        rows = []
        if res.get("poll"):
            p = res["poll"]
            L += ["<b>Опитування</b>: " + esc(p["q"])] + [f"  ▫️ {esc(o)}" for o in p["options"]] + [""]
            rows.append([("👁 Опитування мені", self.cb("pls", did, "pm")), ("📤 У канал", self.cb("pls", did, "pc"))])
        if res.get("quiz"):
            q = res["quiz"]
            L += ["<b>Вікторина</b>: " + esc(q["q"])] + [f"  {'✅' if i == q['correct'] else '▫️'} {esc(o)}" for i, o in enumerate(q["options"])]
            if q.get("explanation"):
                L.append(f"  💡 {esc(q['explanation'])}")
            rows.append([("👁 Вікторину мені", self.cb("pls", did, "qm")), ("📤 У канал", self.cb("pls", did, "qc"))])
        if not rows:
            L.append("Не вийшло скласти коректне опитування — спробуйте ще раз.")
        rows += [[("🔁 Інші опитування", self.cb("plg", did))], [("« Новина", self.cb("d", did))]]
        return "\n".join(L)[:4000], rows

    async def poll_send(self, chat, did, how):
        res = self.store.get(f"poll:{did}") or {}
        p = res.get("quiz" if how[0] == "q" else "poll")
        target = self.store.get("publish_chat") if how[1] == "c" else chat
        if not p or not target:
            return "Немає каналу публікацій (📤 Публікація)" if how[1] == "c" else "Немає опитування"
        kw = {"is_anonymous": True}
        if how[0] == "q":
            kw.update(type="quiz", correct_option_id=p["correct"], explanation=p.get("explanation") or None)
        await self.host.bot().send_poll(int(target), p["q"], p["options"], **kw)
        return "📤 Надіслано в канал" if how[1] == "c" else "👁 Надіслано вам"

    # ---------------------------------------------------------------- виправлення опублікованого
    def corr_screen(self, did):
        d = self.store.draft(did)
        if not d or d["status"] != "published" or not d.get("published_msg"):
            return "Оновлювати можна лише опубліковане з бота.", [[("« Новина", self.cb("d", did))]]
        log_ = [c for c in (self.store.get("corr_log") or []) if c["did"] == did]
        L = [f"✏️ <b>Оновити опубліковане · #{did}</b>", f"<b>{esc(d['headline'])}</b>", "",
             "Бот допише в сам допис у каналі позначку з часом — читачі бачать, що й коли змінилось (так чесно й прозоро):"]
        for k, (t, why) in CORR.items():
            L.append(f"{t} — {why}")
        if log_:
            L += ["", "Уже було:"] + [f"• {dt.datetime.fromtimestamp(c['at']):%d.%m %H:%M} {CORR[c['kind']][0]}: {esc(c['text'][:80])}" for c in log_]
        return "\n".join(L), [[(CORR[k][0], self.cb("cri", did, k)) for k in CORR], [("« Новина", self.cb("d", did))]]

    async def corr_apply(self, chat, did, kind, text):
        d = self.store.draft(did)
        note = f"{CORR[kind][0]} {self._now_local():%d.%m %H:%M}: {text.strip()}"
        hist = (self.store.get(f"corr:{did}") or [])
        hist.append(note)
        body = self.post_html(d) + "\n\n" + "\n".join(esc(n) for n in hist)
        try:
            await self.host.bot().edit_message_text(body[:4096], chat_id=int(d["published_chat"]), message_id=int(d["published_msg"]),
                                                    parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ Не вдалося змінити допис: {exc}"
        self.store.set(f"corr:{did}", hist)
        lg = (self.store.get("corr_log") or [])
        lg.append({"did": did, "at": time.time(), "kind": kind, "text": text.strip()[:500], "by": chat})
        self.store.set("corr_log", lg[-300:])
        return "✅ Допис у каналі оновлено"

    def corr_log_screen(self):
        lg = (self.store.get("corr_log") or [])[-20:][::-1]
        L = ["🧾 <b>Журнал виправлень</b>", "", "Усі оновлення, виправлення й спростування опублікованого — для прозорості редакції.", ""]
        for c in lg:
            d = self.store.draft(c["did"]) or {"headline": "(видалено)"}
            L.append(f"• {dt.datetime.fromtimestamp(c['at']):%d.%m %H:%M} · {CORR[c['kind']][0]} · #{c['did']} {esc(d['headline'][:60])}\n   {esc(c['text'][:140])}")
        if not lg:
            L.append("Поки виправлень не було.")
        return "\n".join(L)[:4000], [[("« Інструменти", self.cb("tl"))]]

    # ---------------------------------------------------------------- онлайн-трансляція
    def live(self):
        return self.store.get("live") or {}

    def live_html(self, lv, part):
        ups = [u for u in lv["updates"] if u.get("part", 0) == part][::-1]
        head = (f"🔴 <b>НАЖИВО: {esc(lv['title'])}</b>" if lv.get("on") else f"⚪️ <b>{esc(lv['title'])}</b> · трансляцію завершено")
        if part:
            head += f"\n<i>Продовження · частина {part + 1}</i>"
        return head + "\n\n" + "\n\n".join(f"<b>{u['hm']}</b> {esc(u['text'])}" for u in ups)

    def live_screen(self):
        lv = self.live()
        if not lv.get("on"):
            return ("🔴 <b>Онлайн-трансляція</b>\n\nДля подій, що розвиваються: бот створює в каналі один допис і дописує в нього оновлення з часом "
                    "(новіші вгорі). Коли допис заповниться — продовжить новим із посиланням.\n\n"
                    + ("" if self.store.get("publish_chat") else "⚠️ Спершу підключіть канал: 📤 Публікація.\n")
                    + (f"Остання: «{esc(lv.get('title', ''))}»" if lv.get("title") else ""),
                    [[("▶️ Почати трансляцію", self.cb("lvs"))], [("« Інструменти", self.cb("tl"))]])
        n = len(lv["updates"])
        return (f"🔴 <b>Наживо: {esc(lv['title'])}</b>\n\nОновлень: {n} · дописів у каналі: {len(lv['msgs'])}\n\n"
                + ("Останнє: " + esc(lv["updates"][-1]["text"][:200]) if n else "Оновлень ще немає."),
                [[("➕ Додати оновлення", self.cb("lva"))], [("⏹ Завершити трансляцію", self.cb("lve"))], [("« Інструменти", self.cb("tl"))]])

    async def live_push(self, lv):
        bot, chat = self.host.bot(), int(lv["chat"])
        part = len(lv["msgs"]) - 1
        html = self.live_html(lv, part)
        if len(html) > 3900:  # допис повний — нова частина з посиланням на попередню
            last = lv["updates"].pop()
            last["part"] = part + 1
            lv["updates"].append(last)
            await bot.edit_message_text(self.live_html(lv, part)[:4096], chat_id=chat, message_id=lv["msgs"][-1], parse_mode="HTML",
                                        disable_web_page_preview=True)
            m = await bot.send_message(chat, self.live_html(lv, part + 1), parse_mode="HTML", disable_web_page_preview=True,
                                       reply_to_message_id=lv["msgs"][-1])
            lv["msgs"].append(m.message_id)
        else:
            await bot.edit_message_text(html, chat_id=chat, message_id=lv["msgs"][-1], parse_mode="HTML", disable_web_page_preview=True)
        self.store.set("live", lv)

    # ---------------------------------------------------------------- рік тому
    def yago_rows(self, day=None):
        day = day or self._now_local().date()
        md = day.strftime("%m-%d")
        rows = [dict(r) for r in self.desk.q("SELECT * FROM ours_posts WHERE strftime('%m-%d', at, 'unixepoch', 'localtime')=? "
                                              "AND strftime('%Y', at, 'unixepoch', 'localtime')<? ORDER BY at DESC LIMIT 60", (md, str(day.year)))]
        return rows

    def yago_text(self):
        rows = self.yago_rows()
        today = self._now_local().date()
        L = [f"📆 <b>Рік тому ми писали</b> · {today:%d.%m}", ""]
        if not rows:
            L.append("Цієї дати в минулі роки наших публікацій у базі немає.")
        by = {}
        for r in rows:
            by.setdefault(dt.datetime.fromtimestamp(r["at"]).year, []).append(r)
        for y in sorted(by, reverse=True):
            L.append(f"<b>{y}</b> · {today.year - y} р. тому")
            for r in by[y][:6]:
                t = esc((r["title"] or r["text"].split("\n")[0])[:100])
                L.append("• " + (f"<a href=\"{esc(r['url'])}\">{t}</a>" if r["url"] else t) + (f" · 👁 {r['views']}" if r.get("views") else ""))
            L.append("")
        return "\n".join(L)[:4000]

    def yago_screen(self):
        on = bool(self.store.get("yago_auto"))
        return (self.yago_text() + f"\n📬 Щоранку о 10:00: {'так' if on else 'ні'}",
                [[("📬 Щоранку: " + ("так" if on else "ні"), self.cb("yagoa"))], [("« Інструменти", self.cb("tl"))]])

    # ---------------------------------------------------------------- розсилка
    def nl_build(self):
        from .weekstats import week_summary
        now = time.time()
        cur = self.ws_posts(now - 7 * 86400, now)
        s = week_summary(cur, [])
        top = [p for p, _x in s["top"]] or sorted(cur, key=lambda p: -(p.get("views") or 0))[:5]
        title = f"Головне за тиждень · {dt.datetime.fromtimestamp(now - 6 * 86400):%d.%m}–{dt.datetime.fromtimestamp(now):%d.%m}"
        tg = [f"📬 <b>{esc(title)}</b>", ""]
        mail = [f"<h2 style=\"font-family:Georgia,serif;color:#102A31\">{_html.escape(title)}</h2>"]
        for i, p in enumerate(top[:7], 1):
            t = (p["title"] or p["text"].split("\n")[0])[:140]
            tg.append(f"{i}. " + (f"<a href=\"{esc(p['url'])}\">{esc(t)}</a>" if p["url"] else esc(t)))
            mail.append(f"<p style=\"font-family:Georgia,serif;font-size:17px\"><b>{i}.</b> "
                        + (f"<a href=\"{_html.escape(p['url'])}\" style=\"color:#256B4A\">{_html.escape(t)}</a>" if p["url"] else _html.escape(t)) + "</p>")
        sig = self.store.get("signature") or ""
        if sig:
            tg += ["", esc(sig)]
            mail.append(f"<p style=\"color:#556\">{_html.escape(sig)}</p>")
        page = ("<!DOCTYPE html><html lang=\"uk\"><head><meta charset=\"utf-8\"><title>" + _html.escape(title) + "</title></head>"
                "<body style=\"background:#D0DCCE;padding:24px\"><div style=\"max-width:600px;margin:auto;background:#fff;padding:24px;border-radius:12px\">"
                + "".join(mail) + "</div></body></html>")
        return title, "\n".join(tg), page, len(top)

    def nl_screen(self):
        title, tg, _page, n = self.nl_build()
        on = bool(self.store.get("nl_auto"))
        return ((tg if n else "📬 <b>Тижнева розсилка</b>\n\nЗа тиждень наших публікацій у базі немає.") +
                f"\n\n— — —\nНайсильніші за 7 днів (як у «📈 Статистиці»). 📬 Щоп'ятниці о 17:00: {'так' if on else 'ні'}",
                [[("📤 У канал", self.cb("nlc")), ("📄 HTML-лист", self.cb("nlh"))],
                 [("📬 Щоп'ятниці: " + ("так" if on else "ні"), self.cb("nla"))], [("« Інструменти", self.cb("tl"))]])

    # ---------------------------------------------------------------- маршрути
    async def _gen(self, chat, make, screen, label):
        """ШІ-генерація: у застосунку — чекаємо й показуємо на тому ж екрані; у чаті — у фоні, результат окремим повідомленням
        (щоб кнопка не «висіла»)."""
        try:
            import miniapp
            in_app = miniapp.CAPTURE.get() is not None
        except Exception:  # noqa: BLE001
            in_app = False
        if in_app:
            try:
                await make()
            except Exception as exc:  # noqa: BLE001
                return f"⚠️ {str(exc)[:150]}", screen()
            return "✨ Готово", screen()

        async def go():
            try:
                await make()
                await self.host.notify(chat, *screen())
            except Exception as exc:  # noqa: BLE001
                await self.host.notify(chat, f"⚠️ {esc(label)}: не вдалося — {esc(str(exc))[:200]}")
        self._bg(go())
        return "✨ Готую…", (f"✨ <b>{esc(label)}</b> — готую, прийде окремим повідомленням за хвилину.", screen()[1])

    async def extras_route(self, chat, a, x, y):
        did = int(x) if (x or "").isdigit() else 0
        if a == "seo":
            return None, self.seo_screen(did)
        if a == "seog":
            return await self._gen(chat, lambda: self.seo_make(did), lambda: self.seo_screen(did), "SEO для сайту")
        if a == "en":
            return None, self.en_screen(did)
        if a == "eng":
            return await self._gen(chat, lambda: self.en_make(did), lambda: self.en_screen(did), "Англійська версія")
        if a == "pll":
            return None, self.poll_screen(did)
        if a == "plg":
            return await self._gen(chat, lambda: self.poll_make(did), lambda: self.poll_screen(did), "Опитування")
        if a == "pls":
            return await self.poll_send(chat, did, y), self.poll_screen(did)
        if a == "cr":
            return None, self.corr_screen(did)
        if a == "cri":
            self.ask(chat, "cri", f"{did}.{y}")
            return None, (f"{CORR[y][0]}: напишіть, що саме змінилося (коротко, для читачів).", [[("✖️ Скасувати", "m:cancel")]])
        if a == "crl":
            return None, self.corr_log_screen()
        if a == "lv":
            return None, self.live_screen()
        if a == "lvs":
            if not self.store.get("publish_chat"):
                return "Спершу підключіть канал", self.live_screen()
            self.ask(chat, "lvs")
            return None, ("🔴 Назва трансляції — напр. «Обстріл Києва 28 листопада: що відомо».", [[("✖️ Скасувати", "m:cancel")]])
        if a == "lva":
            self.ask(chat, "lva")
            return None, ("➕ Текст оновлення (час бот поставить сам).", [[("✖️ Скасувати", "m:cancel")]])
        if a == "lve":
            lv = self.live()
            if lv.get("on"):
                lv["on"] = False
                try:
                    await self.live_push(lv)
                except Exception as exc:  # noqa: BLE001
                    log.info("live end: %s", exc)
                self.store.set("live", lv)
            return "⏹ Завершено", self.live_screen()
        if a == "yago":
            return None, self.yago_screen()
        if a == "yagoa":
            self.store.set("yago_auto", not self.store.get("yago_auto"))
            return None, self.yago_screen()
        if a == "nl":
            return None, self.nl_screen()
        if a == "nla":
            self.store.set("nl_auto", not self.store.get("nl_auto"))
            return None, self.nl_screen()
        if a == "nlc":
            title, tg, _p, n = self.nl_build()
            ch = self.store.get("publish_chat")
            if not ch or not n:
                return "Немає каналу або матеріалів", self.nl_screen()
            await self.host.bot().send_message(int(ch), tg[:4096], parse_mode="HTML", disable_web_page_preview=True)
            return "📤 Розсилку опубліковано", self.nl_screen()
        if a == "nlh":
            title, _tg, page, _n = self.nl_build()
            from telegram import InputFile
            await self.host.bot().send_document(chat, InputFile(page.encode("utf-8"), filename="rozsylka.html"),
                                                caption="📄 HTML-лист — вставте в поштовий сервіс (Mailchimp, Sendpulse, Gmail)")
            return "📄 Надіслано", self.nl_screen()
        return None, self.tools_screen()

    async def extras_text(self, chat, key, data, text):
        if key == "cri":
            did, kind = str(data).split(".")
            return await self.corr_apply(chat, int(did), kind, text), self.corr_screen(int(did))
        if key == "lvs":
            title = text.strip()[:200]
            m = await self.host.bot().send_message(int(self.store.get("publish_chat")), f"🔴 <b>НАЖИВО: {esc(title)}</b>\n\nСтежте за оновленнями тут.",
                                                   parse_mode="HTML")
            self.store.set("live", {"on": True, "title": title, "chat": str(self.store.get("publish_chat")), "msgs": [m.message_id],
                                    "updates": [], "at": time.time()})
            return "🔴 Трансляцію почато", self.live_screen()
        if key == "lva":
            lv = self.live()
            if not lv.get("on"):
                return "Трансляція не йде", self.live_screen()
            lv["updates"].append({"hm": self._now_local().strftime("%H:%M"), "text": text.strip()[:1500], "part": len(lv["msgs"]) - 1})
            try:
                await self.live_push(lv)
            except Exception as exc:  # noqa: BLE001
                return f"⚠️ {exc}", self.live_screen()
            return "➕ Додано", self.live_screen()
        return None, self.tools_screen()

    async def extras_tick(self):
        """Щоранку «рік тому», щоп'ятниці розсилка — якщо ввімкнено."""
        now = self._now_local()
        if self.store.get("yago_auto") and now.hour == 10 and self.store.get("yago_sent") != now.date().isoformat():
            self.store.set("yago_sent", now.date().isoformat())
            if self.yago_rows():
                await self.host.notify_admins(self.yago_text(), [[("📆 Рік тому", self.cb("yago"))]])
        if self.store.get("nl_auto") and now.weekday() == 4 and now.hour == 17 and self.store.get("nl_sent") != now.date().isoformat():
            self.store.set("nl_sent", now.date().isoformat())
            title, tg, _p, n = self.nl_build()
            if n:
                await self.host.notify_admins(tg + "\n\n— чернетка розсилки; опублікувати — кнопкою.", [[("📬 Розсилка", self.cb("nl"))]])
