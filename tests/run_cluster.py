"""Два сервери в одному процесі: A (головний) і B (резервний). Мережа — через httpx.MockTransport до їхніх маршрутів."""
import asyncio, os, sys, sqlite3, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zoneinfo import ZoneInfo
import httpx
from starlette.applications import Starlette
os.environ.update(CLUSTER_KEY="k" * 20, CLUSTER_NODES="A=https://a.example,B=https://b.example", CLUSTER_SYNC="0")
import cluster
def mkdb(d, val):
    p = os.path.join(d, "bot.db"); c = sqlite3.connect(p); c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)"); c.execute("INSERT OR REPLACE INTO kv VALUES('x',?)", (val,)); c.commit(); c.close(); return p
def val(p): return sqlite3.connect(p).execute("SELECT v FROM kv WHERE k='x'").fetchone()[0]
nodes, apps, UP = {}, {}, {"A": True, "B": True}
for n in "AB":
    d = tempfile.mkdtemp(); os.environ["CLUSTER_NAME"] = n
    nodes[n] = cluster.Node(d, mkdb(d, f"дані {n}"), ZoneInfo("Europe/Kyiv"))
    nodes[n].active = False
log = []
for n in "AB":
    async def deact(n=n): log.append(f"{n}: вимкнувся")
    apps[n] = Starlette(routes=nodes[n].routes(deact))
class Net(httpx.AsyncBaseTransport):
    async def handle_async_request(self, req):
        n = "A" if req.url.host == "a.example" else "B"
        if not UP[n]: raise httpx.ConnectError("down")
        return await httpx.ASGITransport(app=apps[n]).handle_async_request(req)
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k): k["transport"] = Net(); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
async def tick(n):
    async def act(): log.append(f"{n}: працює")
    async def deact(): log.append(f"{n}: у резерві")
    async def notify(t): log.append(f"{n} → вам: {t}")
    await nodes[n].tick(act, deact, notify)
async def main():
    for _ in range(4):
        await tick("A"); await tick("B")
    print("старт:", "A active" if nodes["A"].active else "", "| B active:", nodes["B"].active, "| B має базу A:", val(nodes["B"].db_path))
    mkdb(os.path.dirname(nodes["A"].db_path), "нові дані A"); nodes["A"].touch()
    await tick("B"); print("резерв синхронізувався:", val(nodes["B"].db_path))
    UP["A"] = False; print("\n— A впав —")
    for _ in range(3): await tick("B")
    print("B active:", nodes["B"].active)
    mkdb(os.path.dirname(nodes["B"].db_path), "дані, записані на B")
    UP["A"] = True; print("\n— A повернувся (той самий процес, був лише розрив мережі) —")
    for _ in range(4):
        await tick("A"); await tick("B")
    print("A active:", nodes["A"].active, "| B active:", nodes["B"].active, "| база A:", val(nodes["A"].db_path))
    print("\n".join(log)); log.clear()
    # сценарій 2: A справді перезапустився (на Railway закінчився кредит, потім відновився)
    UP["A"] = False
    for _ in range(3): await tick("B")
    mkdb(os.path.dirname(nodes["B"].db_path), "ще новіші дані з B")
    nodes["A"].active = False; UP["A"] = True; print("\n— A перезапустився —")
    for _ in range(4):
        await tick("A"); await tick("B")
    print("A active:", nodes["A"].active, "| B active:", nodes["B"].active, "| база A:", val(nodes["A"].db_path))
    print("\n".join(log)); log.clear()
    # сценарій 3: вручну перемикаємо на B з меню A
    await nodes["A"].prefer("B")
    for _ in range(3):
        await tick("A"); await tick("B")
    print("\nвручну «B»: A active:", nodes["A"].active, "| B active:", nodes["B"].active)
    await nodes["B"].prefer(None)
    for _ in range(4):
        await tick("A"); await tick("B")
    print("автоматично знову: A active:", nodes["A"].active, "| B active:", nodes["B"].active)
    # чужий без ключа
    async with httpx.AsyncClient() as c:
        r = await c.get("https://a.example/cluster/db"); print("\nбез ключа:", r.status_code)
asyncio.run(main())
async def selection():
    A, B = nodes["A"], nodes["B"]
    # A — платний (Railway), B — безкоштовний: в «🤖 Автоматично» має працювати B, хоч він і другий у списку
    os.environ["CLUSTER_NAME"] = "A"
    A.cost = lambda: "paid"; B.cost = lambda: "free"
    for n in (A, B): n.set("select", "auto"); n.set("pool", None); n.set("prefer", None); n.last_check = 0; n.kick = True
    for _ in range(5):
        A.kick = B.kick = True; await tick("A"); await tick("B")
    print("\n🤖 авто (A платний, B безкоштовний): A active:", A.active, "| B active:", B.active)
    from cluster import cluster_route
    await cluster_route(A, "clsel", "list")
    for _ in range(5):
        A.kick = B.kick = True; await tick("A"); await tick("B")
    print("📋 за списком: A active:", A.active, "| B active:", B.active)
    await cluster_route(A, "clpool", "A")
    for _ in range(4):
        A.kick = B.kick = True; await tick("A"); await tick("B")
    print("🚫 A вимкнено зі списку: A active:", A.active, "| B active:", B.active)
    # режим «🤖 за сервером»
    A.set("mode", "auto"); B.set("mode", "auto")
    print("режим auto: на A (платний):", A.mode(), "| на B (безкоштовний):", B.mode())
asyncio.run(selection())
