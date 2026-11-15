import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/twh"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", TG_WEBHOOK="1", RAILWAY_PUBLIC_DOMAIN="orion.up.railway.app")
import httpx
_o = httpx.AsyncClient.__init__
def h(req): return httpx.Response(200, json={"choices": [{"message": {"content": "відповідь"}, "finish_reason": "stop"}]})
def _i(self, *a, **k): k.setdefault("transport", httpx.MockTransport(h)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
import uvicorn, bot
async def fake_serve(self, *a, **k):
    app = self.config.app
    wh = [p for m, p in tg.calls if m == "setWebhook"][-1]
    print("setWebhook:", wh["url"], "| polling started:", any(m == "getUpdates" for m, _ in tg.calls))
    path = "/" + wh["url"].split("/", 3)[3]
    upd = {"update_id": 1, "message": {"message_id": 5, "date": int(time.time()), "chat": {"id": 111, "type": "private"}, "from": {"id": 111, "is_bot": False, "first_name": "O"}, "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        bad = await c.post(path, json=upd); print("no secret ->", bad.status_code)
        ok = await c.post(path, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": wh["secret_token"]}); print("with secret ->", ok.status_code, ok.text)
    await asyncio.sleep(0.5)
    print("bot answered:", tg.last_text(111).split("\n")[0])
uvicorn.Server.serve = fake_serve
tg.app = bot.tg_app
asyncio.run(bot.main())
