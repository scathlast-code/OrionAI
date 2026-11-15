"""Перевірка головного бота: додати ботів, скринька через HTTP, правило пересилання, захист від зациклень, моніторинг."""
import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tm"
os.makedirs(D, exist_ok=True)
for f in os.listdir(D):
    os.remove(os.path.join(D, f))
os.environ.update(MANAGER_TOKEN="2:m", MANAGER_SECRET="h" * 20, ALLOWED_USERS="111", MANAGER_DB_PATH=f"{D}/m.db", PUBLIC_URL="https://hub.example")
import httpx

CMD = []
DOWN = {"flag": False}


def handler(req: httpx.Request):
    u = str(req.url)
    if "api.telegram.org/bot77:" in u:
        m = u.rsplit("/", 1)[1]
        if DOWN["flag"]:
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        if m == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 77, "is_bot": True, "first_name": "Монітор", "username": "mon_bot"}})
        if m == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": "", "pending_update_count": 0}})
        if m == "getMyCommands":
            return httpx.Response(200, json={"ok": True, "result": [{"command": "start", "description": "Почати"}]})
        if m in ("getMyName", "getMyDescription", "getMyShortDescription"):
            k = {"getMyName": "name", "getMyDescription": "description", "getMyShortDescription": "short_description"}[m]
            return httpx.Response(200, json={"ok": True, "result": {k: "Монітор"}})
        return httpx.Response(200, json={"ok": True, "result": True})
    if u.startswith("https://ai.example/cmd"):
        body = json.loads(req.content)
        CMD.append(body)
        return httpx.Response(200, json={"ok": True, "reply": f"ШІ: прийняв «{body['text'][:30]}»"})
    return httpx.Response(404, text="nope " + u)


_orig = httpx.AsyncClient.__init__
def _init(self, *a, **k):
    k.setdefault("transport", httpx.MockTransport(handler)); _orig(self, *a, **k)
httpx.AsyncClient.__init__ = _init

import faketg; tg = faketg.install()
import manager as M

U = 111


async def http(path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=M.web), base_url="https://hub.example") as c:
        r = await c.post(path, json=body)
    return r.status_code, r.json()


async def main():
    M.init_db()
    await tg.start(M.app)
    await tg.text(U, "/start")
    print("KB:", tg.reply_kb(U)); print(tg.last_text(U)[:300]); print(tg.last_buttons(U))
    # додати Telegram-бота
    await tg.press(U, "m:badd"); await tg.press(U, "m:badd2:telegram")
    await tg.text(U, "77:SECRETTOKEN")
    print("\n## додано TG:", tg.last_text(U)[:300])
    print("видалено повідомлення з токеном:", any(m == "deleteMessage" for c, m, p in tg.sent))
    # додати HTTP-бота (ШІ-бот)
    await tg.press(U, "m:badd"); await tg.press(U, "m:badd2:http"); await tg.text(U, "ШІ-бот")
    print("\n## додано HTTP:", tg.last_text(U)[:400])
    bots = M.bots_list() if hasattr(M, "bots_list") else None
    rows = [dict(r) for r in M.db().execute("SELECT id, name, kind, inbox_key FROM bots")]
    print(rows)
    mon, ai = rows[0], rows[1]
    M.bot_update(ai["id"], cmd_url="https://ai.example/cmd")
    # подія від монітора через HTTP
    st, r = await http(f"/{M.SECRET}/in", {"key": mon["inbox_key"], "type": "alert", "text": "🔴 Сайт greenpost.ua не відповідає"})
    print("\n## HTTP in:", st, r); print(tg.last_text(U)[:300]); print(tg.last_buttons(U))
    # переслати вручну в ШІ-бот
    eid = r["event_id"]
    await tg.press(U, f"m:efw:{eid}"); print("\n## куди:", tg.last_buttons(U))
    await tg.press(U, tg.find(U, "ШІ-бот")); print(tg.last_text(U)[:300]); print("CMD:", CMD[-1] if CMD else None)
    # правило: усі тривоги монітора -> ШІ-бот автоматично
    await tg.press(U, "m:rl"); await tg.press(U, "m:rnew"); print("\n## правило:", tg.last_text(U)[:200], tg.last_buttons(U))
    await tg.press(U, tg.find(U, "Монітор")); print(tg.last_buttons(U))
    await tg.press(U, tg.find(U, "Тривога")); print("2:", tg.last_text(U)[:200], tg.last_buttons(U))
    for _ in range(4):
        btns = [t for r_ in tg.last_buttons(U) for t, _ in r_]
        pick = next((t for t in btns if "ШІ-бот" in t), None) or next((t for t in btns if "Будь-" in t or "Без" in t or "Усі" in t or "Зберегти" in t or "Готово" in t), None)
        if not pick:
            break
        await tg.press(U, tg.find(U, pick)); print("->", pick, "|", tg.last_text(U)[:250], tg.last_buttons(U))
    n = len(CMD)
    st, r = await http(f"/{M.SECRET}/in", {"key": mon["inbox_key"], "type": "alert", "text": "🔴 Знову падіння"})
    print("\n## авто-пересилання:", len(CMD) - n, "; сповіщення:", tg.last_text(U)[:300])
    # зациклення: подія з hop=3 не йде далі
    st, r = await http(f"/{M.SECRET}/in", {"key": mon["inbox_key"], "type": "alert", "text": "петля", "hop": 3})
    print("## hop=3 переслано:", len(CMD) - n - 1)
    # флуд: 40 подій за хвилину
    for i in range(40):
        st, r = await http(f"/{M.SECRET}/in", {"key": mon["inbox_key"], "type": "info", "text": f"спам {i}"})
    print("## флуд, остання відповідь:", r)
    # неправильний ключ
    print("## чужий ключ:", await http(f"/{M.SECRET}/in", {"key": "bad", "text": "x"}))
    print("## неправильний секрет:", (await http("/wrong/in", {"key": mon["inbox_key"], "text": "x"}))[0])
    # моніторинг: бот впав
    DOWN["flag"] = True
    await M.check_bot(M.bot_get(mon["id"]))
    print("\n## бот упав ->", tg.last_text(U)[:200])
    DOWN["flag"] = False
    await M.check_bot(M.bot_get(mon["id"]))
    print("## бот піднявся ->", tg.last_text(U)[:200])
    # профіль і команди
    await tg.press(U, f"m:bp:{mon['id']}"); print("\n## профіль:", tg.last_text(U)[:300])
    await tg.press(U, f"m:bc:{mon['id']}"); print("## команди:", tg.last_text(U)[:300])
    await tg.press(U, "m:in"); print("\n## вхідні:", tg.last_text(U)[:500]); print(tg.last_buttons(U)[:3])
    # переслане вручну повідомлення від бота
    await tg.text(U, "Звіт за день: 12 нових підписників", forward_origin={"type": "user", "date": 1, "sender_user": {"id": 77, "is_bot": True, "first_name": "Монітор", "username": "mon_bot"}})
    print("\n## переслане від бота:", tg.last_text(U)[:300])


