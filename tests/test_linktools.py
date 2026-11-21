import asyncio

import httpx

import linktools as LT


def test_clean_url_strips_tracking():
    u = "https://greenpost.ua/news/a-i112922?utm_content=health&utm_medium=social&fbclid=X&id=5"
    assert LT.clean_url(u) == "https://greenpost.ua/news/a-i112922?id=5"
    assert LT.clean_url("https://www.instagram.com/p/DSef/?img_index=1&igsh=MWR") == "https://www.instagram.com/p/DSef/?img_index=1"
    assert "si=" in LT.clean_url("https://www.youtube.com/watch?v=a&si=b")  # на ютубі «si» — не чіпаємо


def test_bad_hosts():
    for h in ("vk.com", "m.vk.com", "ok.ru", "news.yandex.ua", "yandex.com", "site.by", "x.xn--p1ai", "dzen.ru"):
        assert LT.is_bad(h), h
    for h in ("greenpost.ua", "youtube.com", "rubik.com", "bvk.com.ua", "ukr.net"):
        assert not LT.is_bad(h), h


def test_verdict_warnings():
    v = LT.verdict(["https://bit.ly/x", "http://1.2.3.4/login"])
    assert v["level"] == "bad" and any("цифри" in w for w in v["warn"]) and any("http" in w for w in v["warn"])
    assert any("скорочене" in i for i in v["info"])
    ok = LT.verdict(["https://greenpost.ua/a?utm_source=tg"])
    assert ok["level"] == "ok" and ok["clean"] == "https://greenpost.ua/a"
    assert LT.verdict(["https://t.co/a", "https://vk.com/x"])["level"] == "bad"


def test_expand_follows_redirects_without_network():
    def handler(req):
        if req.url.host == "bit.ly":
            return httpx.Response(301, headers={"location": "https://greenpost.ua/n?utm_source=x"})
        return httpx.Response(200)
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await LT.expand("https://bit.ly/abc", client=c)
    v = asyncio.run(go())
    assert v["chain"] == ["https://bit.ly/abc", "https://greenpost.ua/n?utm_source=x"]
    assert v["clean"] == "https://greenpost.ua/n" and v["status"] == 200


def test_expand_stops_before_bad_host():
    seen = []
    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(302, headers={"location": "https://ok.ru/video/1"})
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await LT.expand("https://cutt.ly/z", client=c)
    v = asyncio.run(go())
    assert seen == ["cutt.ly"] and v["level"] == "bad"


def test_report_and_qr():
    html = LT.report_html(LT.verdict(["https://bit.ly/x", "https://a.ua/?fbclid=1"]), lambda s: s)
    assert "Маршрут" in html and "Чисте посилання" in html
    assert LT.qr_png("https://a.ua").startswith(b"\x89PNG")
