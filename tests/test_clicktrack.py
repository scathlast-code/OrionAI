import asyncio
import html

import clicktrack as C


def _tracker(tmp_path):
    t = C.Tracker(tmp_path / "c.db")

    async def geo(ip):
        return ("Україна", "Львів", "")
    t.geo = geo
    return t


def test_clicks_report(tmp_path):
    t = _tracker(tmp_path)
    full = "https://greenpost.ua/news/zubr?utm_content=zubr&utm_medium=post&utm_campaign=news&utm_source=facebook"
    code = t.link_for(full, C.base_of(full), "facebook", "zubr")
    assert t.link_for(full, C.base_of(full), "facebook", "zubr") == code
    assert t.target(code) == full

    async def go():
        await t.record(code, "1.1.1.1", "Mozilla/5.0 (iPhone) Mobile [FBAN/FBIOS;FBAV/1]", "", "uk")
        await t.record(code, "9.9.9.9", "TelegramBot (like TwitterBot)", "", "")
    asyncio.run(go())
    r = t.report(days=30)
    assert r["clicks"] == 1 and r["previews"] == 1
    assert r["network"]["Facebook"] == 1 and r["country"]["Україна"] == 1 and r["device"]["Телефон"] == 1
    assert "Кліків: <b>1</b>" in C.report_html(r, html.escape)
    assert C.export_xlsx(r)[:2] == b"PK"


def test_network_detection():
    assert C.network_of("telegram_stories", "", "") == "Telegram"
    assert C.network_of("facebook", "Instagram", "") == "Instagram"
    assert C.network_of("twitter", "", "t.co") == "Twitter/X"
    assert C.parse_ua("Mozilla/5.0 (Windows NT 10.0) Chrome/120")[:2] == ("Комп'ютер", "Windows")


def test_shortio_write_needs_permission(monkeypatch):
    import plugins.newsroom.shortio as S
    sio = S.ShortIO(key="k", domain="greenpost.s.gy")
    calls = []

    async def req(*a, **k):
        calls.append(a)
        return {"shortURL": "https://greenpost.s.gy/a", "idString": "1"}
    sio.request = req
    for policy, approved, ok in (("ask", False, False), ("ask", True, True), ("off", True, False), ("all", False, True)):
        monkeypatch.setattr(S, "WRITE_POLICY", lambda p=policy: p)
        before = len(calls)
        try:
            asyncio.run(sio.shorten("https://greenpost.ua/x", approved=approved))
        except S.ShortIOError:
            pass
        assert (len(calls) > before) == ok, policy


def test_own_shortener_rules(tmp_path):
    import time
    t = C.Tracker(tmp_path / "s.db")
    code = t.create("https://greenpost.ua/news/x", "zubr-lviv", "Зубри")
    assert t.check(code, "Mozilla") == ("go", "https://greenpost.ua/news/x")
    t.update(code, rules={"device": {"ios": "https://apps.apple.com/x"}})
    assert t.check(code, "Mozilla/5.0 (iPhone) Mobile")[1] == "https://apps.apple.com/x"
    t.update(code, pw_hash=C.password_hash(code, "1"))
    assert t.check(code, "Mozilla")[0] == "password" and t.check(code, "Mozilla", password="1")[0] == "go"
    t.update(code, expires=time.time() - 1)
    assert t.check(code, "Mozilla")[0] == "gone"
    assert t.rename(code, "zubr2") == "zubr2" and t.get("zubr2")["title"] == "Зубри"
    assert C.qr_png("https://x.ua/zubr2")[:4] == b"\x89PNG"
