import asyncio, os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = "/tmp/tlo"; os.makedirs(D, exist_ok=True)
import shutil; shutil.rmtree(D, ignore_errors=True); os.makedirs(D, exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH=f"{D}/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", SERVER_COST="free")
import httpx
import faketg; tg = faketg.install()
import bot, cluster, ollama_admin as OL

# фальшива Ollama
MODELS = [{"name": "gemma3:4b", "size": 3_300_000_000}]
def handler(req):
    p = req.url.path
    if p == "/api/tags": return httpx.Response(200, json={"models": MODELS})
    if p == "/api/ps": return httpx.Response(200, json={"models": [{"name": "gemma3:4b", "size": 4_100_000_000}]})
    if p == "/api/version": return httpx.Response(200, json={"version": "0.12.0"})
    if p == "/api/pull":
        name = json.loads(req.content)["model"]; MODELS.append({"name": name, "size": 1_900_000_000})
        lines = [{"status": "pulling", "total": 100, "completed": 50}, {"status": "success"}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
    if p == "/api/delete":
        name = json.loads(req.content)["model"]; MODELS[:] = [m for m in MODELS if m["name"] != name]; return httpx.Response(200)
    return httpx.Response(404)
_orig = httpx.AsyncClient.__init__
def patched(self, *a, **k):
    k["transport"] = httpx.MockTransport(handler); _orig(self, *a, **k)

async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    # 🔗 посилання
    await tg.press(U, "m:lnk"); print("links:", [t for r in tg.last_buttons(U) for t, d in r])
    await tg.press(U, "m:lnkgh"); await tg.text(U, "https://github.com/ivan/OrionAI.git")
    print("repo:", cluster.github_repo(bot.NODE), [d for r in tg.last_buttons(U) for t, d in r if "Репозиторій" == t.split(" ", 1)[-1]])
    await tg.text(U, "/start"); await tg.press(U, "m:lnkgh"); await tg.text(U, "не адреса"); print("bad:", tg.last_text(U)[:40])
    # 🦙 Ollama
    await tg.press(U, "m:cancel") if False else None
    bot.AWAIT.pop(U, None)
    await tg.press(U, "m:ol"); print("ollama off:", tg.last_text(U).split("\n")[2][:60])
    httpx.AsyncClient.__init__ = patched
    await tg.press(U, "m:olurl"); await tg.text(U, "http://ollama:11434")
    print("provider:", "ollama_local" in bot.PROVIDERS, "in auto:", "ollama_local" in bot.AUTO_ORDER)
    print([l for l in tg.last_text(U).split("\n") if l.startswith(("Стан", "●", "○", "🧠"))])
    j = [x[0] for x in OL.RECOMMENDED].index("qwen2.5:3b")
    await tg.press(U, f"m:olpull:{j}"); await asyncio.sleep(0.3)
    print("after pull:", tg.last_text(U)[:60])
    await tg.press(U, "m:ol"); i = [m["name"] for m in MODELS].index("qwen2.5:3b")
    await tg.press(U, f"m:oluse:{i}"); print("model:", bot.PROVIDERS["ollama_local"]["model"])
    await tg.press(U, f"m:oldel:0"); await tg.press(U, "m:oldely:0"); print("left:", [m["name"] for m in MODELS])
    httpx.AsyncClient.__init__ = _orig
    # 🔔 спільний стан нагадування: дочірній → головний
    a, b = cluster.Node(D, f"{D}/x.db", bot.TZ, fname="n1.json"), cluster.Node(D, f"{D}/y.db", bot.TZ, fname="n2.json")
    cluster.oracle_route(a, "orset", "21"); time.sleep(0.01); cluster.oracle_route(a, "orok", "")
    print("merge:", cluster.oracle_merge(b, cluster.oracle_state(a)), b.get("oracle_every"), b.get("oracle_next") == a.get("oracle_next"),
          "| повторно:", cluster.oracle_merge(b, cluster.oracle_state(a)), "| старіше назад:", cluster.oracle_merge(a, {"ts": 1, "oracle_every": 7}))
    print("plan has oracle:", "oracle" in bot.my_plan())
asyncio.run(main())
