import asyncio
import json
import types

from tests.test_plugins_lists import news


def msg(text, chat=1):
    out = []

    async def reply_text(t, **kw):
        out.append(t)
    m = types.SimpleNamespace(chat=types.SimpleNamespace(type="private"), chat_id=chat, text=text, caption=None, photo=None,
                              forward_origin=None, entities=[], caption_entities=[], reply_text=reply_text)
    return m, out


def test_collect_then_template_with_scenario(tmp_path):
    p, sent = news(tmp_path)
    prompts = []

    async def ai(prompt, system=None, label=None, image=None):
        prompts.append(prompt)
        return json.dumps({"headline": "Головне за день", "body": "Зведення", "region": "Україна", "skip": False}), "Fake"
    p.host.ask_ai = ai
    p.host.extra["awaiting"] = lambda c: False
    asyncio.run(p.route(None, None, 1, ["cgo"]))
    assert p.collecting(1)
    for t in ("Перша новина про Київ", "Друга новина про Львів", "Третя про Одесу"):
        m, out = msg(t)
        assert asyncio.run(p.intercept(types.SimpleNamespace(message=m, effective_user=types.SimpleNamespace(id=1)), None))
        assert out[-1].startswith("✅ Прийнято")
    assert out[-1] == "✅ Прийнято · 3" and not prompts           # під час збору ШІ не викликається
    toast, scr = asyncio.run(p.route(None, None, 1, ["cmk", "t1"]))
    assert "сценарій" in scr[0].lower() and any("📍 За областями" in t for r in scr[1] for t, _ in r)
    asyncio.run(p.make_from_collected(1, "t1", "Хронологія"))
    assert "Перша новина про Київ" in prompts[0] and "Сценарій (пріоритетний): Хронологія" in prompts[0]
    d = p.store.drafts(kind="digest", limit=1)[0]
    assert d["headline"] == "Головне за день" and not p.collected(1)   # «очищати після» — зібране прибрано


def test_day_news_by_topic(tmp_path):
    p, _ = news(tmp_path)
    p.store.add_draft(None, {"headline": "Нічна атака дронів на Київ", "body": "ППО збила 20 дронів", "category": "війна"}, status="approved")
    p.store.add_draft(None, {"headline": "Притулок для собак у Львові", "body": "Зоозахисники відкрили притулок", "category": "тварини"})
    p.store.add_draft(None, {"headline": "Меркурій ретроградний", "body": "Астрологи радять", "category": "астрологія"})
    topics = {t["key"]: t["id"] for t in p.desk.topics()}
    assert {"war", "astrology", "animals", "eco"} <= set(topics)
    toast, _ = asyncio.run(p.route(None, None, 1, ["cdt", str(topics["animals"])]))
    assert "додано 1" in toast and "собак" in p.collected(1)[0]["text"]
    toast, _ = asyncio.run(p.route(None, None, 1, ["cdt", str(topics["war"])]))
    assert "додано 1" in toast and len(p.collected(1)) == 2
    asyncio.run(p.route(None, None, 1, ["cdt", "0"]))
    assert len(p.collected(1)) == 3          # «усі теми» докладають лише те, чого ще немає
