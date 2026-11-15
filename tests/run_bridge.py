"""Перевірка мосту: ШІ-бот у WhatsApp та Instagram на підробному сервері Meta.
    python tests/run_bridge.py whatsapp|instagram
"""
import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAT = sys.argv[1] if len(sys.argv) > 1 else "whatsapp"
D = f"/tmp/tb_{PLAT}"
os.makedirs(D, exist_ok=True)
for f in os.listdir(D):
    os.remove(os.path.join(D, f)) if os.path.isfile(os.path.join(D, f)) else None
U = "380501112233" if PLAT == "whatsapp" else "17841400000000001"
os.environ.update(TELEGRAM_TOKEN="1:bridge", ALLOWED_USERS=U, DB_PATH=f"{D}/bot.db", MCP_SECRET="s" * 20, GROQ_API_KEY="k",
                  WA_TOKEN="T", WA_PHONE_ID="PH", IG_TOKEN="T", PUBLIC_URL="https://bot.example", BRIDGE_PLATFORM=PLAT)
import httpx

OUT = []  # що бот надіслав у Meta


def handler(req: httpx.Request):
    u = str(req.url)
    if "groq.com" in u:
        body = json.loads(req.content)
        q = body["messages"][-1]["content"]
        q = q if isinstance(q, str) else str(q)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": f"Відповідь на: {q[-60:]}"}, "finish_reason": "stop"}],
                                         "usage": {"total_tokens": 10}})
    if u.endswith("/messages") and req.method == "POST":
        OUT.append(json.loads(req.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]} if PLAT == "whatsapp" else {"message_id": "m1"})
    if u.endswith("/media") and req.method == "POST":
        OUT.append({"upload": True})
        return httpx.Response(200, json={"id": "MEDIA1"})
    if "/MEDIAIN" in u:
        return httpx.Response(200, json={"url": "https://lookaside.example/voice"})
    if "lookaside.example" in u or "cdn.example" in u:
        return httpx.Response(200, content=b"OggS fake voice")
    if "refresh_access_token" in u:
        return httpx.Response(200, json={"access_token": "T2-refreshed", "token_type": "bearer", "expires_in": 5184000})
    if "messenger_profile" in u:
        return httpx.Response(200, json={"result": "success"})
    return httpx.Response(404, json={"error": {"message": "nope " + u}})


_orig = httpx.AsyncClient.__init__
def _init(self, *a, **k):
    k["transport"] = httpx.MockTransport(handler); _orig(self, *a, **k)
httpx.AsyncClient.__init__ = _init

import bridge as B
br = B.WhatsApp(D) if PLAT == "whatsapp" else B.Instagram(D, "https://bot.example")
br.install()
WHICH = sys.argv[2] if len(sys.argv) > 2 else "ai"
os.environ.update(MANAGER_TOKEN="2:bridge", MANAGER_SECRET="h" * 20, MANAGER_DB_PATH=f"{D}/m.db")
if WHICH == "manager":
    import manager as M
import bot
import media

async def _tr(data, name):
    return "Нагадай про зустріч завтра", "groq", []
media.transcribe = _tr


def wa_text(text):
    return {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": U, "profile": {"name": "Я"}}], "messages": [{"from": U, "id": f"w{len(OUT)}{text[:3]}", "type": "text", "text": {"body": text}}]}}]}]}


def wa_btn(data):
    return {"entry": [{"changes": [{"value": {"messages": [{"from": U, "id": f"b{len(OUT)}", "type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": data, "title": "x"}}}]}}]}]}


def ig_text(text):
    return {"entry": [{"id": "ME", "messaging": [{"sender": {"id": U}, "recipient": {"id": "ME"}, "message": {"mid": f"m{len(OUT)}", "text": text}}]}]}


def ig_btn(data):
    return {"entry": [{"id": "ME", "messaging": [{"sender": {"id": U}, "recipient": {"id": "ME"}, "postback": {"payload": data, "title": "x"}}]}]}


text_in = wa_text if PLAT == "whatsapp" else ig_text
btn_in = wa_btn if PLAT == "whatsapp" else ig_btn


