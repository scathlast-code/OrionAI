import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/th", exist_ok=True)
import shutil; shutil.rmtree("/tmp/th", ignore_errors=True); os.makedirs("/tmp/th", exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/th/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", HUB_URL="https://hub.example/" + "h"*20, HUB_KEY="KEY1")
import httpx
HB = []
def handler(req):
    u = str(req.url)
    if "groq.com" in u:
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Сайт впав — перевірте хостинг."}, "finish_reason": "stop"}]})
    if "hub.example" in u:
        HB.append((u, json.loads(req.content))); return httpx.Response(200, json={"ok": True})
    return httpx.Response(404)
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k):
    k.setdefault("transport", httpx.MockTransport(handler)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
import bot
async def main():
    bot.init_db(); await tg.start(bot.tg_app)
    app = bot.mcp.streamable_http_app(streamable_http_path=f"/{bot.MCP_SECRET}/mcp", stateless_http=True, host="0.0.0.0")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/{bot.MCP_SECRET}/cmd", json={"key": "KEY1", "text": "🔴 Сайт не відповідає", "from": "Монітор", "type": "alert"}); print(r.status_code, r.json())
        r = await c.post(f"/{bot.MCP_SECRET}/cmd", json={"key": "bad", "text": "x"}); print("bad key:", r.status_code)
        r = await c.post(f"/{bot.MCP_SECRET}/cmd", json={"key": "KEY1", "text": "статус"}); print(r.json())
    os.environ["PUBLIC_URL"] = "https://ai.example"
    await bot.hub_tick(); print("heartbeat:", HB[-1] if HB else None)
    print("dialogs:", [d["title"] for d in bot.dialogs_list(111)])
asyncio.run(main())
