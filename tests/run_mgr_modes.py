import asyncio, os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tmm"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(MANAGER_TOKEN="1:x", ALLOWED_USERS="111", MANAGER_DB_PATH=f"{D}/m.db", MANAGER_SECRET="s"*20, RAILWAY_PUBLIC_DOMAIN="hub.up.railway.app", CRONJOB_API_KEY="cj", HEARTBEAT_TIMEOUT="900")
import httpx
CRON = {}; N = [0]
def h(req):
    u = str(req.url)
    if "cron-job.org" in u:
        if req.method == "PUT": N[0] += 1; CRON[N[0]] = json.loads(req.content)["job"]; return httpx.Response(200, json={"jobId": N[0]})
        CRON.pop(int(u.rsplit("/", 1)[1]), None); return httpx.Response(200, json={})
    return httpx.Response(200, json={"ok": True})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k.setdefault("transport", httpx.MockTransport(h)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install(); tg.handlers["getUpdates"] = lambda p: []
import manager as M
async def main():
    M.init_db(); await tg.start(M.app); await M.app.start(); U = 111
    bid = M.bot_add("http", "ШІ-бот")
    b = M.bot_get(bid)
    # бот попереджає: сплю до +2 год
    await M.ingest(b, "sleep", "😴 Засинаю: прокинусь о 21:00", "api", until=time.time() + 7200)
    b = M.bot_get(bid); print("іконка:", M.bot_icon(b), "|", M.sleep_text(b), "| сповіщень власнику:", len([p for c, m, p in tg.sent if c == U]))
    M.bot_update(bid, last_seen=time.time() - 5000)  # давно мовчить, але спить — тривоги немає
    st, d = await M.check_bot(M.bot_get(bid)); print("перевірка сплячого:", st, d)
    M.bot_update(bid, sleep_until=time.time() - 3600 - 900)  # обіцяв прокинутись і не прокинувся
    st, d = await M.check_bot(M.bot_get(bid)); print("проспав:", st, "|", d)
    # схеми моніторингу
    await tg.text(U, "/start"); await tg.press(U, "m:mon"); print("\n" + tg.last_text(U).split("\n")[2]); print([t for r in tg.last_buttons(U) for t, d in r][:5])
    await tg.press(U, "m:msc:times"); await tg.text(U, "09:00, 21:00"); print("розклад:", tg.last_text(U).split("\n")[2], "| due-текст бота:", M.sched_line(M.bot_get(bid)))
    await tg.press(U, "m:msc:manual"); print("за наказом:", M.schedule_text(M.global_rule()), "| due:", M.schedule_due(M.effective_sched(M.bot_get(bid)), 0, tz=M.TZ))
    await tg.text(U, "/check"); print("/check:", tg.last_text(U).split("\n")[0])
    # режим роботи головного
    await tg.press(U, "m:msc:times"); await tg.text(U, "09:00, 21:00")
    await tg.press(U, "m:rm"); await tg.press(U, "m:rmset:timer"); print("\nрежим:", tg.last_text(U).split("\n")[1], "| будильник на cron-job.org:", sorted((j["schedule"]["hours"][0]) for j in CRON.values()))
    await tg.press(U, "m:rw"); print(tg.last_text(U).replace("\n", " | ")[:300])
    await tg.press(U, "m:rmset:sleep"); print("\nсон + схема «Постійно 5 хв»:"); await tg.press(U, "m:msc:always"); await tg.press(U, "m:rm"); print([l for l in tg.last_text(U).split("\n") if "⚠️" in l])
    await M.app.updater.stop() if M.app.updater.running else None
asyncio.run(main())
