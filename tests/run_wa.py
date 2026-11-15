import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(TELEGRAM_TOKEN="1:bridge", ALLOWED_USERS="380671112233", DB_PATH="/tmp/t6/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k",
                  WA_TOKEN="T", WA_PHONE_ID="PH", META_APP_SECRET="", WA_VERIFY_TOKEN="v")
import httpx
OUT = []
def handler(req):
    u = str(req.url)
    if u.endswith("/PH/messages"):
        b = json.loads(req.content); OUT.append(b)
        if b.get("to") == "380000000000": return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
        return httpx.Response(200, json={"messages": [{"id": "wamid"}]})
    if u.endswith("/PH/media"): OUT.append({"upload": True}); return httpx.Response(200, json={"id": "MEDIA1"})
    if u.endswith("/VOICE1"): return httpx.Response(200, json={"url": "https://lookaside.example/voice"})
    if "lookaside" in u: return httpx.Response(200, content=b"OGGDATA")
    return httpx.Response(404, text=u)
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k["transport"] = httpx.MockTransport(handler); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import bridge as B
br = B.WhatsApp("/tmp/t6").install()
import bot, media
br.app = bot.tg_app
async def fake(h, c, preferred=None, fallback=True): return "Відповідь моделі *важлива*", "groq", [], []
bot.ask_llm = fake
async def ftr(data, name=None, **kw): return "Нагадай про зустріч о десятій", "groq_stt", []
media.transcribe = ftr; bot.media.transcribe = ftr
async def fimg(prompt, w=1024, h=1024, **kw): return b"\xff\xd8\xffIMG", "pollinations", []
def wh(uid, m): return {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": uid, "profile": {"name": "Я"}}], "messages": [{"from": uid, "id": "w1", **m}]}}]}]}
def show(n=3):
    for b in OUT[-n:]:
        t = b.get("type")
        if t == "text": print("  TEXT:", b["text"]["body"][:200].replace("\n", " | "))
        elif t == "interactive":
            it = b["interactive"]; acts = it["action"].get("buttons") or it["action"]["sections"][0]["rows"]
            print("  ", it["type"].upper(), ":", it["body"]["text"][:120].replace("\n", " | "), "->", [ (x.get("reply") or x)["title"] if "reply" in x else x["title"] for x in acts])
        else: print("  ", b)
async def main():
    bot.init_db(); await bot.tg_app.initialize(); await bot.tg_app.start()
    U = "380671112233"
    await br.webhook(wh(U, {"type": "text", "text": {"body": "/start"}})); print("START"); show(4)
    await br.webhook(wh(U, {"type": "text", "text": {"body": "меню"}})); print("MENU word"); show(1)
    await br.webhook(wh(U, {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "__menu:☰ Меню", "title": "☰ Меню"}}})); print("HOME"); show(1)
    await br.webhook(wh(U, {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "m:grp:store"}}})); print("STORE"); show(1)
    await br.webhook(wh(U, {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "m:nav:back"}}})); print("BACK"); show(1)
    await br.webhook(wh(U, {"type": "text", "text": {"body": "Що таке фотосинтез?"}})); print("ASK"); show(2)
    await br.webhook(wh(U, {"type": "audio", "audio": {"id": "VOICE1", "voice": True, "mime_type": "audio/ogg"}})); print("VOICE"); show(4)
    await br.webhook(wh(U, {"type": "audio", "context": {"forwarded": True}, "audio": {"id": "VOICE1", "mime_type": "audio/ogg"}})); print("FWD VOICE"); show(3)
    bot.media.generate_image = fimg
    await bot.do_image(bot.tg_app.bot, int(U), "green park"); print("IMAGE"); show(2)
    m = bot.media_list(int(U)); print("library:", len(m), m[0]["file_id"][:8] if m else None)
    await bot.resend_media(bot.tg_app.bot, int(U), m[0]); print("RESEND"); show(2)
    await br.webhook(wh(U, {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "m:md"}}})); print("MODELS"); show(1)
    await br.webhook(wh(U, {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "m:cpnew"}}}))
    await br.webhook(wh(U, {"type": "text", "text": {"body": "DeepSeek"}})); await br.webhook(wh(U, {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "m:cpurl:0"}}}))
    await br.webhook(wh(U, {"type": "text", "text": {"body": "sk-123"}})); print("KEY msg"); show(3)
    # out of window
    br.last_seen[380000000000] = 0
    await bot.tg_app.bot.send_message(380000000000, "нагадування"); print("queued:", len(br.outbox.get(380000000000, [])))
asyncio.run(main())
