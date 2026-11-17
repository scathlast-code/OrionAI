import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from plugins import Host, Registry
from plugins.newsroom import Plugin as News, verify_signature, whatsapp_messages
from plugins.newsroom.engine import Engine, length_of, parse_json, region_key
from plugins.newsroom.feeds import FeedError, parse
from plugins.newsroom.store import Store, similarity


def run(c):
    return asyncio.run(c)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))

        class M:
            message_id = len(self.sent)
        return M()

    async def delete_message(self, *a):
        pass


def make_host(tmp_path, reply):
    bot = FakeBot()
    calls = []

    async def ai(prompt, system=None, label=None, image=None):
        calls.append(prompt)
        return reply(prompt, len(calls)), "FakeModel"

    host = Host(data_dir=tmp_path, admins=lambda: [1], is_admin=lambda u: u == 1, is_allowed=lambda u: True,
                ask_ai=ai, bot=lambda: bot)
    return host, bot, calls


NEWS = {"headline": "У Києві відкрили новий сквер", "body": "Тестовий текст новини.", "region": "Київ",
        "category": "суспільство", "urgency": "normal", "confirmed": ["відкрили сквер"], "unconfirmed": [],
        "sources": ["КМДА"], "notes": "", "skip": False}


def test_parse_json_variants():
    assert parse_json('```json\n{"a": "b{c}"}\n```')["a"] == "b{c}"
    assert parse_json('Ось: {"x": 1, "y": {"z": "}"}} кінець')["y"]["z"] == "}"
    with pytest.raises(ValueError):
        parse_json("без json")


def test_dedup_and_related(tmp_path):
    s = Store(tmp_path / "n.db")
    a, st, _ = s.add_item("manual", "Сьогодні вранці в Житомирі відкрили новий міст через річку Тетерів після ремонту")
    assert st == "queued"
    b, st, dup = s.add_item("manual", "Сьогодні вранці в Житомирі відкрили новий міст через річку Тетерів після ремонту!")
    assert st == "duplicate" and dup == a
    assert similarity("один два три чотири п'ять шість", "один два три чотири п'ять сім") > 0.3


def test_exact_length_loop(tmp_path):
    target = 40

    def reply(prompt, n):
        if "РІВНО" in prompt and "СКОРОТИ" not in prompt and "ДОДАЙ" not in prompt:
            return json.dumps(NEWS | {"body": "x" * 35})
        return json.dumps({"body": "y" * target})
    host, bot, calls = make_host(tmp_path, reply)
    data, model, ok = run(Engine(host.ask_ai).write("сирий текст", {"origin": "manual"}, target))
    assert ok and length_of(data["body"]) == target and len(calls) == 2


def test_length_mismatch_blocks_approval(tmp_path):
    host, bot, _ = make_host(tmp_path, lambda p, n: json.dumps(NEWS | {"body": "z" * 10}))
    p = News(host)
    p.store.set("target", 50)
    iid, st, _ = p.ingest("manual", "Якийсь новинний текст про подію в області", requested_by=1)
    run(p._process(iid))
    did = p.store.draft_for_item(iid)
    assert p.store.draft(did)["status"] == "length_mismatch"
    toast, _ = run(p.route(None, None, 1, ["ok", str(did)]))
    assert "довжину" in toast and p.store.draft(did)["status"] == "length_mismatch"


def test_full_flow_publish(tmp_path):
    host, bot, _ = make_host(tmp_path, lambda p, n: json.dumps(NEWS))
    p = News(host)
    p.store.set("publish_chat", "-100123")
    p.store.set("signature", "🌿 GreenPost")
    iid, st, _ = p.ingest("manual", "У столиці відкрили сквер біля метро", requested_by=1)
    run(p._process(iid))
    did = p.store.draft_for_item(iid)
    text, rows = p.card(did)
    assert "Схвалити" in str(rows) and "У Києві" in text
    toast, _ = run(p.route(None, None, 1, ["pub", str(did)]))
    assert "лише схвалене" in toast.lower()
    run(p.route(None, None, 1, ["ok", str(did)]))
    toast, _ = run(p.route(None, None, 1, ["pub", str(did)]))
    assert p.store.draft(did)["status"] == "published"
    assert any(chat == -100123 and "GreenPost" in t for chat, t in bot.sent)


