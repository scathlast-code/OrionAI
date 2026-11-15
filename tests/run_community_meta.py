import asyncio, os, sys, sqlite3, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from community import Community
P = "/tmp/tcm.db"
if os.path.exists(P): os.remove(P)
def db():
    c = sqlite3.connect(P); c.row_factory = sqlite3.Row; return c
class Bot:
    sent = []
    async def send_message(self, chat, text, **k): self.sent.append((chat, text)); return None
class Br:
    def __init__(self, plat): self.platform, self.phone_id, self.calls, self.n = plat, "PH", [], 0
    def count_sent(self): self.n += 1
    async def api(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if path == "PH/groups": return {"data": [{"id": "G1", "subject": "Клієнти"}]}
        if path.endswith("/join_requests") and method == "GET": return {"data": [{"join_request_id": "JR1", "wa_id": "380501112233"}]}
        if path == "me": return {"user_id": "IG1", "username": "zubr"}
        if path == "me/media": return {"id": "C1"}
        return {}
async def main():
    cm = Community(db, lambda: [111]); cm.bot = Bot()
    # WhatsApp
    cm.bridge = Br("wa")
    t, scr = await cm.route(111, "m:cm:warf".split(":")); print("wa refresh:", t, scr[1][0])
    c = cm.chat("wa:G1"); c["cfg"]["rules"]["links"] = 1; c["cfg"]["joins"] = "ask"; cm.save_cfg("wa:G1", c["cfg"])
    t, scr = await cm.route(111, "m:cm:r:wa:G1".split(":")); print("wa rules buttons:", [r[0][0] for r in scr[1]][:6])
    for i in range(3): await cm.wa_group_message("G1", "380509", "Іван", "купуй тут https://x.com")
    print("wa calls:", [(m, p, kw.get("json", {}).get("text", kw.get("json", {}).get("participants"))) for m, p, kw in cm.bridge.calls[1:]], "counted:", cm.bridge.n)
    await cm.wa_poll_joins(); print("wa join notify:", cm.bot.sent[-1][1][:70])
    rid = cm.q("SELECT id FROM cm_reqs WHERE kind='join'", one=True)["id"]; print("decide:", await cm.decide_join(rid, True), cm.bridge.calls[-1][:2], cm.bridge.calls[-1][2]["json"])
    pid = cm.x("INSERT INTO cm_posts(cid, text, kind, sched, created, status) VALUES ('wa:G1','Привіт, групо','text','{\"k\":\"now\"}',0,'active')")
    await cm.publish(pid); print("wa post:", cm.bridge.calls[-1][2]["json"]["recipient_type"], cm.bridge.calls[-1][2]["json"]["text"])
    # Instagram
    cm.bridge = Br("ig")
    t, scr = await cm.route(111, "m:cm:igon".split(":")); print("\nig:", t, scr[0].split("\n")[0])
    c = cm.chat("ig:IG1"); c["cfg"]["rules"]["words"] = 1; c["cfg"]["words"] = ["спам"]; cm.save_cfg("ig:IG1", c["cfg"])
    await cm.ig_comment("IG1", {"id": "CM5", "text": "Це спам!!!", "from": {"id": "U9", "username": "bad"}})
    await cm.ig_comment("IG1", {"id": "CM6", "text": "Гарне фото", "from": {"id": "U8", "username": "good"}})
    print("ig hide:", cm.bridge.calls[-1])
    pid = cm.x("INSERT INTO cm_posts(cid, text, kind, sched, created, status, image) VALUES ('ig:IG1','Зубр дня','text','{}',0,'active','https://ex.com/z.jpg')")
    await cm.publish(pid); print("ig publish:", [c[:2] for c in cm.bridge.calls[-2:]])
    t, scr = await cm.route(111, "m:cm:a:ig:IG1".split(":")); print("ig actions:", scr[1])
    print("log:", [r["text"] for r in cm.q("SELECT text FROM cm_log ORDER BY id")])
asyncio.run(main())
