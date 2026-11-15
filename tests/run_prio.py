import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/tpr", exist_ok=True)
import shutil; shutil.rmtree("/tmp/tpr", ignore_errors=True); os.makedirs("/tmp/tpr", exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/tpr/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", LLM_PARALLEL="1",
                  HUB_URL="https://hub.example/hhhhhhhhhhhhhhhhhhhh", HUB_KEY="KEY1", HUB_REPLY_WAIT="1", SUMMARY_EVERY="100")
import httpx
HUBIN = []
async def handler(req):
    u = str(req.url)
    if "hub.example" in u:
        HUBIN.append(json.loads(req.content)); return httpx.Response(200, json={"ok": True})
    body = json.loads(req.content); q = str(body["messages"][-1]["content"])
    await asyncio.sleep(2.0 if "довге" in q else 0.6)
    return httpx.Response(200, json={"choices": [{"message": {"content": f"відповідь на «{q[:30]}»"}, "finish_reason": "stop"}]})
class T(httpx.AsyncBaseTransport):
    async def handle_async_request(self, req):
        await req.aread(); return await handler(req)
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k):
    k.setdefault("transport", T()); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
import bot
bot.Progress.__init__.__defaults__ = (True, 0.2)
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    da = bot.current_dialog(U); bot.dialog_update(da, title="Чернетки")
    db_ = bot.dialog_create(U); bot.dialog_update(db_, title="Терміновий звіт")
    # пріоритет: «Терміновий звіт» вгору, «Чернетки» вниз
    await tg.press(U, f"m:prm:d:{db_}:up"); await tg.press(U, f"m:prm:d:{da}:down")
    await tg.press(U, "m:pr:d"); print(tg.last_text(U)); print(tg.last_buttons(U)[:4])
    # А (низький) почав довге; Б (високий) приходить — А на паузі, Б виконано, потім А
    bot.set_pref(U, "dialog", da); ta = asyncio.create_task(tg.text(U, "довге есе"))
    await asyncio.sleep(0.4)
    bot.set_pref(U, "dialog", db_); tb = asyncio.create_task(tg.text(U, "звіт за тиждень"))
    await tb; await ta
    for p in tg.out(U)[-8:]:
        t = p["text"].replace("\n", " | ")
        if not t.startswith("⏳"): print("  >", t[:150])
    print("paused total:", bot.WQ.paused_total)
    print("journal:", bot.CM.q("SELECT 1") and [(r["status"], r["prompt"][:15]) for r in __import__("sqlite3").connect("/tmp/tpr/bot.db").execute("SELECT status, prompt FROM journal").fetchall()] if False else "")
    import sqlite3; print("journal:", sqlite3.connect("/tmp/tpr/bot.db").execute("SELECT status, substr(prompt,1,15) FROM journal").fetchall())
    # запит від головного бота, поки ШІ зайнята — у черзі, повтор не дублюється
    bot.set_pref(U, "dialog", da); tl = asyncio.create_task(tg.text(U, "довге продовження"))
    await asyncio.sleep(0.3)
    from starlette.testclient import TestClient
    class Req:
        def __init__(self, b): self.b = b
        async def json(self): return self.b
    body = {"key": "KEY1", "text": "сайт впав", "type": "alert", "from": "Монітор сайту", "prio": -1}
    r1 = json.loads((await bot.hub_command(Req(body))).body); print("\nhub 1:", {k: r1[k] for k in r1 if k != "reply"}, r1["reply"][:90])
    r2 = json.loads((await bot.hub_command(Req(body))).body); print("hub dup:", r2["reply"][:60])
    await tl; await asyncio.sleep(1.5)
    print("hub got:", [h["text"][:70] for h in HUBIN if "сайт" in h["text"] or "Монітор" in h["text"]])
    await tg.press(U, "m:pr:b"); print("\n" + tg.last_text(U).split("\n", 4)[-1])
asyncio.run(main())
