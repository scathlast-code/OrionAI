import asyncio
import datetime as dt

from plugins.newsroom.observances import kind_of, parse_wikitext
from tests.test_plugins_lists import news

WIKI = """'''6 листопада''' — 310-й день року.
== Свята і пам'ятні дні ==
=== Міжнародні ===
* [[Міжнародний день запобігання експлуатації довкілля під час війни]]<ref>ООН</ref>
=== Національні ===
* {{Прапорець|Україна}} [[Україна]]: [[День працівників соціальної сфери]].
* {{Прапорець|Швеція}} [[Швеція]]: День Густава Адольфа.
== Події ==
* 1913 — щось сталося
"""


def test_parse_wiki_and_kinds():
    got = parse_wikitext(WIKI)
    titles = [t for t, _ in got]
    assert "Міжнародний день запобігання експлуатації довкілля під час війни" in titles
    assert any("соціальної сфери" in t for t in titles) and not any("Густава" in t for t in titles)
    assert kind_of("Тиждень інформування про грип") == "week" and kind_of("Місячник прибирання") == "month"


def test_calendar_logic_scan_and_screens(tmp_path):
    p, sent = news(tmp_path)
    p.today = lambda: dt.date(2026, 11, 6)
    p.obs_add("День супу", "day", "11-06", "11-06", 1, "ваш запис", "https://uk.wikipedia.org/wiki/6_листопада")
    p.obs_add("Тиждень інформування про грип", "week", "11-03", "11-09", 1, "ваш запис", "https://example.org/flu")
    p.obs_add("Новорічний місячник", "month", "12-15", "01-15", 1, "ваш запис")      # через межу року
    titles = [r["title"] for r in p.obs_on(dt.date(2026, 11, 6))]
    assert "День супу" in titles and "Тиждень інформування про грип" in titles and "Movember — місяць чоловічого здоров'я" in titles
    assert "Новорічний місячник" in [r["title"] for r in p.obs_on(dt.date(2027, 1, 3))]
    assert any("рік пасовищ" in r["title"] for r in p.obs_on(dt.date(2026, 6, 1)))
    assert not any("рік пасовищ" in r["title"] for r in p.obs_on(dt.date(2027, 6, 1)))
    n = p.scan_post("Сьогодні — Всесвітній день сну! Висипайтеся.", "@kanal")
    assert n == 1 and "Всесвітній день сну" in [r["title"] for r in p.obs_on(dt.date(2026, 11, 6))]
    assert p.scan_post("Гарного дня всім, хороший день сьогодні", "@kanal") == 0
    for args in (["obh"], ["obt", "week"], ["obt", "month"], ["obt", "year"], ["obc", "202611"], ["obd", "20261106"]):
        toast, scr = asyncio.run(p.route(None, None, 1, args))
        assert scr[0] and all(len(d.encode()) <= 64 for r in scr[1] for _t, d in r if not d.startswith("url:"))
    t, rows = p.obs_home()
    assert "День супу" in t and "Триває" in t
    asyncio.run(p.route(None, None, 1, ["obz", str(p.obs_on(dt.date(2026, 11, 6))[0]["id"])]))
    assert p.collected(1)
    toast, scr = asyncio.run(p.obs_text(1, "oba", None, "Місячник прибирання | 01.04–30.04 | щороку"))
    assert any(r["title"] == "Місячник прибирання" for r in p.obs_on(dt.date(2027, 4, 20)))


WIKI2 = """== Свята і пам'ятні дні ==
* [[Всесвітній день довкілля]]
== Іменини ==
Анатолій, Григорій, [[Едмунд]]
== Події ==
=== XX століття ===
* [[1945]] — почався [[Нюрнберзький процес]]
* 1990 — щось інше
== Народились ==
* 1858 — [[Сельма Лагерлеф]], шведська письменниця
== Померли ==
* 1910 — [[Лев Толстой]]
"""