def show(o):
    if PLAT == "whatsapp":
        if o.get("type") == "text":
            return "TEXT: " + o["text"]["body"][:160].replace("\n", " | ")
        if o.get("type") == "interactive":
            it = o["interactive"]
            if it["type"] == "button":
                return f"BUTTONS: {it['body']['text'][:80]!r} -> " + str([b["reply"]["title"] for b in it["action"]["buttons"]])
            return f"LIST: {it['body']['text'][:80]!r} -> " + str([(r['title'], r['id']) for r in it["action"]["sections"][0]["rows"]])
        return o.get("type", str(o))[:100]
    m = o.get("message", o)
    if "quick_replies" in m:
        return f"QR: {m['text'][:80]!r} -> " + str([q["title"] for q in m["quick_replies"]])
    if "attachment" in m:
        p = m["attachment"]["payload"]
        if p.get("template_type") == "button":
            return f"BTNTPL: {p['text'][:80]!r} -> " + str([b["title"] for b in p["buttons"]])
        return "ATTACH: " + m["attachment"]["type"] + " " + p.get("url", "")
    if "text" in m:
        return "TEXT: " + m["text"][:160].replace("\n", " | ")
    return str(o)[:100]


def find(label):
    for o in reversed(OUT):
        s = json.dumps(o, ensure_ascii=False)
        if label in s:
            if PLAT == "whatsapp" and o.get("type") == "interactive":
                it = o["interactive"]
                items = [(b["reply"]["title"], b["reply"]["id"]) for b in it["action"]["buttons"]] if it["type"] == "button" else [(r["title"], r["id"]) for r in it["action"]["sections"][0]["rows"]]
            else:
                m = o.get("message", {})
                items = [(q["title"], q["payload"]) for q in m.get("quick_replies", [])] or [(b["title"], b.get("payload")) for b in m.get("attachment", {}).get("payload", {}).get("buttons", [])]
            for t, d in items:
                if label in t:
                    return d
    raise AssertionError("немає " + label)


async def step(title, payload):
    n = len(OUT)
    await br.dispatch(payload)
    print(f"\n### {title}")
    for o in OUT[n:]:
        print("  ", show(o))


async def main_manager():
    M.init_db()
    br.app = M.app
    await M.app.initialize()
    await step("старт", text_in("/start"))
    await step("меню", text_in("меню"))
    await step("🤖 Боти", btn_in("__menu:🤖 Боти"))
    await step("додати", btn_in("m:badd"))
    await step("тип HTTP", btn_in("m:badd2:http"))
    await step("назва", text_in("Монітор сайту"))
    await step("підключення", btn_in(find("Підключення") if any("Підключення" in json.dumps(o, ensure_ascii=False) for o in OUT) else "m:bl"))


async def main():
    if WHICH == "manager":
        return await main_manager()
    bot.init_db()
    br.app = bot.tg_app
    await bot.tg_app.initialize()
    await step("старт", text_in("/start"))
    await step("меню", text_in("меню"))
    await step("Робота", btn_in(find("Робота")))
    await step("Нотатки", btn_in(find("Нотатки")))
    await step("Назад", btn_in(find("Назад")))
    await step("питання ШІ", text_in("Яка столиця Польщі?"))
    await step("кнопки (редактор)", btn_in("m:btn"))
    await step("редактор: пункт", btn_in("m:bi:tl"))
    await step("Моделі", btn_in("m:md"))
    if PLAT == "whatsapp":
        await step("голосове", {"entry": [{"changes": [{"value": {"messages": [{"from": U, "id": "v1", "type": "audio", "audio": {"id": "MEDIAIN", "voice": True, "mime_type": "audio/ogg"}}]}}]}]})
    # вікно 24 год: людина мовчить довше доби
    br.last_seen[int(U)] = 0
    n = len(OUT)
    await bot.tg_app.bot.send_message(int(U), "⏰ нагадування поза вікном")
    print("\n### поза вікном 24 год — у черзі:", len(br.outbox.get(int(U), [])), "надіслано:", len(OUT) - n)
    await step("людина пише — черга доставляється", text_in("привіт"))
    # той самий вебхук двічі
    p = text_in("дубль")
    n = len(OUT)
    await br.dispatch(p)
    r = br.dispatch(p)
    print("\n### дубль вебхука проігноровано:", r is None)


asyncio.run(main())