asyncio.run(main())


async def reset_test():
    await tg.press(U, "m:fr"); print("\n## reset:", tg.last_text(U)[:150])
    await tg.press(U, "m:frt:events"); await tg.press(U, "m:frt:rules"); await tg.press(U, "m:frgo"); await tg.press(U, "m:fryes")
    print([p["text"][:100] for c, m, p in tg.sent if m == "sendMessage"][-2])
    con = M.db()
    print("bots:", con.execute("SELECT count(*) FROM bots").fetchone()[0], "events:", con.execute("SELECT count(*) FROM events").fetchone()[0], "rules:", con.execute("SELECT count(*) FROM rules").fetchone()[0])
    print("docs:", sum(1 for c, m, p in tg.sent if m == "sendDocument"))

asyncio.run(reset_test())


async def sched_diag_test():
    await tg.press(U, "m:bsch:1"); print("\n## sched:", tg.last_text(U)[:300]); print(tg.last_buttons(U))
    await tg.press(U, "m:bse:1:900"); print("every15:", M.sched_line(M.bot_get(1)))
    await tg.press(U, "m:bswin:1"); await tg.text(U, "08:00-23:00"); print("window:", M.sched_line(M.bot_get(1)))
    await tg.press(U, "m:bsd:1"); await tg.text(U, "09:30"); print("daily:", M.sched_line(M.bot_get(1)))
    await tg.press(U, "m:bsp:1:1h"); print("pause:", M.sched_line(M.bot_get(1)))
    s = M.bsched(M.bot_get(1)); s["pause_until"] = 1; M.set_bsched(1, s); M.bot_update(1, last_check=0)
    await M.monitor_tick(); print("auto-resume msg:", [p["text"] for c, m, p in tg.sent if m == "sendMessage"][-2:])
    await tg.press(U, "m:mon"); print("\n## mon:", tg.last_text(U)[:400]); print(tg.last_buttons(U)[:5])
    await tg.press(U, "m:mgp:3h"); print(tg.last_text(U)[:120])
    with M.closing(M.db()) as con, con:
        con.execute("INSERT INTO rules (from_bot, type, contains, to_bot, created) VALUES (2, NULL, NULL, 1, 0)")
        con.execute("INSERT INTO rules (from_bot, type, contains, to_bot, created) VALUES (1, NULL, NULL, 2, 0)")
    for t in ("sum", "tips", "nums"):
        await tg.press(U, f"m:diag:{t}"); print(f"\n== diag {t}\n" + tg.last_text(U))

asyncio.run(sched_diag_test())
