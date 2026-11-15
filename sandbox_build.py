"""Збирає 🧪 пісочницю зі справжнього коду бота на демо-даних.

    python sandbox_build.py
Результат: simulations/sandbox_screens.json (для чату) і simulations/sandbox.html (Mini App / файл).
Нічого не пише в робочу базу: усе відбувається в тимчасовій теці.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="orion_sandbox_")
_out = os.environ.get("SANDBOX_OUT")
os.environ.update(TELEGRAM_TOKEN="", DB_PATH=os.path.join(TMP, "bot.db"), MCP_SECRET="sandbox-" + "x" * 20, ALLOWED_USERS="1",
                  ORION_MINIAPP_URL="", TELEGRAM_SESSION="")
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import sandbox  # noqa: E402
from plugins.newsroom.lists import parse_text  # noqa: E402

SAMPLE = """## Області
@kyiv_oda | КМВА | Київ
@lviv_oda | Львівська ОВА | Львівська
@odesa_oda | Одеська ОВА | Одеська
@kharkiv_oda | Харківська ОВА | Харківська
t.me/+AbCdInvite77 | Закритий чат волонтерів
-1001234567890 | Група громади
## Екологія
@ecoaction_ua | Екодія
@uncg_ua | Зелений фронт | | ні
## Офіційні
@dsns_telegram | ДСНС
@mvs_ukraine | МВС
"""

# дії, які змінюють дані, ходять в інтернет або чекають введення: у пісочниці лише описуються
SKIP = set(sandbox.ACTIONS) | {"in", "ed", "lenask", "dgask", "soadd", "sig", "rulesask", "Lnew", "Lup", "Lren", "Linc", "Lexc", "Lmin",
                                "Lreg", "Ladd", "Lbdel", "Lboff", "Lbon", "Epos", "Elab", "Ereg", "Elstn", "infoask", "trkask", "selask",
                                "Dst", "Fed", "Tgadd", "Tged", "Tpadd", "Tpe", "Bka", "Oua", "Wte", "pubch", "Ldelask", "Lacc"}
PROMPTS = ["m:pl:news:in", "m:pl:news:Lup", "m:pl:news:Lnew", "m:pl:stk:in", "m:pl:news:Ladd:1", "m:pl:news:Lbdel:1",
           "m:pl:news:ed:1", "m:pl:news:pubch", "m:pl:news:Tgadd", "m:pl:news:Bka:word", "m:pl:news:Oua", "m:pl:news:Ldelask:1"]


def seed():
    bot.init_db()
    P = bot.PLUGINS
    news, stk = P.plugins["news"], P.plugins["stk"]
    asyncio.run(news.import_groups(1, *parse_text(SAMPLE), 0, "add", "текст"))
    L = news.lists
    first = L.lists()[0]["id"]
    for i, e in enumerate(L.entries(first)):
        L.update_entry(e["id"], last_check=time.time() - 300 * (i + 1), last_id=1200 + i, taken=[14, 9, 6, 11, 0, 0][i],
                       via="web" if i < 4 else "", title=e["label"])
    L.update_entry(L.entries(first)[4]["id"], last_error="потрібен службовий акаунт (📲) або бот у цьому чаті")
    lb = L.auto_list("bot")
    L.upsert_chat(lb, "public", "riznoroby", -1001111, "Різноробочі", "channel", enabled=1)
    lo = L.auto_list("ours")
    L.add_entries(lo, parse_text("@greenpostua | GreenPost")[0][0][1])
    L.add_entries(first, parse_text("https://x.com/ukrenergo | Укренерго\nhttps://www.threads.net/@suspilne.news | Суспільне\n"
                                     "https://www.facebook.com/kyivcity.gov.ua | КМДА | Київ\nhttps://ecoaction.tumblr.com | Екодія блог")[0][0][1])
    L.add_entries(lo, parse_text("https://instagram.com/greenpost.ua | GreenPost Instagram\nhttps://www.threads.net/@greenpost.ua | GreenPost Threads\n"
                                 "https://www.facebook.com/GreenPostUA | GreenPost Facebook")[0][0][1])
    D = news.desk
    # 📊 місяць наших публікацій для звіту (вигадані цифри для пісочниці)
    import datetime as _dt, random as _r
    _r.seed(7)
    t0 = _dt.datetime.now().replace(day=1, hour=10) - _dt.timedelta(days=20)
    sample = [("tg", "Обстріл Харкова: пошкоджено школу", 1), ("tg", "У Києві відкрили новий притулок для тварин", 3),
              ("ig", "Сортування сміття в Одесі: як працює", 2), ("tg", "На Дніпропетровщині збили дрони", 1),
              ("fb", "У Львові висадили 300 дерев", 1.4), ("tg", "Атака дронів на Київ уночі", 1.1),
              ("threads", "Притулок у Полтаві шукає волонтерів", 2.6), ("tg", "Обстріл Запоріжжя: є поранені", 0.9),
              ("tg", "Енергетики: графіки на Харківщині", 1), ("ig", "Екоакція на Дніпрі в Києві", 1.5)]
    for i in range(36):
        pl, title, k = sample[i % len(sample)]
        at = (t0 + _dt.timedelta(hours=19 * i)).timestamp()
        base = {"tg": 2400, "ig": 0, "fb": 0, "threads": 900}[pl]
        D.add_ours("GreenPost", f"https://example.org/ours/{i}", title, title + ".", at, platform=pl,
                   metrics={"views": int(base * k * _r.uniform(.7, 1.3)), "likes": int(40 * k * _r.uniform(.6, 1.4)),
                            "reposts": int(6 * k), "comments": int(5 * k)})
    oid = D.q("SELECT id FROM ours_posts WHERE title LIKE '%притулок%' LIMIT 1")[0]["id"]
    for j, kind in enumerate(["quote", "repost", "retell", "plagiarism"]):
        D.x("INSERT INTO echoes(at,kind,ours_id,source,url,excerpt,score,attributed,key) VALUES(?,?,?,?,?,?,?,?,?)",
            (t0.timestamp() + 86400 * (j + 2), kind, oid, ["Київ Оперативний", "Зоозахист UA", "Новини Полтави", "Клон-канал"][j],
             "", "…", 0.9, 0 if kind == "plagiarism" else 1, f"sbx{j}"))
    ym = (_dt.datetime.now().replace(day=1) - _dt.timedelta(days=1)).strftime("%Y-%m")
    asyncio.run(news.pin_gaps(1, ym, "menu"))
    D.add_ours("GreenPost", "https://t.me/greenpostua/4100", "У Києві висадять 800 дерев на Осокорках",
               "У Дарницькому районі столиці облаштують екопарк і висадять 800 дерев, повідомили в КМДА.", time.time() - 86400 * 3)
    for d, status, target in [
        ({"headline": "У Києві відкрили екопарк на Осокорках", "body": "У Дарницькому районі столиці відкрили екопарк площею 12 гектарів, повідомили в КМДА.\n\nНа території висадили понад 800 дерев, облаштували велодоріжки та станцію сортування відходів.", "region": "Київ", "category": "екологія", "unconfirmed": ["точна кількість дерев"], "sources": ["https://t.me/kyiv_oda/1201"], "notes": "🔁 Оновлення нашої новини від 02.11: парк уже відкрито."}, "draft", 0),
        ({"headline": "На Львівщині виявили незаконне сміттєзвалище біля річки", "body": "Екоінспектори виявили стихійне сміттєзвалище площею близько 0,5 га поблизу русла річки.", "region": "Львівська", "category": "екологія", "sources": ["https://t.me/lviv_oda/1202"], "urgency": "urgent"}, "length_mismatch", 2000),
        ({"headline": "Головне за добу: екологія та безпека", "body": "Київ: відкрили екопарк на Осокорках…\n\nЛьвівська: виявлено сміттєзвалище біля річки…", "region": "Україна"}, "approved", 0),
    ]:
        news.store.add_draft(None, d, model="Gemini", target=target, status=status, kind="digest" if "Головне" in d["headline"] else "news")
    news.store.set("publish_title", "GreenPost 🌿")
    news.store.set("publish_chat", "-100999")
    D.x("INSERT INTO tags(query,last_run,found) VALUES(?,?,?)", ("Укренерго відключення", time.time() - 1200, 17))
    D.x("INSERT INTO tags(query,last_run,found) VALUES(?,?,?)", ("сміттєзвалище OR звалище", time.time() - 3000, 6))
    now = time.time()
    for n, (t, src, h, sn, sc) in enumerate([
        ("Укренерго: графіки відключень у шести областях завтра", "Укрінформ", 1, "Енергетики попереджають про планові відключення через ремонт.", {"importance": 8, "fit": 9, "credibility": "media", "urgency": "urgent", "topic": "енергетика", "why": "зачіпає мільйони людей"}),
        ("У Кіровоградській області ліквідують сміттєзвалище біля села", "Суспільне", 5, "Громада отримала кошти на рекультивацію.", {"importance": 6, "fit": 10, "credibility": "media", "topic": "екологія", "why": "наша основна тема"}),
        ("Блог: чому сортування сміття не працює у малих містах", "Українська правда. Блоги", 20, "Автор аналізує досвід трьох громад.", {"importance": 5, "fit": 8, "credibility": "blog", "topic": "екологія", "why": "авторський погляд"}),
    ], 1):
        D.add_find("tag", 1, {"title": t, "url": f"https://example.org/n{n}", "source": src, "published": now - h * 3600, "snippet": sn}, sc,
                   __import__("plugins.newsroom.desk", fromlist=["priority"]).priority(sc, now - h * 3600))
    D.add_block("word", "казино")
    D.add_block("source", "@spam_news")
    D.log_spam("спам: слова: промокод, розіграш", "channel", "Розіграші UA", "", "🔥🔥 РОЗІГРАШ! Промокод на знижку…")
    import sqlite3
    with sqlite3.connect(stk.db_path) as con:
        con.execute("INSERT INTO history(chat,at,packs,mode,files,dups,parts,bytes,errors) VALUES(1,?,?,?,?,?,?,?,0)",
                    (time.time() - 3600, json.dumps([["addstickers", "CuteCats"], ["addemoji", "UkrEmoji"]]), "one", 184, 3, 1, 9_400_000))
        con.execute("INSERT INTO tracked(chat,name,kind,title,signature,ids,count,checked,changed) VALUES(1,'CuteCats','addstickers','Cute Cats','x','[]',120,?,?)",
                    (time.time(), time.time() - 86400 * 2))
    return news, lb


class Chat:
    id = 1
    last = None

    async def send_message(self, text, **kw):
        m = kw.get("reply_markup")
        rows = [[(b.text, b.callback_data or ("url:" + b.url if b.url else ("app:" if b.web_app else ""))) for b in r] for r in m.inline_keyboard] if m else []
        Chat.last = (text, rows)


UPD = types.SimpleNamespace(effective_chat=Chat(), callback_query=None, effective_user=types.SimpleNamespace(id=1))


def action(d):
    p = d.split(":")
    if d.startswith("m:pl:"):
        rest = p[2:]
        if rest and rest[0] in ("tog", "mgr"):
            return rest[0]
        if len(rest) > 1:
            return rest[1]
    return None


async def grab(d):
    Chat.last = None
    try:
        await asyncio.wait_for(bot.open_data(UPD, None, d, edit=False, record=False), 10)
    except Exception as exc:  # noqa: BLE001
        print("помилка екрана", d, repr(exc)[:120])
    bot.AWAIT.pop(1, None)
    return Chat.last


# Дії, які в симуляції не виконуються: видалення, скидання, відновлення, вихід, файли, мережа.
import re as _re
UNSAFE = _re.compile(r"(del|purge|wipe|fryes|frgo|reset|lv:|leave|bkgo|bkrs|restore|empty|trash:all|stop|logout|utmx|utmd|bmv|bup|bdn|bhide|bshow|bgup|bgdn|bset|frt|olpull|olwarm|oldely|clrun|rwsync|lkwake|lkplan|bwake)")


async def crawl(limit=2600):
    screens = {}
    queue = ["m:home", "m:grp:plugins", "m:pl", "m:pl:news", "m:pl:stk", "m:utm", "m:utmreg", "m:utmst", "m:utmres",
             "m:grp:work", "m:grp:create", "m:grp:ai", "m:grp:services", "m:grp:settings", "m:grp:start", "m:rm", "m:cl", "m:lnk", "m:ol", "help:main"]
    while queue and len(screens) < limit:
        d = queue.pop(0)
        if d in screens:
            continue
        r = await grab(d)
        if not r:
            continue
        screens[d] = {"text": r[0], "rows": r[1]}
        for row in r[1]:
            for _t, nd in row:
                if not nd or nd.startswith(("url:", "app:", "m:nav", "m:cancel", "ui:")) or nd in screens:
                    continue
                if nd.startswith("m:pl") and action(nd) in SKIP:
                    continue
                if nd.startswith("m:pl:hub") and not bot.PLUGINS.plugins["hub"].is_view(nd.split(":")[3:]):
                    continue  # у головному боті — лише перегляд екранів, без дій
                if nd.startswith(("m:pl", "m:grp", "m:home")):
                    queue.append(nd)
                elif nd.startswith(("m:", "help:")) and not UNSAFE.search(nd):
                    queue.append(nd)  # уся решта бота: налаштування, режими, сервери, UTM, довідка
    for d in PROMPTS:
        r = await grab(d)
        news = bot.PLUGINS.plugins["news"]
        news.wait_file.pop(1, None)
        news.wait_forward.pop(1, None)
        if r:
            screens[d] = {"text": r[0], "rows": r[1]}
    return screens


async def scenes(news, lb):
    out = {}
    t, rows = news.card(1)
    out["forward"] = {"user": "↪️ Переслано з «КМВА»: У Дарницькому районі відкрили екопарк площею 12 га…",
                      "bot": ["📥 Узяв у ньюзрум #2. Чернетка прийде окремим повідомленням."], "card": {"text": t, "rows": rows}}
    out["update"] = {"user": None, "bot": [], "card": {"text": "🔁 <b>Оновлення нашої новини</b>\n\n🆕 Нове: КМДА: екопарк на Осокорках відкрито для відвідувачів\nhttps://t.me/kyiv_oda/1201\nЩо саме: парк уже відкрито, площа 12 га\n\n" + news.story_text(1),
                                                    "rows": [[("🔁 Написати оновлення", "m:pl:news:Su:1:update"), ("❌ Написати спростування", "m:pl:news:Su:1:refute")], [("📚 Історія", "m:pl:news:St:1")]]}}
    out["sticker"] = {"user": "🐱 [стікер]", "bot": [], "card": {"text": "🧷 Стікер із пакета <code>CuteCats</code>",
                      "rows": [[("📦 Завантажити пакет", "m:pl:stk:go:x"), ("🔎 Про пакет", "m:pl:stk:inf:x")], [("👁 Стежити за оновленнями", "m:pl:stk:trk1:x")]]}}
    toast, scr = await news.import_groups(1, *parse_text("## Південь\n@mykolaiv_oda | Миколаївська ОВА | Миколаївська\n@kherson_oda | Херсонська ОВА | Херсонська\nрядок без посилання"), 0, "add", "канали_області.xlsx")
    out["file"] = {"user": "📎 канали_області.xlsx", "bot": [], "card": {"text": scr[0], "rows": scr[1]}}
    out["channel"] = {"user": None, "bot": [], "card": {"text": "📡 <b>Ньюзрум</b>: бот бачить групу «Чат громади». Брати звідти новини автоматично?",
                      "rows": [[("✅ Моніторити", "m:pl:news:Etog:x"), ("📋 Список", f"m:pl:news:Lo:{lb}")]]}}
    Chat.last = None
    chunks = []
    _orig = Chat.send_message
    async def _cap(self, text, **kw):
        await _orig(self, text, **kw); chunks.append(Chat.last)
    Chat.send_message = _cap
    try:
        await bot.utm_output(UPD, 1, "https://greenpost.ua/news/zubr-zbyly-na-lvivshchyni-i112233", "zubr_lvivshchyna")
    finally:
        Chat.send_message = _orig
    tt, tr = bot.scr_utm_tags(1, "https://greenpost.ua/news/zubr-zbyly-na-lvivshchyni-i112233")
    out["utm"] = {"user": "/utm https://greenpost.ua/news/zubr-zbyly-na-lvivshchyni-i112233", "bot": [],
                  "card": {"text": tt, "rows": tr}}
    out["utm9"] = {"user": "2. zubr_zbyly", "bot": [c[0] for c in chunks[:-1]],
                   "card": {"text": chunks[-1][0], "rows": chunks[-1][1]} if chunks else None}
    out["tag"] = {"user": None, "bot": [], "card": dict(zip(("text", "rows"), news.finds_screen("tag", 1, "prio", 0, title="🏷 Укренерго відключення · нових 3")))}
    return out


def main():
    # кнопки вже без згортання: kb() повертає все як є
    news, lb = seed()
    import utm_flow
    led = bot.utm_ledger()
    utm_flow.generate(led, "https://greenpost.ua/news/zubr-zbyly-na-lvivshchyni-i112233", "zubr_lvivshchyna")
    utm_flow.generate(led, "https://greenpost.ua/eco/u-kyievi-vidkryly-ekopark-i112240", "ekopark")
    bot._utm_state(1, url="https://greenpost.ua/news/zubr-zbyly-na-lvivshchyni-i112233", tag="zubr_lvivshchyna",
                   tags=utm_flow.suggest_tags("https://greenpost.ua/news/zubr-zbyly-na-lvivshchyni-i112233"))
    screens = asyncio.run(crawl())
    sc = asyncio.run(scenes(news, lb))
    data = {"version": "зведена версія · " + time.strftime("%d.%m.%Y"), "screens": screens, "scenes": sc, "actions": sandbox.ACTIONS}
    out = Path(os.environ.get("SANDBOX_OUT") or ROOT / "simulations")
    out.mkdir(parents=True, exist_ok=True)
    (out / "sandbox_screens.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tpl = (ROOT / "simulations" / "sandbox_template.html").read_text("utf-8")
    (out / "sandbox.html").write_text(tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")), "utf-8")
    print(f"пісочниця: {len(screens)} екранів, {len(sc)} сценаріїв")
    return data


if __name__ == "__main__":
    main()
