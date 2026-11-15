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
    p.obs_add("День супу", "day", "11-06", "11-06", 1, "ваш запис")
    p.obs_add("Тиждень інформування про грип", "week", "11-03", "11-09", 1, "ваш запис")
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
