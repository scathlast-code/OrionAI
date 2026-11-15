import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(MANAGER_TOKEN="1:bridge", ALLOWED_USERS="17841400000000001", MANAGER_DB_PATH="/tmp/t7/m.db", MANAGER_SECRET="x"*20, MANAGER_PUBLIC_URL="https://hub.example", IG_TOKEN="IGT")
import httpx
OUT = []
def handler(req):
    u = str(req.url)
    if u.endswith("/me/messages"): OUT.append(json.loads(req.content)); return httpx.Response(200, json={"message_id": "m"})
    if "messenger_profile" in u: OUT.append({"profile": json.loads(req.content)}); return httpx.Response(200, json={"result": "success"})
    if "api.telegram.org/botMON:" in u:
        m = u.rsplit("/",1)[1]
        res = {"getMe": {"id": 555, "is_bot": True, "first_name": "Монітор", "username": "mon_bot"}, "getWebhookInfo": {"url": ""}, "getMyCommands": []}.get(m, {})
        return httpx.Response(200, json={"ok": True, "result": res})
    return httpx.Response(404, text=u)
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k["transport"] = httpx.MockTransport(handler); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
import bridge as B
br = B.Instagram("/tmp/t7", "https://hub.example").install()
import manager as M
br.app = M.app
def wh(uid, ev): return {"object": "instagram", "entry": [{"id": "999", "messaging": [{"sender": {"id": uid}, "recipient": {"id": "999"}, **ev}]}]}
def show(n=2):
    for b in OUT[-n:]:
        m = b.get("message", b)
        if "quick_replies" in m: print("  QR:", m["text"][:150].replace("\n"," | "), "->", [q["title"] for q in m["quick_replies"]])
        elif "attachment" in m: p = m["attachment"]["payload"]; print("  TPL:", p.get("text","")[:120].replace("\n"," | "), [x["title"] for x in p.get("buttons", [])] or p.get("url"))
        elif "text" in m: print("  TEXT:", m["text"][:200].replace("\n"," | "))
        else: print("  ", str(b)[:200])
async def main():
    M.init_db(); await M.app.initialize(); await M.app.start(); U = "17841400000000001"
    await br.setup_menu([t for row in M.L.reply_rows(int(U)) for t in row]); show(1)
    await br.webhook(wh("123", {"message": {"mid": "1", "text": "hi"}})); print("STRANGER"); show(1)
    await br.webhook(wh(U, {"message": {"mid": "2", "text": "/start"}})); print("START"); show(3)
    await br.webhook(wh(U, {"postback": {"payload": "__menu:🤖 Боти"}})); print("BOTS"); show(1)
    await br.webhook(wh(U, {"message": {"mid": "3", "text": "➕ Додати бота", "quick_reply": {"payload": "m:badd"}}})); show(1)
    await br.webhook(wh(U, {"message": {"mid": "4", "text": "t", "quick_reply": {"payload": "m:badd2:telegram"}}})); show(1)
    await br.webhook(wh(U, {"message": {"mid": "5", "text": "MON:abc"}})); print("ADDED"); show(3)
    await br.webhook(wh(U, {"message": {"mid": "6", "text": "меню"}})); print("MENU"); show(1)
asyncio.run(main())
