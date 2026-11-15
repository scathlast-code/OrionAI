"""ШІ-бот і головний бот в одному процесі: обмін планами сну лише при змінах, тиша під час сну, «розбудити» в обидва боки."""
import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tplans"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
SEC = "m" * 20
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", CRONJOB_API_KEY="cj", ORION_PLUGINS_OFF="hub",
                  PUBLIC_URL="https://ai.example", MANAGER_TOKEN="2:y", MANAGER_SECRET=SEC, MANAGER_DB_PATH=f"{D}/m.db", MANAGER_PUBLIC_URL="https://hub.example",
                  HUB_URL=f"https://hub.example/{SEC}", HUB_KEY="")
import httpx
LOG = []
class Net(httpx.AsyncBaseTransport):
    async def handle_async_request(self, req):
        u = str(req.url); await req.aread()
        if "cron-job.org" in u:
            return httpx.Response(200, json={"jobId": 1} if req.method == "PUT" else {})
        if req.url.host == "hub.example":
            LOG.append(("→головний", u.split(SEC)[-1] if SEC in u else u.split("example")[1], (json.loads(req.content or b"{}") or {}).get("type")))
            return await httpx.ASGITransport(app=M.web).handle_async_request(req)
        if req.url.host == "ai.example":
            body = json.loads(req.content or b"{}") if req.content else {}
            LOG.append(("→ШІ", req.url.path.split("/")[1] if "/wake/" in req.url.path else "cmd", body.get("type")))
            if "/wake/" in req.url.path: return httpx.Response(200, text="ok")
            class R:
                async def json(self_): return body
            r = await bot.hub_command(R()); return httpx.Response(r.status_code, content=r.body, headers={"content-type": "application/json"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k["transport"] = Net(); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import faketg; tg = faketg.install(); tg.handlers["getUpdates"] = lambda p: []
import bot, manager as M
from zoneinfo import ZoneInfo
async def main():
    bot.init_db(); M.init_db()
    bid = M.bot_add("http", "ШІ-бот", cmd_url=f"https://ai.example/{'s'*20}/cmd")
    key = M.bot_get(bid)["inbox_key"]; bot.hub.configure(f"https://hub.example/{SEC}", key); bot.hub.key = key
    os.environ["HUB_KEY"] = key
    await tg.start(bot.tg_app); await bot.tg_app.start()
    # ШІ-бот: «⏰ Сон + будильник», прокидається о 09:00 і 21:00
    bot.NODE.set("mode", "timer"); bot.NODE.set("wake", [{"t": "09:00", "d": []}, {"t": "21:00", "d": []}]); await bot.apply_receiving()
    LOG.clear(); await bot.hub_tick()
    print("1) план ШІ-бота → головному:", LOG); b = M.bot_get(bid); print("   головний знає:", M.plan_text(M.bplan(b)), "|", M.sleep_text(b) or "не спить")
    LOG.clear(); await bot.hub_tick(); await bot.hub_tick(); print("2) без змін — жодного обміну:", LOG == [])
    # головний: перевірки за розкладом і сам «⏰»
    g = M.gsched(); g["scheme"] = "times"; g["times"] = [{"t": "09:00", "d": []}]; M.set_gsched(g)
    M.NODE.set("mode", "timer"); M.HOOK_ON["on"] = True
    LOG.clear(); n = await M.push_all_plans(); print("3) головний змінив план → ШІ-боту:", LOG, "| ШІ знає:", bot.plan_text(bot.hub.peer_plan))
    # обидва сплять (поза вікнами)
    M.bot_update(bid, last_seen=time.time() - 3600); bot.NODE.set("hub_plan", bot.hub.peer_plan)
    asleep_now = bot.plan_state(bot.hub.peer_plan)[0] == "asleep"
    LOG.clear(); await bot.hub.send("звичайний звіт", "info"); print("4) головний спить:", asleep_now, "→ звіт відкладено:", LOG == [] and len(bot.hub.held) == 1 if asleep_now else "(зараз його вікно)")
    LOG.clear(); await bot.hub.send("🚨 Тривога", "alert"); print("   тривога йде одразу:", [x for x in LOG if x[2] == "alert"] != [])
    M.bot_update(bid, last_seen=time.time() - 3600); LOG.clear()
    st, d = await M.check_bot(M.bot_get(bid)); print("5) головний перевіряє сплячого ШІ-бота:", st, "|", d, "| запитів до ШІ:", [x for x in LOG if x[0] == "→ШІ"])
    eid, _ = M.event_add(None, "info", "передай ШІ", "manager"); ok, det = await M.deliver(M.event_get(eid), M.bot_get(bid)); print("6) пересилання сплячому:", det)
    LOG.clear(); await tg.start(M.app); await tg.text(111, "/start")
    await tg.press(111, f"m:bwake:{bid}"); print("7) «⏰ Розбудити» з головного:", [x for x in LOG if x[0] == "→ШІ"], "|", [p["text"] for c, m, p in tg.sent if "розбуджено" in p.get("text", "")][-1:])
    await tg.press(111, "m:diag:links"); print("\n" + "\n".join(tg.last_text(111).split("\n")[:9]))
asyncio.run(main())
async def child_ui():
    U = 111
    await tg.start(bot.tg_app)
    await tg.text(U, "/start"); await tg.press(U, "m:diag:links"); print("\n--- ШІ-бот, 🔗 Зв'язки ---\n" + "\n".join(tg.last_text(U).split("\n")[:12])); print([t for r in tg.last_buttons(U) for t, d in r])
    LOG.clear(); await tg.press(U, "m:lkwake"); print("розбудити головного:", LOG[:2])
    await tg.press(U, "m:lkhub"); await tg.text(U, "https://hub2.example/XYZ/in NEWKEY"); print("нова адреса головного:", bot.hub.url, bot.hub.key)
    await tg.press(U, "m:lkpub"); await tg.text(U, "https://ai2.example"); print("моя адреса:", bot.public_base(), "| webhook:", [p["url"] for m, p in tg.calls if m == "setWebhook"][-1])
asyncio.run(child_ui())
async def failover_addresses():
    # головний переїхав на інший сервер → ШІ-бот сам перемикається на нову адресу
    key = bot.hub.key
    bot.on_hub_plan({"mode": "always", "inbox": "https://hub-b.example/" + SEC})
    print("\nШІ-бот пише головному на:", bot.hub.url, "| ключ той самий:", bot.hub.key == key)
    # ШІ-бот переїхав → головний оновлює адресу команд
    b = M.bot_get(1); M.bot_update(1, cmd_url="https://old.example/cmd")
    class R:
        path_params = {"secret": SEC}; method = "POST"; headers = {}
        async def json(self): return {"key": b["inbox_key"], "type": "plan", "plan": {"mode": "always", "cmd_url": "https://ai-b.example/" + "s"*20 + "/cmd"}}
    await M.http_in(R()); print("головний шле команди на:", M.bot_get(1)["cmd_url"])
asyncio.run(failover_addresses())
