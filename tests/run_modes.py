import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tmodes"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k",
                  RAILWAY_PUBLIC_DOMAIN="orion.up.railway.app", CRONJOB_API_KEY="cj")
import httpx
CRON = {}; NEXT = [100]
def h(req):
    if "cron-job.org" in str(req.url):
        if req.method == "PUT":
            NEXT[0] += 1; CRON[NEXT[0]] = json.loads(req.content)["job"]; return httpx.Response(200, json={"jobId": NEXT[0]})
        if req.method == "DELETE":
            CRON.pop(int(str(req.url).rsplit("/", 1)[1]), None); return httpx.Response(200, json={})
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k.setdefault("transport", httpx.MockTransport(h)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install()
tg.handlers["getUpdates"] = lambda p: []
import uvicorn, bot
calls = lambda m: [p for mm, p in tg.calls if mm == m]
async def fake_serve(self, *a, **k):
    U = 111
    print("start mode:", bot.NODE.mode(bot.DEFAULT_MODE), "| polling:", bot.tg_app.updater.running, "| webhook:", len(calls("setWebhook")))
    await tg.text(U, "/start")
    await tg.press(U, "m:grp:settings"); print("menu has mode:", any("Режим роботи" in t for r in tg.last_buttons(U) for t, d in r))
    await tg.press(U, "m:rm"); print(tg.last_text(U)[:300].replace("\n", " | "))
    await tg.press(U, "m:rmset:sleep"); print("\n→ sleep: polling:", bot.tg_app.updater.running, "| webhook set:", calls("setWebhook")[-1]["url"])
    await tg.press(U, "m:rmset:timer"); await tg.press(U, "m:rw"); print("\n" + tg.last_text(U)[:400].replace("\n", " | "))
    await tg.press(U, "m:rwadd"); await tg.text(U, "08:30 пн,ср,пт; 21:00"); print("\nwake:", bot.NODE.wake_text(), "| cron jobs:", [(j["schedule"]["hours"], j["schedule"]["minutes"], j["schedule"]["wdays"]) for j in CRON.values()])
    # заплановане завдання -> окреме пробудження до нього
    tid = bot.task_create(U, 0, "Нагадай про зустріч", "Нагадай про зустріч", None, "", status="scheduled", run_at=time.time() + 3 * 3600)
    await bot.sync_wakes(); print("with task:", len(CRON), "jobs; task job:", [j["schedule"] for j in CRON.values() if j["schedule"]["mdays"] != [-1]])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.config.app), base_url="http://t") as c:
        r = await c.get(bot.WAKE_PATH); print("wake endpoint:", r.status_code, r.text)
    await tg.press(U, "m:rmset:always"); print("\n→ always: polling:", bot.tg_app.updater.running, "| deleteWebhook:", len(calls("deleteWebhook")), "| cron jobs left:", len(CRON))
    await tg.press(U, "m:cl"); print("\n" + tg.last_text(U)[:200].replace("\n", " | "))
    await bot.tg_app.updater.stop()
uvicorn.Server.serve = fake_serve
tg.app = bot.tg_app
asyncio.run(bot.main())
