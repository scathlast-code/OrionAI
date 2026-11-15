import asyncio, os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tml"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(MANAGER_TOKEN="1:x", ALLOWED_USERS="111", MANAGER_DB_PATH=f"{D}/m.db", MANAGER_SECRET="s"*20, RAILWAY_PUBLIC_DOMAIN="hub.up.railway.app")
import faketg; tg = faketg.install()
import manager as M, cluster
async def main():
    M.init_db(); await tg.start(M.app); U = 111
    await tg.text(U, "/start")
    await tg.press(U, "m:rm"); print([t for r in tg.last_buttons(U) for t, d in r][-6:])
    print([l for l in tg.last_text(U).split("\n") if "Oracle" in l or "Нагадування" in l])
    await tg.press(U, "m:lnk"); print("links:", tg.last_text(U).split("\n")[0])
    await tg.press(U, "m:lnkgh"); await tg.text(U, "ivan/OrionAI"); print("repo:", cluster.github_repo(M.NODE))
    # дочірній на Oracle → головний (на Railway) теж нагадує; дочірній натиснув «Зайшов» → стан у головного
    bid = M.bot_add("http", "ШІ-бот")
    child = {"name": "ШІ-бот", "mode": "always", "oracle": {"oracle_remind": None, "oracle_every": 21, "oracle_next": time.time() + 21*86400,
             "oracle_last": time.time(), "ts": time.time(), "need": True}}
    M.bot_update(bid, plan=json.dumps(child)); M.take_child_oracle(child)
    print("needed:", M.oracle_needed(), "| every:", M.NODE.get("oracle_every"), "| due now:", cluster.oracle_due(M.NODE, need=True))
    await tg.press(U, "m:rm"); print([l for l in tg.last_text(U).split("\n") if "Oracle" in l])
    print("manager plan oracle every:", M.manager_plan()["oracle"]["oracle_every"])
asyncio.run(main())
