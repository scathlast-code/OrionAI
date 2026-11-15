import asyncio

import httpx

from plugins.newsroom.echo import classify
from plugins.newsroom.lists import parse_text
from tests.test_plugins_lists import news

OURS = ("У Дарницькому районі столиці облаштують екопарк і висадять 800 дерев, повідомили в КМДА. На території з’являться "
        "велодоріжки, зони відпочинку і станція сортування відходів, а роботи завершать до кінця року.")


def test_rewrite_is_never_plagiarism():
    b = ["GreenPost"]
    assert classify(OURS + " ", OURS, b)[0] == "plagiarism"
    assert classify(OURS + " Джерело: GreenPost", OURS, b)[0] == "copy"
    rewrite = ("Столична влада оголосила, що на Осокорках у Дарницькому районі з’явиться новий екопарк: там посадять "
               "близько 800 дерев і зроблять велодоріжки, пишуть у КМДА.")
    assert classify(rewrite, OURS, b)[0] == "retell"
    half = OURS[: len(OURS) // 2] + " А ще місто планує нові маршрути автобусів і ремонт доріг у кількох районах столиці цього року."
    assert classify(half, OURS, b)[0] in ("quote", "retell")
    assert classify("У Львові відкрили новий трамвайний маршрут до вокзалу, повідомили в міськраді.", OURS, b)[0] is None
    assert classify("Дякуємо GreenPost за матеріал про ліси Карпат", OURS, b)[0] == "mention"


PAGE = """<meta property="og:title" content="Чужий канал"><div class="tgme_channel_info"></div>
<div data-post="other_chan/5"><div class="tgme_widget_message_text js-message_text">старий</div></div>
<div data-post="other_chan/6"><a class="tgme_widget_message_forwarded_from_name" href="https://t.me/greenpostua">GreenPost</a>
<div class="tgme_widget_message_text js-message_text">{ours}</div></div>
<div data-post="other_chan/7"><div class="tgme_widget_message_text js-message_text">{ours} </div></div>"""


def test_repost_and_plagiarism_detected_not_ingested(tmp_path):
    p, sent = news(tmp_path)
    p.store.set("publish_username", "greenpostua")
    p.store.set("publish_title", "GreenPost 🌿")
    p.desk.add_ours("GreenPost", "https://t.me/greenpostua/4100", "Екопарк", OURS)
    pages = [PAGE.split('<div data-post="other_chan/6">')[0]]
    p.web.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=pages[0])))
    lid = p.lists.create_list("Чужі")
    p.lists.add_entries(lid, parse_text("@other_chan")[0][0][1])
    asyncio.run(p.check_sources(force=True))          # перше читання — лише запам'ятати
    pages[0] = PAGE.format(ours=OURS)
    n, _ = asyncio.run(p.check_sources(force=True))
    kinds = sorted(e["kind"] for e in p.echo.items())
    assert kinds == ["plagiarism", "repost"] and n == 0   # наш текст новиною не вважається
    assert any("Плагіат" in t or "Репост" in t for _c, t, _r in sent)
