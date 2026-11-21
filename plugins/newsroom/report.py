"""📊 Звіт за місяць: що з наших тем «заходить», чого замало, які області забуваємо — і закріплення «де просідаємо».

Без ШІ: теми — за ключовими словами новинарні (🧭 Теми), області — за назвами міст і областей,
охоплення — перегляди, реакції, поширення, коментарі з наших каналів і соцмереж, відгомін — цитування,
репости й плагіат наших дописів на чужих ресурсах.

Індекс допису = (перегляди/100 + реакції + 2·коментарі + 3·поширення + 5·відгомін) / медіана того ж показника на тій
самій платформі за місяць. 1.0 — звичайний допис; 2.0 — удвічі сильніший. Так Telegram-перегляди не «задавлюють» Instagram-лайки.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import re
import statistics
import time

from .desk import docx_bytes
from .echo import KINDS as ECHO_KINDS

log = logging.getLogger("orion.newsroom.report")

REGIONS = [
    ("Київ", r"(?<![а-яіїєґ])(київ(?!ськ|щин)|києв|столиц[яіюе])"),
    ("Київська", r"київськ\w* обл|київщин|ірпін|(?<![а-яіїєґ])буч[аіу](?![а-яіїєґ])|бровар|біл\w+ церкв|бориспол|обухів|вишгород|фастів"),
    ("Вінницька", r"вінниц|вінниччин"),
    ("Волинська", r"волин|луцьк|ковел"),
    ("Дніпропетровська", r"дніпропетровськ|дніпровщин|(?<![а-яіїєґ])дніпр[іоау]?(?![а-яіїєґ])|кривий ріг|кривого рогу|кривому розі|нікопол|павлоград|марганц"),
    ("Донецька", r"донеч|донецьк|краматорськ|слов.янськ|покровськ|бахмут|маріупол|авдіїв|костянтинів|дружків"),
    ("Житомирська", r"житомир|бердичів|коростен"),
    ("Закарпатська", r"закарпат|ужгород|мукачев|берегов|хуст"),
    ("Запорізька", r"запоріж|запорізьк|мелітопол|енергодар|бердянськ|токмак"),
    ("Івано-Франківська", r"івано-франків|франківськ|прикарпат|калуш|коломи"),
    ("Кіровоградська", r"кіровоград|кропивницьк|олександрі"),
    ("Луганська", r"луган|сєвєродонецьк|лисичанськ|рубіжн"),
    ("Львівська", r"львів|львов|дрогобич|стрий|червоноград|шептицьк"),
    ("Миколаївська", r"миколаїв|миколаєв|первомайськ|вознесенськ"),
    ("Одеська", r"одес|одещин|ізмаїл|білгород-дністров|чорноморськ"),
    ("Полтавська", r"полтав|кременчу|миргород|лубн"),
    ("Рівненська", r"(?<![а-яіїєґ])рівн(е|ого|ому)(?![а-яіїєґ])|рівненськ|рівненщин|вараш|дубн"),
    ("Сумська", r"сумськ|сумщин|(?<![а-яіїєґ])(у|в|м\.) сумах|охтир|шостк|конотоп|ромн"),
    ("Тернопільська", r"тернопіл|чортків|кременец"),
    ("Харківська", r"харків|харков|куп.янськ|ізюм|чугуїв|лозов|балакле"),
    ("Херсонська", r"херсон|каховк|олешк|скадовськ"),
    ("Хмельницька", r"хмельниц|кам.янець-поділ|шепетів|нетішин"),
    ("Черкаська", r"черкас|(?<![а-яіїєґ])уман|сміл"),
    ("Чернівецька", r"чернівц|буковин"),
    ("Чернігівська", r"черніг|ніжин|прилук"),
    ("АР Крим і Севастополь", r"(?<![а-яіїєґ])крим|севастопол|сімферопол|ялт[аиі]|керч|джанко"),
]
REGIONS_RE = [(n, re.compile(rx)) for n, rx in REGIONS]
PLAT = {"tg": "✈️ Telegram", "x": "𝕏 X", "threads": "🧵 Threads", "ig": "📸 Instagram", "fb": "📘 Facebook", "tumblr": "🌀 Tumblr",
        "yt": "▶️ YouTube", "rss": "🌐 Сайт"}
MONTHS = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def low(t):
    return (t or "").lower().replace("’", "'").replace("ʼ", "'").replace("`", "'")


def regions_of(text):
    t = low(text)
    return [n for n, rx in REGIONS_RE if rx.search(t)]


def raw_score(p, echo_n=0):
    return (p.get("views") or 0) / 100 + (p.get("likes") or 0) + 2 * (p.get("comments") or 0) + 3 * (p.get("reposts") or 0) + 5 * echo_n


def month_bounds(ym, tz=None):
    y, m = map(int, ym.split("-"))
    a = dt.datetime(y, m, 1, tzinfo=tz)
    b = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=tz)
    return a.timestamp(), b.timestamp()


def shift(ym, k):
    y, m = map(int, ym.split("-"))
    m += k
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def month_name(ym):
    y, m = map(int, ym.split("-"))
    return f"{MONTHS[m - 1]} {y}"


def analyze(posts, echoes, topics, matches, prev_posts=None):
    """Чиста функція (для тестів): дописи місяця, відгомін, теми -> словник звіту."""
    echo_by = {}
    for e in echoes:
        if e.get("ours_id"):
            echo_by.setdefault(e["ours_id"], []).append(e)
    by_plat = {}
    for p in posts:
        p["_raw"] = raw_score(p, len(echo_by.get(p["id"], [])))
        by_plat.setdefault(p.get("platform") or "tg", []).append(p)
    for plat, ps in by_plat.items():
        med = statistics.median([x["_raw"] for x in ps]) or 1.0
        for x in ps:
            x["_idx"] = round(x["_raw"] / med, 2) if med else 1.0
    total = len(posts) or 1
    # ---- теми
    trows = []
    for t in topics:
        ps = [p for p in posts if matches(t, (p.get("title") or "") + "\n" + p["text"])]
        idx = statistics.mean([p["_idx"] for p in ps]) if ps else 0
        ech = sum(len(echo_by.get(p["id"], [])) for p in ps)
        trows.append({"key": t["key"], "title": t["title"], "n": len(ps), "share": len(ps) / total, "idx": round(idx, 2),
                      "views": sum(p.get("views") or 0 for p in ps), "echo": ech})
    even = 1 / max(1, len(topics))
    for r in trows:
        if r["n"] == 0:
            r["verdict"] = "none"
        elif r["share"] < even * 0.5 and r["idx"] >= 1.1:
            r["verdict"] = "more"          # рідко, а заходить — давати частіше
        elif r["share"] < even * 0.5:
            r["verdict"] = "rare"
        elif r["share"] > even * 2 and r["idx"] < 0.8:
            r["verdict"] = "less"          # дуже часто, а заходить слабко
        else:
            r["verdict"] = "ok"
    # ---- області
    reg = {n: {"n": 0, "idx": []} for n, _ in REGIONS}
    for p in posts:
        for n in regions_of((p.get("title") or "") + "\n" + p["text"]):
            reg[n]["n"] += 1
            reg[n]["idx"].append(p["_idx"])
    counts = [v["n"] for v in reg.values()]
    mean = statistics.mean(counts) if counts else 0
    rrows = []
    for n, v in reg.items():
        verdict = "none" if v["n"] == 0 else "much" if mean and v["n"] > 2.5 * mean and v["n"] >= 5 else \
            "little" if mean and v["n"] < 0.35 * mean else "ok"
        rrows.append({"name": n, "n": v["n"], "idx": round(statistics.mean(v["idx"]), 2) if v["idx"] else 0, "verdict": verdict})
    rrows.sort(key=lambda r: -r["n"])
    # ---- відгомін
    ek = {}
    who = {}
    for e in echoes:
        ek[e["kind"]] = ek.get(e["kind"], 0) + 1
        if e.get("source"):
            who[e["source"]] = who.get(e["source"], 0) + 1
    # ---- платформи, час
    prow = []
    for plat, ps in sorted(by_plat.items(), key=lambda kv: -len(kv[1])):
        prow.append({"plat": plat, "n": len(ps), "views": sum(p.get("views") or 0 for p in ps),
                     "inter": sum((p.get("likes") or 0) + (p.get("comments") or 0) + (p.get("reposts") or 0) for p in ps)})
    wd, hr = {}, {}
    for p in posts:
        lt = time.localtime(p["at"])
        wd.setdefault(lt.tm_wday, []).append(p["_idx"])
        hr.setdefault(lt.tm_hour // 3 * 3, []).append(p["_idx"])
    best_wd = max(((statistics.mean(v), k) for k, v in wd.items() if len(v) >= 3), default=None)
    best_hr = max(((statistics.mean(v), k) for k, v in hr.items() if len(v) >= 3), default=None)
    top = sorted(posts, key=lambda p: -p["_idx"])[:5]
    no_metrics = sum(1 for p in posts if not any(p.get(k) for k in ("views", "likes", "reposts", "comments")))
    return {"total": len(posts), "topics": trows, "regions": rrows, "echo": ek, "echo_total": len(echoes),
            "who": sorted(who.items(), key=lambda kv: -kv[1])[:5], "platforms": prow, "top": top,
            "best_wd": best_wd, "best_hr": best_hr, "no_metrics": no_metrics,
            "untopic": sum(1 for p in posts if not any(matches(t, (p.get('title') or '') + '\n' + p['text']) for t in topics)),
            "prev_total": len(prev_posts) if prev_posts is not None else None,
            "prev_views": sum(p.get("views") or 0 for p in prev_posts) if prev_posts else None,
            "views": sum(p.get("views") or 0 for p in posts)}


def gaps_of(r):
    """Усе «рідкісне» зі звіту — те, що можна закріпити зверху. -> [(id, короткий текст)]"""
    out = []
    if not r["total"]:
        return out
    for t in r["topics"]:
        if t["verdict"] == "more":
            out.append((f"t:{t['key']}", f"{t['title']} — рідко ({t['n']}), а заходить ×{t['idx']}"))
        elif t["verdict"] == "none":
            out.append((f"t:{t['key']}", f"{t['title']} — жодного допису"))
        elif t["verdict"] == "rare":
            out.append((f"t:{t['key']}", f"{t['title']} — лише {t['n']}"))
    for g in r["regions"]:
        if g["verdict"] == "none":
            out.append((f"r:{g['name']}", f"🗺 {g['name']} — жодного допису"))
        elif g["verdict"] == "little":
            out.append((f"r:{g['name']}", f"🗺 {g['name']} — лише {g['n']}"))
    return out


class ReportUI:
    # ---------------------------------------------------------------- дані
    def report_month(self, ym=None):
        tz = self.host.tz
        now = dt.datetime.now(tz) if tz else dt.datetime.now()
        ym = ym or shift(now.strftime("%Y-%m"), -1)
        a, b = month_bounds(ym, tz)
        posts = self.desk.q("SELECT * FROM ours_posts WHERE at>=? AND at<? ORDER BY at", (a, b))
        pa, pb = month_bounds(shift(ym, -1), tz)
        prev = self.desk.q("SELECT id, views FROM ours_posts WHERE at>=? AND at<?", (pa, pb))
        try:
            echoes = self.desk.q("SELECT * FROM echoes WHERE at>=? AND at<?", (a, b))
        except Exception:  # noqa: BLE001
            echoes = []
        r = analyze(posts, echoes, self.desk.topics(), self.desk.matches_topic, prev)
        r["ym"] = ym
        r["partial"] = now.strftime("%Y-%m") == ym
        return r

    # ---------------------------------------------------------------- текст
    def report_text(self, r, full=False):
        from plugins import esc
        V = {"more": "⬆️ давати частіше", "rare": "▫️ рідко", "none": "⛔️ не було", "less": "⬇️ забагато, слабко заходить", "ok": "✅"}
        L = [f"📊 <b>Звіт за {month_name(r['ym'])}</b>" + (" (місяць ще триває)" if r["partial"] else ""), ""]
        if not r["total"]:
            L += ["Наших публікацій за цей місяць у базі немає.",
                  "Додайте наші канали й соцмережі в «🏠 Наші ресурси» — бот почне рахувати охоплення й відгомін."]
            return "\n".join(L)
        dv = ""
        if r["prev_total"]:
            d = (r["total"] - r["prev_total"]) / r["prev_total"] * 100
            dv = f" ({'+' if d >= 0 else ''}{d:.0f}% до попереднього)"
        L.append(f"Публікацій: <b>{r['total']}</b>{dv} · переглядів: <b>{r['views']:,}</b>".replace(",", " "))
        if r["platforms"]:
            L.append(" · ".join(f"{PLAT.get(p['plat'], p['plat'])} {p['n']}" for p in r["platforms"]))
        if r["no_metrics"]:
            L.append(f"ℹ️ Без цифр охоплення: {r['no_metrics']} (для них потрібні ключі API — 🌐 Соцмережі)")
        L += ["", "🔥 <b>Найпопулярніші теми</b> (індекс 1.0 — звичайний допис)"]
        hot = sorted([t for t in r["topics"] if t["n"] >= 2], key=lambda t: -t["idx"])[:5]
        L += [f"• {esc(t['title'])} — ×{t['idx']} · {t['n']} доп." + (f" · 📣 {t['echo']}" if t["echo"] else "") for t in hot] or ["— замало даних"]
        L += ["", "🧭 <b>Теми: частота й віддача</b>"]
        for t in sorted(r["topics"], key=lambda t: ("more", "none", "rare", "less", "ok").index(t["verdict"])):
            L.append(f"• {esc(t['title'])}: {t['n']} ({t['share'] * 100:.0f}%)" + (f", ×{t['idx']}" if t["n"] else "") + f" — {V[t['verdict']]}")
        if r["untopic"]:
            L.append(f"• Поза темами: {r['untopic']} — додайте ключові слова в 🧭 Теми, щоб бачити точніше")
        much = [g for g in r["regions"] if g["verdict"] == "much"]
        little = [g for g in r["regions"] if g["verdict"] == "little"]
        none = [g for g in r["regions"] if g["verdict"] == "none"]
        L += ["", "🗺 <b>Області</b>"]
        top_r = [g for g in r["regions"] if g["n"]][:6]
        L.append("Найчастіше: " + (", ".join(f"{g['name']} {g['n']}" for g in top_r) or "—"))
        if much:
            L.append("⬇️ Забагато: " + ", ".join(f"{g['name']} ({g['n']})" for g in much))
        if little:
            L.append("⬆️ Замало: " + ", ".join(f"{g['name']} ({g['n']})" for g in little))
        if none:
            L.append("⛔️ Жодного допису: " + ", ".join(g["name"] for g in none))
        strong = [g for g in r["regions"] if g["n"] >= 2 and g["idx"] >= 1.3 and g["verdict"] in ("little", "ok")]
        if strong:
            L.append("💡 Заходять краще за середнє: " + ", ".join(f"{g['name']} ×{g['idx']}" for g in strong[:4]))
        L += ["", f"📣 <b>Відгомін на чужих ресурсах</b>: {r['echo_total']}"]
        if r["echo"]:
            L.append(" · ".join(f"{ECHO_KINDS.get(k, ('', k))[0]} {ECHO_KINDS.get(k, ('', k))[1].split(' (')[0].lower()} {n}" for k, n in r["echo"].items()))
        if r["who"]:
            L.append("Найчастіше нас підхоплюють: " + ", ".join(f"{esc(w)} ({n})" for w, n in r["who"]))
        if r["echo"].get("plagiarism"):
            L.append(f"🚩 Плагіат (майже дослівно без посилання): {r['echo']['plagiarism']} — див. 📣 Відгомін")
        if r["best_wd"] or r["best_hr"]:
            L += ["", "⏰ <b>Коли заходить краще</b>: "
                  + ", ".join(x for x in ((f"{WEEKDAYS[r['best_wd'][1]]} (×{r['best_wd'][0]:.2f})" if r["best_wd"] else ""),
                                         (f"{r['best_hr'][1]:02d}:00–{r['best_hr'][1] + 3:02d}:00 (×{r['best_hr'][0]:.2f})" if r["best_hr"] else "")) if x)]
        if full and r["top"]:
            L += ["", "🏆 <b>Найсильніші дописи</b>"]
            for p in r["top"]:
                L.append(f"• ×{p['_idx']} {esc((p.get('title') or p['text'])[:90])}" + (f"\n  {p['url']}" if p.get("url") else ""))
        g = gaps_of(r)
        if g:
            L += ["", f"📌 Рідкісного: {len(g)} — можна закріпити зверху, щоб завжди бачити, де просідаємо."]
        return "\n".join(L)

    # ---------------------------------------------------------------- екрани
    def report_screen(self, chat, ym=None):
        r = self.report_month(ym)
        text = self.report_text(r)
        ym = r["ym"]
        tz = self.host.tz
        cur = (dt.datetime.now(tz) if tz else dt.datetime.now()).strftime("%Y-%m")
        oldest = self.desk.q("SELECT MIN(at) AS a FROM ours_posts")[0]["a"]
        nav = []
        if oldest and oldest < month_bounds(ym, tz)[0]:
            nav.append(("◀️ " + month_name(shift(ym, -1)).split()[0], self.cb("rp", shift(ym, -1))))
        if ym < cur:
            nav.append((month_name(shift(ym, 1)).split()[0] + " ▶️", self.cb("rp", shift(ym, 1))))
        pinned = self.store.get("gaps_pinned") or {}
        rows = [nav] if nav else []
        if gaps_of(r):
            rows.append([("📌 Закріпити рідкісне", self.cb("rpsel", ym))])
        if pinned.get("items"):
            rows.append([("📍 Що закріплено", self.cb("rpshow"))])
        auto = self.store.get("report_auto") is not False
        rows += [[("📄 Повний звіт (Word)", self.cb("rgo", ym)), (("🔔" if auto else "🔕") + " Щомісяця 1-го", self.cb("rpa"))],
                 [("« Наші ресурси", self.cb("Ou"))]]
        return text[:4000], rows

    def report_pick_screen(self, chat, ym):
        r = self.report_month(ym)
        gaps = gaps_of(r)
        sel = set(self.store.get(f"gap_sel_{chat}") or [g for g, _ in gaps])
        rows = [[(("✅ " if gid in sel else "▫️ ") + txt[:56], self.cb("rpt", ym, gid[:40]))] for gid, txt in gaps[:20]]
        rows.append([("📌 У головному меню", self.cb("rpin", ym, "menu")), ("📍 Повідомленням у чаті", self.cb("rpin", ym, "msg"))])
        rows.append([("« Звіт", self.cb("rp", ym))])
        return ("📌 <b>Що закріпити зверху?</b>\n\nПозначте пункти. «У головному меню» — рядок над кнопками Оріона й новинарні; "
                "«Повідомленням у чаті» — закріплене повідомлення вгорі цього чату, бот сам оновлюватиме його щомісяця."), rows

    def pinned_lines(self):
        p = self.store.get("gaps_pinned") or {}
        return p.get("items") or []

    def banner(self, chat):
        """Рядок «де просідаємо» над головним меню (лише для адміністраторів)."""
        items = self.pinned_lines()
        if not items or not self.host.is_admin(chat) or not (self.store.get("gaps_pinned") or {}).get("menu", True):
            return ""
        from plugins import esc
        p = self.store.get("gaps_pinned")
        head = f"📌 <b>Де просідаємо</b> ({month_name(p['ym'])})"
        topics = [x for x in items if not x.startswith("🗺")]
        none = [x[2:].split(" — ")[0] for x in items if x.startswith("🗺") and "жодного" in x]
        little = [x[2:].split(" — ")[0] for x in items if x.startswith("🗺") and "жодного" not in x]
        out = ["• " + esc(x) for x in topics[:3]] + ([f"• …і ще тем: {len(topics) - 3}"] if len(topics) > 3 else [])
        if little:
            out.append("• 🗺 Замало: " + esc(", ".join(little[:4])) + (f" +{len(little) - 4}" if len(little) > 4 else ""))
        if none:
            out.append("• 🗺 Жодного допису: " + esc(", ".join(none[:3])) + (f" +{len(none) - 3}" if len(none) > 3 else ""))
        return head + "\n" + "\n".join(out)

    async def pin_gaps(self, chat, ym, how):
        r = self.report_month(ym)
        gaps = gaps_of(r)
        sel = set(self.store.get(f"gap_sel_{chat}") or [g for g, _ in gaps])
        items = [t for g, t in gaps if g in sel]
        p = self.store.get("gaps_pinned") or {}
        p.update({"ym": ym, "items": items, "at": time.time(), "auto": True})
        if how == "menu":
            p["menu"] = True
        if how == "msg":
            p["msg_chat"] = chat
            await self._pin_message(p)
        self.store.set("gaps_pinned", p)
        return len(items)

    async def _pin_message(self, p):
        bot = self.host.bot()
        if not bot or not p.get("msg_chat"):
            return
        text = f"📌 Де просідаємо ({month_name(p['ym'])}):\n" + "\n".join("• " + x for x in p["items"][:25])
        mid = p.get("msg_id")
        try:
            if mid:
                await bot.edit_message_text(text[:4000], chat_id=p["msg_chat"], message_id=mid)
                return
        except Exception:  # noqa: BLE001
            mid = None
        m = await bot.send_message(p["msg_chat"], text[:4000])
        p["msg_id"] = m.message_id
        try:
            await bot.pin_chat_message(p["msg_chat"], m.message_id, disable_notification=True)
        except Exception as exc:  # noqa: BLE001
            log.info("pin: %s", exc)

    async def unpin_gaps(self):
        p = self.store.get("gaps_pinned") or {}
        bot = self.host.bot()
        if bot and p.get("msg_id"):
            try:
                await bot.unpin_chat_message(p["msg_chat"], message_id=p["msg_id"])
            except Exception:  # noqa: BLE001
                pass
        self.store.set("gaps_pinned", {})

    def report_docx(self, r):
        blocks = [("h1", f"Звіт за {month_name(r['ym'])}")]
        for para in re.sub(r"<[^>]+>", "", self.report_text(r, full=True)).split("\n\n")[1:]:
            lines = para.split("\n")
            blocks.append(("h2", lines[0]))
            blocks += [("p", x) for x in lines[1:] if x.strip()]
        blocks.append(("small", "Індекс 1.0 — звичайний допис платформи за місяць. Теми — за ключовими словами 🧭 Тем, області — за назвами міст і областей."))
        return docx_bytes([("Звіт", blocks)])

    async def send_report(self, chat, ym=None, file=True):
        r = self.report_month(ym)
        text, rows = self.report_screen(chat, r["ym"])
        await self.host.notify(chat, text, rows)
        bot = self.host.bot()
        if file and bot and r["total"]:
            from telegram import InputFile
            await bot.send_document(chat, InputFile(io.BytesIO(self.report_docx(r)), filename=f"zvit_{r['ym']}.docx"),
                                    caption=f"📄 Звіт за {month_name(r['ym'])} — повна версія")

    async def _report_loop(self):
        __import__("notify").KIND.set("news")  # 🔔 тип сповіщень для режимів людини
        await asyncio.sleep(120)
        while True:
            try:
                tz = self.host.tz
                now = dt.datetime.now(tz) if tz else dt.datetime.now()
                ym = shift(now.strftime("%Y-%m"), -1)
                if now.day == 1 and now.hour >= 9 and self.store.get("report_sent") != ym and self.store.get("report_auto") is not False:
                    self.store.set("report_sent", ym)
                    for uid in self.host.admins():
                        await self.send_report(uid, ym)
                    p = self.store.get("gaps_pinned") or {}
                    if p.get("items") and p.get("auto", True):       # закріплене оновлюється новим місяцем
                        p.update({"ym": ym, "items": [t for _g, t in gaps_of(self.report_month(ym))], "at": time.time()})
                        self.store.set("gaps_pinned", p)
                        if p.get("msg_id"):
                            await self._pin_message(p)
                            self.store.set("gaps_pinned", p)
            except Exception:  # noqa: BLE001
                log.exception("report loop")
            await asyncio.sleep(1800)

    async def report_route(self, chat, a, x, y):
        if a == "rp":
            return None, self.report_screen(chat, x)
        if a == "rpsel":
            self.store.set(f"gap_sel_{chat}", None)
            return None, self.report_pick_screen(chat, x)
        if a == "rpt":
            r = self.report_month(x)
            allg = [g for g, _ in gaps_of(r)]
            sel = set(self.store.get(f"gap_sel_{chat}") or allg)
            gid = next((g for g in allg if g[:40] == y), y)
            sel ^= {gid}
            self.store.set(f"gap_sel_{chat}", [g for g in allg if g in sel])
            return None, self.report_pick_screen(chat, x)
        if a == "rpin":
            n = await self.pin_gaps(chat, x, y)
            return f"📌 Закріплено: {n}", self.report_screen(chat, x)
        if a == "rpshow":
            items = self.pinned_lines()
            p = self.store.get("gaps_pinned") or {}
            txt = (f"📍 <b>Закріплено</b> ({month_name(p['ym'])})\n\n" + "\n".join("• " + i for i in items)) if items else "Нічого не закріплено."
            auto = p.get("auto", True)
            return None, (txt, [[(("🔁" if auto else "⏸") + " Оновлювати щомісяця", self.cb("rpau"))],
                                [("🧹 Відкріпити", self.cb("rpun")), ("« Звіт", self.cb("rp"))]])
        if a == "rpau":
            p = self.store.get("gaps_pinned") or {}
            p["auto"] = not p.get("auto", True)
            self.store.set("gaps_pinned", p)
            return None, (await self.report_route(chat, "rpshow", None, None))[1]
        if a == "rpun":
            await self.unpin_gaps()
            return "Відкріплено", self.report_screen(chat)
        if a == "rpa":
            self.store.set("report_auto", self.store.get("report_auto") is False)
            return None, self.report_screen(chat)
        if a == "rgo":
            self._bg(self.send_report(chat, x))
            return "📄 Готую файл…", self.report_screen(chat, x)
        return None, self.report_screen(chat)