def test_daybook_sections_importance_astro():
    from plugins.newsroom import daybook as DB
    s = DB.parse_sections(WIKI2)
    assert s["history"][0].startswith("1945 — почався Нюрнберзький процес")
    assert s["born"] and s["died"] and "Едмунд" in s["names"][0]
    assert DB.auto_imp("День Незалежності України") == 2 and DB.auto_imp("Всесвітній день боротьби з діабетом") == 1
    assert DB.auto_imp("День супу") == 0
    why, since = DB.why_since({"title": "День батька (третя неділя червня), з 2019 року", "start": "06-15", "finish": "06-15", "yearly": 1})
    assert "неділя" in why and since == "2019"
    assert DB.astro_lines(dt.date(2026, 11, 20)) and "Скорпіон" in DB.sun_sign(dt.date(2026, 11, 20))


def test_lists_edit_shadow_personal(tmp_path):
    p, sent = news(tmp_path)
    p.today = lambda: dt.date(2026, 11, 20)
    p.obs_add("Всесвітній день довкілля", "day", "11-20", "11-20", 1, "Вікіпедія", "https://uk.wikipedia.org/wiki/20_листопада")
    p.db_add("11-20", "history", "1945 — почався процес", "https://x")
    t, rows = p.obs_home(1, "l")
    assert "Історія" in t and "‼️" in t and "Підготував" in t
    it = p.db_items("11-20", "history", 1)[0]
    asyncio.run(p.route(None, None, 1, ["obx", f"d{it['id']}"]))
    assert not p.db_items("11-20", "history", 1)
    p.db_add("11-20", "history", "1945 — почався процес", "https://x")  # оновлення з інтернету не повертає затінене
    assert not p.db_items("11-20", "history", 1)
    asyncio.run(p.route(None, None, 1, ["obr", f"d{it['id']}", "20261120"]))
    assert p.db_items("11-20", "history", 1)
    asyncio.run(p.obs_text(1, "obn", ["names", "20261120", True], "Мирослава"))
    assert p.db_items("11-20", "names", 1) and not p.db_items("11-20", "names", 2)  # особисте бачить лише автор
    oid = p.obs_on(dt.date(2026, 11, 20), 1)[0]["id"]
    asyncio.run(p.route(None, None, 1, ["obf", str(oid)]))
    assert p.obs_on(dt.date(2026, 11, 20), 1)[0]["fav"]
    up = p.upcoming(1, 3)
    assert isinstance(up, list)


def test_content_plan_merged_into_day(tmp_path):
    import io
    import openpyxl
    from plugins.newsroom import contentplan as CP
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Листопад 2026"
    ws.append([2026]); ws.append(["листопад"]); ws.append(["пн", "вт", "ср", "чт", "пт", "сб", "нд"])
    ws.append([2, 3, 4, 5, 6, 7, 8])
    ws.append([None, None, None, None, "День супу - Лариса\nДень павича - Валентин"])
    ws.append(["Сайт", None, None, None, "Як варити борщ"])
    b = io.BytesIO(); wb.save(b)
    plan = CP.parse(b.getvalue())
    assert {"title": "День павича", "author": "Валентин", "where": ""} in plan["2026-11-06"]
    p, _sent = news(tmp_path)
    p.today = lambda: dt.date(2026, 11, 6)
    p.cplan_bg = lambda: None
    p.obs_add("День супу", "day", "11-06", "11-06", 1, "ваш запис", "https://uk.wikipedia.org/wiki/6_листопада")
    for items in plan.values():
        for it in items:
            it["src"] = "https://docs.google.com/x"
    p.store.set("cplan_data", plan)
    t, _rows = p.obs_home()
    assert "✍️ Лариса" in t and "🗓 Контент-план · 2" in t and "День павича" in t and "Як варити борщ" in t
    assert "шукаю джерела" not in t and "триває" not in t.split("📌 Сьогодні відзначається")[1].split("\n")[0]


def test_garden_tips_clean_and_adapt(tmp_path):
    p, _sent = news(tmp_path)
    assert p.clean_snip("Роби так. Ще так. #### Пов'язані теми: [...] 17 листопада – перекопайте") == "Роби так. Ще так."

    async def fake(prompt, label=None):
        return "1. Перекопайте грядки — поки ґрунт не замерз.\n2. —", None
    p.host.ask_ai = fake
    got = asyncio.run(p.adapt_tips([{"text": "a", "url": "https://greenpost.ua/x"}, {"text": "b", "url": "https://b.ua"}], "листопад"))
    assert got == [{"text": "поки ґрунт не замерз.", "url": "https://greenpost.ua/x", "head": "Перекопайте грядки"}]
    assert "11 прикмет" in p.more_line("lore", 11, dt.date(2026, 11, 21))