def test_skip_not_news(tmp_path):
    host, bot, _ = make_host(tmp_path, lambda p, n: '{"skip": true, "notes": "реклама"}')
    p = News(host)
    iid, _, _ = p.ingest("manual", "Купуйте наші знижки!!!", requested_by=1)
    run(p._process(iid))
    assert p.store.item(iid)["status"] == "skipped"


def test_digest_region_order():
    regions = ["Львівська", "Світ", "Київ", "Україна", "Київська область"]
    assert sorted(regions, key=region_key)[:2] == ["Київ", "Київська область"]
    assert sorted(regions, key=region_key)[-1] == "Світ"


def test_rss_atom_and_dtd():
    rss = b"""<?xml version='1.0'?><rss><channel><title>T</title><item><title>A</title><link>https://x.ua/1</link>
    <guid>g1</guid><description>&lt;p&gt;Text&lt;/p&gt;</description></item></channel></rss>"""
    title, items = parse(rss)
    assert title == "T" and items[0]["guid"] == "g1" and items[0]["text"] == "Text"
    atom = b"""<feed xmlns='http://www.w3.org/2005/Atom'><title>F</title><entry><id>e1</id><title>B</title>
    <link rel='alternate' href='https://y.ua/2'/><summary>S</summary></entry></feed>"""
    title, items = parse(atom)
    assert title == "F" and items[0]["link"] == "https://y.ua/2"
    with pytest.raises(FeedError):
        parse(b"<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>")


def test_whatsapp_signature_and_filter():
    raw = b'{"entry":[]}'
    sig = "sha256=" + hmac.new(b"s3", raw, hashlib.sha256).hexdigest()
    assert verify_signature(b"s3", raw, sig) and not verify_signature(b"bad", raw, sig)
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "380501", "type": "text", "text": {"body": "новина"}},
        {"from": "999", "type": "text", "text": {"body": "чужий"}},
        {"from": "380501", "type": "image", "image": {"id": "m1", "caption": "фото"}}]}}]}]}
    out = whatsapp_messages(payload, {"380501"})
    assert [x[2] for x in out] == ["новина", "фото"] and out[1][3] == "m1"


def test_registry_toggle_and_admin(tmp_path):
    host, bot, _ = make_host(tmp_path, lambda p, n: "{}")
    reg = Registry(host)
    assert set(reg.plugins) == {"hub", "news", "stk", "inb", "med"}
    assert "m:pl:news" not in str(reg.home(5)[1]) and "лише для власника" in reg.home(5)[0]  # без кнопки, лише пояснення
    toast, _ = run(reg.route(None, None, 5, ["news"]))
    assert "власника" in toast
    assert reg.is_view(["news", "d", "3"]) and not reg.is_view(["news", "ok", "3"])
    toast, _ = run(reg.route(None, None, 1, ["tog", "stk"]))
    assert not reg.enabled("stk") and reg.get("stk") is None
    assert not reg.plugins["stk"].on()


def test_http_routes(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    host, bot, _ = make_host(tmp_path, lambda p, n: json.dumps(NEWS))
    p = News(host)
    app = Starlette(routes=p.routes("sekret"))
    c = TestClient(app)
    r = c.post("/sekret/newsroom/ingest", json={"text": "Новина з n8n про подію в Одесі сьогодні", "source": "n8n"})
    assert r.json()["ok"] and r.json()["status"] == "queued"
    assert c.post("/sekret/newsroom/ingest", json={"text": "Новина з n8n про подію в Одесі сьогодні"}).json()["status"] == "duplicate"
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "vt")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "as")
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", "+380501")
    assert c.get("/sekret/newsroom/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"}).text == "42"
    assert c.get("/sekret/newsroom/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "no"}).status_code == 403
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"from": "380501", "type": "text", "text": {"body": "WhatsApp-новина про Харків"}}]}}]}]}).encode()
    assert c.post("/sekret/newsroom/whatsapp", content=body, headers={"x-hub-signature-256": "sha256=00"}).status_code == 403
    sig = "sha256=" + hmac.new(b"as", body, hashlib.sha256).hexdigest()
    assert c.post("/sekret/newsroom/whatsapp", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    assert p.store.count("items", "origin='whatsapp'") == 1
