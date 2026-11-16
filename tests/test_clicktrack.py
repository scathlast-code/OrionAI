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