def test_church_feasts_today(tmp_path):
    p, _sent = news(tmp_path)
    p.today = lambda: dt.date(2026, 11, 21)
    p.cplan_bg = lambda: None
    p.holidays_bg = lambda: None
    t, _rows = p.obs_home()
    assert "Введення в храм Пресвятої Богородиці" in t and "‼️" in t
    p.store.set("church_v", 0)
    p.ensure_church()
    assert sum("Введення" in r["title"] for r in p.obs_on(dt.date(2026, 11, 21))) == 1


def test_cplan_diff_and_tip_dedup(tmp_path):
    p, _sent = news(tmp_path)
    far = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    old = {far: [{"title": "День супу", "author": "Лариса", "where": ""}]}
    new = {far: [{"title": "День супу", "author": "Валентин", "where": ""}, {"title": "День чаю", "author": "", "where": "SMM"}]}
    ch = p.cplan_diff(old, new)
    assert ("+", far, "День чаю") in ch and ("−", far, "День супу · ✍️ Лариса") in ch

    async def fake(prompt, label=None):
        return ("1. Перекопування ґрунту з добривами — підготувати ґрунт до наступного року.\n"
                "2. Перекопування та добрива — продовжувати перекопувати ґрунт і вносити добрива.\n"
                "3. Обрізка смородини — прибрати старі гілки."), None
    p.host.ask_ai = fake
    got = asyncio.run(p.adapt_tips([{"text": "a", "url": "https://a.ua"}, {"text": "b", "url": "https://b.ua"}, {"text": "c", "url": "https://c.ua"}], "листопад"))
    assert [g["url"] for g in got] == ["https://a.ua", "https://c.ua"]


def test_cplan_month_any_language_and_year_by_weekday():
    import io
    import openpyxl
    from plugins.newsroom import contentplan as CP
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = " DECEMBER"
    ws.append(["DECEMBER"]); ws.append(["пн", "вт", "ср", "чт", "пт", "сб", "нд"])
    ws.append([None, 1, 2, 3, 4, 5, 6]); ws.append([None, "День X - Лариса"])
    b = io.BytesIO(); wb.save(b)
    assert "2026-12-01" in CP.parse(b.getvalue())
    assert CP._month_of("Listopad") == 11 and CP._month_of("Marzec") == 3 and CP._month_of("décembre") == 12


def test_jump_blinks_target_button(tmp_path):
    p, _sent = news(tmp_path)
    calls = []

    class B:
        username = "ai_orion_7bot"

        async def send_message(self, chat, text, **kw):
            calls.append(("send", text, kw["reply_markup"]))
            return type("M", (), {"message_id": 5})()

        async def edit_message_reply_markup(self, chat, mid, reply_markup=None):
            calls.append(("edit", reply_markup))
    p.host.bot = lambda: B()
    import plugins.newsroom.observances as O
    real = O.asyncio.sleep

    async def fast(_s):
        await real(0)
    O.asyncio.sleep = fast
    try:
        asyncio.run(p.jump(1, "lore", "20261121"))
    finally:
        O.asyncio.sleep = real
    labels = [b.text for row in calls[0][2].inline_keyboard for b in row]
    assert "👉 📜 Прикмети 👈" in labels and len(calls) == 7
    lore_btn = [b for row in calls[0][2].inline_keyboard for b in row if "Прикмети" in b.text][0]
    assert lore_btn.callback_data.endswith("obl:lore.20261121")
    assert 'start=obb_cplan_20261121' in p.cplan_ref(dt.date(2026, 11, 21))


def test_tidy_astro_lines():
    from plugins.newsroom.dayextra import tidy
    assert tidy("] 14 листопада Меркурій рушає вперед, петлі— у «тіні».", "2026-11-14") == "Меркурій рушає вперед, петлі — у «тіні»."


def test_seed_topics_have_sources(tmp_path):
    p, _sent = news(tmp_path)
    rs = [r for r in p.obs_on(dt.date(2026, 11, 21)) if "(ООН)" in r["title"] or "Movember" in r["title"]]
    assert rs and all(r["url"].startswith("https://") for r in rs)
    assert all("<a href" in p.obs_line(r) for r in rs)
